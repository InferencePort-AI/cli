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


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int = PCM_RATE) -> bytes:
    """Linear-interpolation resample of mono little-endian PCM16."""
    samples = array.array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder == "big":  # pragma: no cover
        samples.byteswap()
    if src_rate == dst_rate or not samples:
        out = samples
    else:
        ratio = src_rate / dst_rate
        n = int(len(samples) / ratio)
        last = len(samples) - 1
        out = array.array("h", bytes(2 * n))
        for i in range(n):
            pos = i * ratio
            i0 = int(pos)
            frac = pos - i0
            a = samples[i0]
            b = samples[i0 + 1] if i0 < last else a
            out[i] = int(a + (b - a) * frac)
    if sys.byteorder == "big":  # pragma: no cover
        out.byteswap()
    return out.tobytes()


def pcm16_from_wav(path: str) -> bytes:
    """Read a PCM WAV file as mono 24 kHz PCM16."""
    try:
        with wave.open(path, "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise IpxError(f"Could not read {path} as a WAV file.",
                       f"{e}. Convert it first: ffmpeg -i in.mp3 -ac 1 -ar 24000 -f wav out.wav")
    if width != 2:
        raise IpxError(f"{path} is {width * 8}-bit audio; 16-bit PCM is required.",
                       "Convert it: ffmpeg -i in.wav -ac 1 -ar 24000 -sample_fmt s16 out.wav")
    if channels > 1:  # keep the first channel
        samples = array.array("h")
        samples.frombytes(frames)
        frames = samples[::channels].tobytes()
    return resample_pcm16(frames, rate)


def pcm_chunks(pcm: bytes, samples: int = CHUNK_SAMPLES) -> Iterator[bytes]:
    step = samples * 2
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]


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


# --------------------------------------------------------------------------- connection


def realtime_url(base_url: str, model: str) -> str:
    scheme = "wss" if base_url.lower().startswith("https") else "ws"
    host = base_url.split("://", 1)[-1].rstrip("/")
    return f"{scheme}://{host}/v1/realtime?" + urllib.parse.urlencode({"model": model})


def open_session(app: App, model: str, session_update: Optional[Dict[str, Any]]) -> wsmod.WebSocket:
    """Connect to ``/v1/realtime`` as the signed-in user (or org) and send the initial ``session.update``."""
    app.require_login()
    app.api._maybe_refresh()
    token = app.creds.access_token
    headers = {"Authorization": f"Bearer {token}", "X-Client-ID": app.cfg.client_id}
    if app.org:
        headers[ORG_HEADER] = app.org
    conn = wsmod.connect(realtime_url(app.api.base_url, model), headers=headers, subprotocol="realtime")
    if session_update:
        conn.send_text(json.dumps({"type": "session.update", "session": session_update}))
    return conn


def closed_error(e: wsmod.WebSocketClosed) -> IpxError:
    message, hint = CLOSE_HINTS.get(e.code, (e.reason or f"The connection was closed (code {e.code}).", None))
    return ApiError(402 if e.code == 4402 else 400, message, hint) if e.code in CLOSE_HINTS else IpxError(message, hint)


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
