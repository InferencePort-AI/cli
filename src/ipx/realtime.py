"""Realtime (OpenAI Realtime protocol) helpers: audio conversion, session setup and event handling."""

from __future__ import annotations

import array
import base64
import io
import json
import sys
import urllib.parse
import wave
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ipx import ws as wsmod
from ipx.app import App
from ipx.errors import ApiError, IpxError
from ipx.http import ORG_HEADER

PCM_RATE = 24000          # what the API meters by default and what we always send
CHUNK_SAMPLES = 4800      # 200 ms per input_audio_buffer.append
DEFAULT_CHAT_MODEL = "gpt-realtime-2.1"
DEFAULT_TRANSCRIBE_MODEL = "gpt-live-transcribe"

# Close codes the server uses (see the API docs) with a message that says what to do.
CLOSE_HINTS = {
    4400: ("The request was rejected as invalid.", None),
    4401: ("You are not signed in, or the key was rejected.", "Run `ipx auth login`."),
    4402: ("You are out of credits.", "Check `ipx p2g balance` and top up in the console."),
    4403: ("Your organization's policy does not allow this model.", "Ask an org admin, or use `ipx auth context --clear`."),
    4404: ("That realtime model was not found.", "List them with `ipx p2g models --type realtime`."),
    4408: ("The session reached its maximum length.", None),
    4429: ("Too many realtime sessions are already open for this account.", None),
    1011: ("The realtime service is unavailable.", "Try again shortly; check `ipx status`."),
}


# --------------------------------------------------------------------------- audio

_BE = sys.byteorder == "big"


