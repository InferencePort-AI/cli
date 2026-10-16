"""`ipx p2g embed` and `ipx p2g realtime ...`: the embedding and realtime modalities (Pay-2-Go only)."""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import click

from ipx import cloud, ui
from ipx import realtime as rt
from ipx import ws as wsmod
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = 32          # the API accepts at most 32 inputs per request; larger sets are split automatically
MAX_EMBED_INPUTS = 1024   # per command, i.e. at most 32 requests
VAD_TAIL_SECONDS = 1.5    # silence appended so a server-side turn detector closes the last utterance


def write_atomic(path: str, data: bytes) -> None:
    """Write ``path`` through a unique temporary file in the same folder, so concurrent writers never interleave."""
    path = os.path.expanduser(path)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ipx-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_inputs(texts, files, lines: bool, use_stdin: bool) -> List[str]:
    inputs: List[str] = [t for t in texts if t and t != "-"]
    if "-" in texts:
        use_stdin = True
    for path in files:
        with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        inputs += [ln for ln in body.splitlines() if ln.strip()] if lines else [body]
    if use_stdin or (not inputs and not sys.stdin.isatty()):
        data = sys.stdin.read()
        inputs += [ln for ln in data.splitlines() if ln.strip()] if lines else ([data] if data.strip() else [])
    inputs = [i for i in inputs if i.strip()]
    if not inputs:
        raise IpxError("Nothing to embed.", "Pass text as arguments, with -f FILE, or pipe it in (add --lines for one input per line).")
    if len(inputs) > MAX_EMBED_INPUTS:
        raise IpxError(f"Too many inputs ({len(inputs)}); the limit is {MAX_EMBED_INPUTS} per command.")
    return inputs


def add_embed(group: click.Group) -> None:
    @group.command("embed", examples=[
        "ipx p2g embed \"The quick brown fox\"",
        "ipx p2g embed \"first\" \"second\" -m text-embedding-3-large --dimensions 256",
        "ipx p2g embed -f notes.txt --lines -o vectors.json",
        "ipx p2g embed \"how do I reset my password\" -m cohere-embed-v4 --input-type query",
        "cat docs.txt | ipx p2g embed --lines --json | jq '.data[0].embedding | length'"])
    @click.argument("texts", nargs=-1)
    @click.option("-m", "--model", default=DEFAULT_EMBED_MODEL, show_default=True, help="Embedding model (see `ipx p2g models --type embedding`).")
    @click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False), help="Embed a file's contents (repeatable).")
    @click.option("--lines", is_flag=True, help="Treat every non-empty line of files and stdin as its own input.")
    @click.option("--stdin", "use_stdin", is_flag=True, help="Also read inputs from standard input.")
    @click.option("--dimensions", type=click.IntRange(128, 4096), help="Shorten the vectors (128-4096; limits depend on the model).")
    @click.option("--input-type", type=click.Choice(["query", "document"]), help="Retrieval role (Cohere models).")
    @click.option("--task-type", help="Retrieval task hint (Gemini models), e.g. RETRIEVAL_QUERY.")
    @click.option("-o", "--output", help="Write the full API response to this JSON file.")
    @pass_app
    def embed(app: App, texts, model: str, files, lines: bool, use_stdin: bool, dimensions: Optional[int],
              input_type: Optional[str], task_type: Optional[str], output: Optional[str]) -> None:
        """Turn text into embedding vectors, billed per input token. More than 32 inputs are sent in batches."""
        app.require_login()
        inputs = _read_inputs(texts, files, lines, use_stdin)
        extra: Dict[str, Any] = {}
        if dimensions:
            extra["dimensions"] = dimensions
        if input_type:
            extra["input_type"] = input_type
        if task_type:
            extra["task_type"] = task_type

        rows: List[Dict[str, Any]] = []
        tokens = 0
        credits: Optional[float] = None
        used_model = model
        with ui.status("Embedding…"):
            for start in range(0, len(inputs), EMBED_BATCH):
                batch = inputs[start:start + EMBED_BATCH]
                body: Dict[str, Any] = {"model": model, "input": batch if len(inputs) > 1 else batch[0], **extra}
                resp = app.api.request("POST", "/v1/embeddings", json=body, timeout=120)
                payload = resp.json() or {}
                got = sorted(payload.get("data") or [], key=lambda r: r.get("index", 0))
                if len(got) != len(batch):
                    raise IpxError("The API returned an unexpected number of embeddings.", f"Expected {len(batch)}, got {len(got)}.")
                for i, row in enumerate(got):
                    rows.append({**row, "index": start + i})
                usage = payload.get("usage") or {}
                tokens += int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
                c = cloud.credits_from(resp, payload)
                if c is not None:
                    credits = (credits or 0.0) + c
                used_model = payload.get("model") or used_model
        result = {"object": "list", "model": used_model, "data": rows,
                  "usage": {"prompt_tokens": tokens, "total_tokens": tokens,
                            **({"payg_credits_charged": credits} if credits is not None else {})}}
        if output:
            write_atomic(output, json.dumps(result).encode("utf-8"))

        def render(_: Any) -> None:
            table = []
            for i, row in enumerate(rows):
                vec = row.get("embedding") or []
                preview = ", ".join(f"{v:.4f}" for v in vec[:4]) + (", …" if len(vec) > 4 else "")
                shown = inputs[i].replace("\n", " ")
                table.append((i + 1, shown[:48] + ("…" if len(shown) > 48 else ""), len(vec), f"[{preview}]"))
            ui.console.print(ui.make_table(["#", "Input", "Dims", "Vector"], table, title=f"{used_model}", right=["Dims"]))
            bits = [f"{ui.fmt_num(tokens)} tokens"]
            if credits is not None:
                bits.append(f"{ui.fmt_credits(credits)} credits charged")
            if output:
                bits.append(f"saved → {output}")
            ui.dim("  " + " · ".join(bits))

        app.out(result, render)