class Resampler:
    """Streaming linear-interpolation resampler for mono little-endian PCM16.

    ``feed`` accepts audio in pieces of any size and returns the converted samples that are ready; ``flush`` returns
    the tail. Positions are tracked as exact integers (in units of 1/``dst_rate`` input samples), so feeding a
    recording in chunks gives byte-identical output to converting it in one go, and large inputs never have to be
    held in memory.
    """

    def __init__(self, src_rate: int, dst_rate: int = PCM_RATE) -> None:
        self.src = src_rate
        self.dst = dst_rate
        self.buf = array.array("h")
        self.num = 0          # position of the next output sample, in 1/dst input samples from buf[0]
        self._odd = b""

    def _interp(self, buf: "array.array[int]", i0: int, rem: int) -> int:
        a = buf[i0]
        b = buf[i0 + 1] if i0 + 1 < len(buf) else a
        return int((a * self.dst + (b - a) * rem) / self.dst)

    def feed(self, data: bytes) -> bytes:
        data = self._odd + data
        keep = len(data) - (len(data) % 2)
        self._odd = data[keep:]
        samples = array.array("h")
        samples.frombytes(data[:keep])
        if _BE:  # pragma: no cover
            samples.byteswap()
        if self.src == self.dst:
            out = samples
        else:
            self.buf.extend(samples)
            out = array.array("h")
            buf, num, dst = self.buf, self.num, self.dst
            while num // dst + 1 < len(buf):          # both neighbours are available
                out.append(self._interp(buf, num // dst, num % dst))
                num += self.src
            drop = min(num // dst, len(buf))
            del buf[:drop]
            self.num = num - drop * dst
        if _BE:  # pragma: no cover
            out.byteswap()
        return out.tobytes()

    def flush(self) -> bytes:
        """The remaining sample(s); the last input sample is held, as the one-shot conversion does."""
        if self.src == self.dst or not len(self.buf):
            return b""
        out = array.array("h")
        buf, num, dst = self.buf, self.num, self.dst
        while num // dst < len(buf):
            out.append(self._interp(buf, num // dst, num % dst))
            num += self.src
        self.buf = array.array("h")
        self.num = 0
        if _BE:  # pragma: no cover
            out.byteswap()
        return out.tobytes()


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int = PCM_RATE) -> bytes:
    """One-shot conversion of mono PCM16 (small inputs and tests; use :class:`Resampler` for streams)."""
    r = Resampler(src_rate, dst_rate)
    return r.feed(data) + r.flush()


def _rechunk(pieces: Iterator[bytes], samples: int = CHUNK_SAMPLES) -> Iterator[bytes]:
    """Re-slice a stream of PCM16 byte strings into fixed-size chunks (the last one may be shorter)."""
    step = samples * 2
    buf = bytearray()
    for piece in pieces:
        buf += piece
        while len(buf) >= step:
            yield bytes(buf[:step])
            del buf[:step]
    if buf:
        yield bytes(buf)


def wav_chunks(path: str) -> Iterator[bytes]:
    """Read a PCM WAV file incrementally as mono 24 kHz PCM16 chunks of ~200 ms."""
    try:
        w = wave.open(path, "rb")
    except (wave.Error, EOFError, OSError) as e:
        raise IpxError(f"Could not read {path} as a WAV file.",
                       f"{e}. Convert it first: ffmpeg -i in.mp3 -ac 1 -ar 24000 -f wav out.wav")
    with w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if width != 2:
            raise IpxError(f"{path} is {width * 8}-bit audio; 16-bit PCM is required.",
                           "Convert it: ffmpeg -i in.wav -ac 1 -ar 24000 -sample_fmt s16 out.wav")
        resampler = Resampler(rate)

        def pieces() -> Iterator[bytes]:
            while True:
                try:
                    frames = w.readframes(max(rate, 1))  # about one second at a time
                except (wave.Error, EOFError, OSError) as e:
                    raise IpxError(f"Could not read audio from {path}.", str(e))
                if not frames:
                    break
                if channels > 1:  # keep the first channel
                    samples = array.array("h")
                    samples.frombytes(frames[: len(frames) - (len(frames) % (2 * channels))])
                    frames = samples[::channels].tobytes()
                yield resampler.feed(frames)
            yield resampler.flush()

        yield from _rechunk(pieces())


def stdin_chunks(stream: Any, rate: int) -> Iterator[bytes]:
    """Read raw PCM16 from ``stream`` as it arrives (so a live ``arecord`` pipe streams while it records)."""
    resampler = Resampler(rate)

    def pieces() -> Iterator[bytes]:
        read = getattr(stream, "read1", None) or stream.read
        while True:
            data = read(65536)
            if not data:
                break
            yield resampler.feed(data)
        yield resampler.flush()

    yield from _rechunk(pieces())


def pcm16_from_wav(path: str) -> bytes:
    """A whole (small) WAV file as mono 24 kHz PCM16."""
    return b"".join(wav_chunks(path))


def pcm_chunks(pcm: bytes, samples: int = CHUNK_SAMPLES) -> Iterator[bytes]:
    step = samples * 2
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]


def silence(seconds: float) -> bytes:
    return bytes(int(PCM_RATE * seconds) * 2)


def wav_bytes(pcm: bytes, rate: int = PCM_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def append_event(chunk: bytes) -> str:
    return json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode()})


# --------------------------------------------------------------------------- sessions

PCM_FORMAT = {"type": "audio/pcm", "rate": PCM_RATE}

# Built-in defaults in the upstream's (GA) Realtime shape, used when the model catalog cannot be fetched. The catalog
# (`GET /v1/realtime/models`) is the source of truth and wins whenever it is reachable.
FALLBACK_SESSIONS: Dict[str, Dict[str, Any]] = {
    "conversation": {"type": "realtime", "output_modalities": ["audio"],
                     "audio": {"input": {"format": PCM_FORMAT, "turn_detection": {"type": "server_vad"}},
                               "output": {"format": PCM_FORMAT}}},
    "gpt-live-transcribe": {"type": "transcription", "audio": {"input": {
        "format": PCM_FORMAT, "transcription": {"model": "gpt-live-transcribe"}, "turn_detection": {"type": "server_vad"}}}},
    "scribe-v2-realtime": {"type": "transcription", "audio": {"input": {"format": PCM_FORMAT, "turn_detection": None}}},
    "mai-transcribe-2-streaming": {"type": "transcription", "audio": {"input": {
        "format": PCM_FORMAT, "transcription": {"model": "microsoft/mai-transcribe-2-streaming"},
        "turn_detection": None, "noise_reduction": None}}},
}
TRANSCRIPTION_IDS = ("gpt-live-transcribe", "scribe-v2-realtime", "mai-transcribe-2-streaming")
_FALLBACK_ALIASES = {"scribe-realtime": "scribe-v2-realtime"}


def _short_id(model: str) -> str:
    short = model.split("/")[-1]
    return _FALLBACK_ALIASES.get(short, short)


def model_info(app: App, model: str) -> Optional[Dict[str, Any]]:
    """The catalog entry for ``model`` (matching id or alias), or ``None`` if the catalog is unavailable."""
    try:
        data = app.api.get("/v1/realtime/models", auth=False, org=False) or {}
    except IpxError:
        return None
    for m in (data.get("data") if isinstance(data, dict) else None) or []:
        if isinstance(m, dict) and (m.get("id") == model or model in (m.get("aliases") or [])):
            return m
    return None


def is_transcription(info: Optional[Dict[str, Any]], model: str) -> bool:
    if info and info.get("session_kind"):
        return info["session_kind"] == "transcription"
    return _short_id(model) in TRANSCRIPTION_IDS


def default_session(info: Optional[Dict[str, Any]], model: str, *, voice: Optional[str] = None,
                    instructions: Optional[str] = None, audio_reply: bool = False) -> Dict[str, Any]:
    """The ``session.update`` body for ``model``.

    Conversational sessions answer in text unless an audio reply is wanted (``audio_reply``), so a plain text chat is
    never billed for audio nobody listens to. Conversational models reject input-transcription settings, so none are
    ever added.
    """
    if info and isinstance(info.get("default_session"), dict):
        session = json.loads(json.dumps(info["default_session"]))
    elif is_transcription(info, model):
        session = json.loads(json.dumps(FALLBACK_SESSIONS.get(_short_id(model)) or FALLBACK_SESSIONS["scribe-v2-realtime"]))
    else:
        session = json.loads(json.dumps(FALLBACK_SESSIONS["conversation"]))
    if session.get("type") == "realtime":
        session["output_modalities"] = ["audio"] if audio_reply else ["text"]
        if instructions:
            session["instructions"] = instructions
        if voice and audio_reply:
            session.setdefault("audio", {}).setdefault("output", {})["voice"] = voice
    return session


def manual_commit(session: Dict[str, Any]) -> bool:
    """True when the session has no server-side turn detection, so audio must be committed explicitly."""
    audio_in = ((session.get("audio") or {}).get("input")) or {}
    return audio_in.get("turn_detection") is None


# --------------------------------------------------------------------------- connection


def realtime_url(base_url: str, model: str) -> str:
    """``wss://host[/prefix]/v1/realtime?model=...`` for an API base URL (any path prefix is kept)."""
    parts = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parts.scheme.lower() == "https" else "ws"
    prefix = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, parts.netloc, f"{prefix}/v1/realtime",
                                    urllib.parse.urlencode({"model": model}), ""))