def _session_arg(raw: Optional[str], default: Dict[str, Any]) -> Dict[str, Any]:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise IpxError("--session is not valid JSON.", str(e))
    if not isinstance(value, dict):
        raise IpxError("--session must be a JSON object.")
    return value


class _LineReader:
    """Reads stdin lines on a thread so the main loop can keep servicing the WebSocket (pings) while it waits."""

    def __init__(self) -> None:
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)


def add_realtime(group: click.Group) -> None:
    @group.group("realtime", cls=IpxGroup, short_help="Live voice chat and streaming transcription")
    def realtime_group() -> None:
        """Realtime sessions over the OpenAI Realtime protocol, billed while they run.

        Token models (gpt-realtime-*) are billed per text, audio and image token; transcription models are billed per
        second of audio you stream. List them with `ipx p2g models --type realtime`.
        """

    # -- chat ----------------------------------------------------------------
    @realtime_group.command("chat", examples=[
        "ipx p2g realtime chat \"Tell me a one-line joke\"",
        "ipx p2g realtime chat \"Say hello\" --audio-out hello.wav --voice marin",
        "ipx p2g realtime chat --audio-in question.wav",
        "ipx p2g realtime chat          # interactive: one message per line, Ctrl+D to end"])
    @click.argument("prompt", required=False)
    @click.option("-m", "--model", default=rt.DEFAULT_CHAT_MODEL, show_default=True, help="Realtime model.")
    @click.option("--voice", help="Voice for spoken replies (with --audio-out).")
    @click.option("--instructions", help="System instructions for the session.")
    @click.option("--audio-in", type=click.Path(exists=True, dir_okay=False), help="Send a WAV file instead of typing.")
    @click.option("--audio-out", help="Have the model speak and save the reply to this WAV file (otherwise replies are text only).")
    @click.option("--session", "session_json", metavar="JSON", help="Replace the default session.update payload.")
    @click.option("--events", is_flag=True, help="Print every raw event to stderr (debugging).")
    @pass_app
    def chat(app: App, prompt, model, voice, instructions, audio_in, audio_out, session_json, events) -> None:
        """Talk to a realtime model. Replies stream to stdout as text; spoken audio can be saved."""
        app.require_login()
        info = rt.model_info(app, model)
        if rt.is_transcription(info, model):
            raise IpxError(f"{model} is a transcription model.", "Use `ipx p2g realtime transcribe` for it.")
        session = _session_arg(session_json, rt.default_session(info, model, voice=voice, instructions=instructions,
                                                                  audio_reply=bool(audio_out)))

        messages: Optional[List[str]] = []  # None = interactive
        if audio_in:
            pass
        elif prompt:
            messages = [prompt]
        elif not sys.stdin.isatty():
            messages = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
        else:
            ui.info("Realtime chat. One message per line; Ctrl+D to end.")
            messages = None
        if messages is not None and not messages and not audio_in:
            raise IpxError("No message given.", "Pass it as an argument, pipe it in, or use --audio-in FILE.")

        audio = bytearray()
        totals = [0, 0]
        replies: List[str] = []
        backlog: List[Dict[str, Any]] = []   # events read while we were busy sending or waiting for input
        echo = not app.json

        try:
            with rt.open_session(app, model, session) as conn:
                def converse(send: Any) -> None:
                    send()
                    replies.append(_read_response(conn, backlog, audio, totals, events, echo))

                if audio_in:
                    def send_audio() -> None:
                        for chunk in rt.wav_chunks(audio_in):
                            conn.send_text(rt.append_event(chunk))
                            backlog.extend(rt.drain(conn))   # keep answering pings during a long upload
                        conn.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
                        conn.send_text(json.dumps({"type": "response.create"}))

                    converse(send_audio)
                elif messages is None:
                    reader = _LineReader()
                    while True:
                        line = None
                        got_line = False
                        while not got_line:   # service the socket while the user thinks
                            try:
                                line = reader.lines.get(timeout=0.25)
                                got_line = True
                            except queue.Empty:
                                backlog.extend(rt.drain(conn))
                        if line is None:
                            break
                        if line.strip():
                            converse(lambda text=line: _send_text(conn, text))
                else:
                    for m in messages:
                        converse(lambda text=m: _send_text(conn, text))
        except wsmod.WebSocketClosed as e:
            raise rt.closed_error(e)
        finally:
            # Written even when the session failed part-way: the audio that arrived was already paid for.
            saved = None
            if audio_out and audio:
                write_atomic(audio_out, rt.wav_bytes(bytes(audio)))
                saved = audio_out
        if audio_out and not saved:
            ui.warn(f"The model sent no audio, so {audio_out} was not written (an existing file there is from an earlier run).")

        text = "\n".join(r for r in replies if r)
        if app.json:
            ui.emit_json({"model": model, "text": text, "replies": replies, "audio_file": saved,
                          "usage": {"input_tokens": totals[0], "output_tokens": totals[1]}})
            return
        if saved:
            ui.ok(f"Saved reply audio → [bold]{saved}[/bold]")
        ui.dim(f"  {ui.fmt_num(totals[0])} input / {ui.fmt_num(totals[1])} output tokens · exact charges: `ipx p2g ledger`")

    # -- transcribe ----------------------------------------------------------
    @realtime_group.command("transcribe", examples=[
        "ipx p2g realtime transcribe meeting.wav",
        "ipx p2g realtime transcribe talk.wav -m scribe-v2-realtime -o talk.txt",
        "arecord -f S16_LE -r 24000 -c 1 -t raw | ipx p2g realtime transcribe - --rate 24000"])
    @click.argument("file")
    @click.option("-m", "--model", default=rt.DEFAULT_TRANSCRIBE_MODEL, show_default=True, help="Realtime transcription model.")
    @click.option("--rate", type=click.IntRange(8000, 48000), default=rt.PCM_RATE, show_default=True,
                  help="Sample rate of raw PCM16 from stdin (`-`). WAV files carry their own.")
    @click.option("--pace", is_flag=True, help="Send audio at real-time speed instead of as fast as possible.")
    @click.option("--wait", type=click.FloatRange(1, 600), default=60.0, show_default=True,
                  help="Seconds to wait for the first transcript after the audio ends.")
    @click.option("--idle", type=click.FloatRange(0.5, 60), default=4.0, show_default=True,
                  help="Seconds of silence from the server, after a transcript, before finishing.")
    @click.option("--session", "session_json", metavar="JSON", help="Replace the default session.update payload.")
    @click.option("-o", "--output", help="Save the transcript to a file.")
    @click.option("--events", is_flag=True, help="Print every raw event to stderr (debugging).")
    @pass_app
    def transcribe(app: App, file, model, rate, pace, wait, idle, session_json, output, events) -> None:
        """Stream a recording (WAV file, or raw PCM16 on stdin with `-`) and print the transcript."""
        app.require_login()
        info = rt.model_info(app, model)
        if not rt.is_transcription(info, model):
            raise IpxError(f"{model} is a conversational model.", "Use `ipx p2g realtime chat` for it, or pick a transcription model.")
        session = _session_arg(session_json, rt.default_session(info, model))
        manual = rt.manual_commit(session)

        if file == "-":
            chunks: Iterator[bytes] = rt.stdin_chunks(sys.stdin.buffer, rate)
        else:
            path = os.path.expanduser(file)
            if not os.path.isfile(path):
                raise IpxError(f"File not found: {file}")
            chunks = rt.wav_chunks(path)

        finals: List[str] = []
        partial: List[str] = []

        def on_event(ev: Dict[str, Any]) -> None:
            if events:
                ui.err.print(json.dumps(ev)[:400], highlight=False, markup=False)
            failure = rt.error_from_event(ev)
            if failure:
                raise failure
            kind, text = rt.text_of(ev)
            if kind == "delta":
                partial.append(text)
            elif kind == "final" and text.strip():
                finals.append(text.strip())
                partial.clear()

        sent_bytes = 0
        try:
            with rt.open_session(app, model, session) as conn:
                started = time.monotonic()
                for chunk in chunks:
                    conn.send_text(rt.append_event(chunk))
                    sent_bytes += len(chunk)
                    if pace:
                        wait_for = sent_bytes / 2 / rt.PCM_RATE - (time.monotonic() - started)
                        if wait_for > 0:
                            time.sleep(wait_for)
                    for ev in rt.drain(conn):
                        on_event(ev)
                if not sent_bytes:
                    raise IpxError("The audio is empty.")
                if manual:
                    conn.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
                else:
                    # Server-side turn detection closes an utterance on silence: give the last one some.
                    tail = rt.silence(VAD_TAIL_SECONDS)
                    for chunk in rt.pcm_chunks(tail):
                        conn.send_text(rt.append_event(chunk))
                    sent_bytes += len(tail)
                # Wait for the transcript itself (not just for a quiet moment), then let trailing segments arrive.
                deadline = time.monotonic() + wait
                while not (finals or partial) and time.monotonic() < deadline:
                    for ev in rt.drain(conn, timeout=min(1.0, max(0.05, deadline - time.monotonic()))):
                        on_event(ev)
                while True:
                    got = rt.drain(conn, timeout=idle)
                    if not got:
                        break
                    for ev in got:
                        on_event(ev)
        except wsmod.WebSocketClosed as e:
            if not rt.is_normal_close(e.code):
                raise rt.closed_error(e)
            # A normal close after the transcript is complete is fine.

        if partial:
            finals.append("".join(partial).strip())
        text = "\n".join(f for f in finals if f)
        seconds = sent_bytes / 2 / rt.PCM_RATE
        if not text:
            ui.warn("No transcript was received.")
        if output:
            write_atomic(output, (text + "\n").encode("utf-8"))
            if not app.json:
                ui.ok(f"Saved transcript → [bold]{output}[/bold]")
        if app.json:
            ui.emit_json({"text": text, "segments": finals, "audio_seconds": round(seconds, 2), "model": model})
        elif not output:
            sys.stdout.write(text + "\n")
        if not app.json:
            ui.dim(f"  {seconds:.1f}s of audio streamed · exact charges: `ipx p2g ledger`")


def _send_text(conn: wsmod.WebSocket, text: str) -> None:
    conn.send_text(json.dumps({"type": "conversation.item.create",
                               "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
    conn.send_text(json.dumps({"type": "response.create"}))


def _read_response(conn: wsmod.WebSocket, backlog: List[Dict[str, Any]], audio: bytearray, totals: List[int],
                   show_events: bool, echo: bool) -> str:
    """Collect one response (printing its text as it streams when ``echo``); stop at ``response.done``."""
    streamed: List[str] = []
    finals: List[str] = []
    while True:
        if backlog:
            ev: Optional[Dict[str, Any]] = backlog.pop(0)
        else:
            raw = conn.recv_text(timeout=120)
            if raw is None:
                raise IpxError("The model did not answer in time.")
            ev = rt.parse(raw)
        if not ev:
            continue
        if show_events:
            ui.err.print(json.dumps(ev)[:400], highlight=False, markup=False)
        failure = rt.error_from_event(ev)
        if failure:
            raise failure
        t = str(ev.get("type") or "")
        if t in ("response.output_audio.delta", "response.audio.delta") and isinstance(ev.get("delta"), str):
            audio += base64.b64decode(ev["delta"])
            continue
        kind, text = rt.text_of(ev)
        if kind == "delta":
            streamed.append(text)
            if echo:
                sys.stdout.write(text)
                sys.stdout.flush()
        elif kind == "final" and text.strip():
            finals.append(text.strip())
        done = rt.usage_totals(ev)
        if done is not None or t == "response.done":
            if done:
                totals[0] += done[0]
                totals[1] += done[1]
            if streamed:
                if echo:
                    sys.stdout.write("\n")
                return "".join(streamed)
            if finals:  # the model sent only completed text, no incremental deltas
                if echo:
                    sys.stdout.write(finals[-1] + "\n")
                return finals[-1]
            return ""