def open_session(app: App, model: str, session_update: Optional[Dict[str, Any]]) -> wsmod.WebSocket:
    """Connect to ``/v1/realtime`` as the signed-in user (or org) and send the initial ``session.update``.

    A 401 at the handshake refreshes the session once and retries, like every other API call.
    """
    app.require_login()
    app.api._maybe_refresh()
    url = realtime_url(app.api.base_url, model)
    conn: Optional[wsmod.WebSocket] = None
    for attempt in (0, 1):
        headers = {"Authorization": f"Bearer {app.creds.access_token}", "X-Client-ID": app.cfg.client_id}
        if app.org:
            headers[ORG_HEADER] = app.org
        try:
            conn = wsmod.connect(url, headers=headers, subprotocol="realtime")
            break
        except ApiError as e:
            refresher = app.api.refresher
            if e.status == 401 and attempt == 0 and refresher and app.creds.data.get("refresh_token") and refresher(app.cfg, app.creds):
                continue
            if e.status == 401:
                e.hint = "Your session expired or the token was rejected. Run `ipx auth login`."
            raise
    assert conn is not None
    if session_update:
        conn.send_text(json.dumps({"type": "session.update", "session": session_update}))
    return conn


CLOSE_STATUS = {4400: 400, 4401: 401, 4402: 402, 4403: 403, 4404: 404, 4408: 408, 4429: 429, 1011: 502}


def closed_error(e: wsmod.WebSocketClosed) -> IpxError:
    """A normal :class:`IpxError` for an unexpected close (a mapped hint for our own codes, the reason otherwise)."""
    if e.code in CLOSE_HINTS:
        message, hint = CLOSE_HINTS[e.code]
        return ApiError(CLOSE_STATUS.get(e.code, 400), message, hint)
    reason = e.reason or (f"The connection was lost (code {e.code})." if e.code == 1006 else f"The server closed the connection (code {e.code}).")
    return IpxError(reason, "Try again; check `ipx status` if it keeps happening." if e.code in (1006, 1011) else None)


def is_normal_close(code: Optional[int]) -> bool:
    return code in (1000, 1001, 1005)


def parse(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    try:
        ev = json.loads(raw)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def text_of(ev: Dict[str, Any]) -> Tuple[str, str]:
    """``(kind, text)`` for transcript / text events: kind is ``delta`` or ``final`` ('' when not a text event)."""
    t = str(ev.get("type") or "")
    if t.endswith(".delta") and isinstance(ev.get("delta"), str) and (
            "transcript" in t or "text" in t) and "audio.delta" not in t and not t.endswith("output_audio.delta"):
        return "delta", ev["delta"]
    if (t.endswith(".done") or t.endswith(".completed")) and ("transcript" in t or "text" in t):
        value = ev.get("transcript") if isinstance(ev.get("transcript"), str) else ev.get("text")
        if isinstance(value, str):
            return "final", value
    return "", ""


def usage_totals(ev: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """``(input_tokens, output_tokens)`` from a ``response.done`` event, else ``None``."""
    if ev.get("type") != "response.done":
        return None
    usage = (ev.get("response") or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


# Error `code`s the server puts in its error events (before it closes the socket) -> HTTP-style status and hint.
ERROR_CODES = {
    "unauthorized": (401, "Run `ipx auth login`."),
    "insufficient_credits": (402, "Check `ipx p2g balance` and top up in the console."),
    "forbidden": (403, "Ask an org admin, or use `ipx auth context --clear`."),
    "model_not_found": (404, "List them with `ipx p2g models --type realtime`."),
    "rate_limited": (429, None),
    "session_time_limit": (408, "Start a new session to continue."),
}


def error_from_event(ev: Dict[str, Any]) -> Optional[IpxError]:
    """An :class:`IpxError` (with a hint where we know one) for an ``error`` event, else ``None``."""
    message = event_error(ev)
    if message is None:
        return None
    err = ev.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    if code in ERROR_CODES:
        status, hint = ERROR_CODES[code]
        return ApiError(status, message, hint)
    return IpxError(message)


def event_error(ev: Dict[str, Any]) -> Optional[str]:
    if ev.get("type") != "error":
        return None
    err = ev.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "The realtime API reported an error.")
    return str(err or "The realtime API reported an error.")


def drain(conn: wsmod.WebSocket, timeout: float = 0.0) -> List[Dict[str, Any]]:
    """Read every event that is already waiting (without blocking longer than ``timeout`` for the first)."""
    out: List[Dict[str, Any]] = []
    t = timeout
    while True:
        raw = conn.recv_text(t)
        if raw is None:
            return out
        t = 0.0
        ev = parse(raw)
        if ev:
            out.append(ev)
