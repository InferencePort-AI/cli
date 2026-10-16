"""`ipx p2g embed` and `ipx p2g realtime ...`: the embedding and realtime modalities (Pay-2-Go only)."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import click

from ipx import cloud, ui
from ipx import realtime as rt
from ipx import ws as wsmod
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
MAX_EMBED_INPUTS = 2048


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
        raise IpxError(f"Too many inputs ({len(inputs)}); the limit is {MAX_EMBED_INPUTS} per request.")
    return inputs


def add_embed(group: click.Group) -> None:
    @group.command("embed", examples=[
        "ipx p2g embed \"The quick brown fox\"",
        "ipx p2g embed \"first\" \"second\" -m text-embedding-3-large --dimensions 256",
        "ipx p2g embed -f notes.txt --lines -o vectors.json",
        "cat docs.txt | ipx p2g embed --lines --json | jq '.data[0].embedding | length'"])
    @click.argument("texts", nargs=-1)
    @click.option("-m", "--model", default=DEFAULT_EMBED_MODEL, show_default=True, help="Embedding model (see `ipx p2g models --type embedding`).")
    @click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False), help="Embed a file's contents (repeatable).")
    @click.option("--lines", is_flag=True, help="Treat every non-empty line of files and stdin as its own input.")
    @click.option("--stdin", "use_stdin", is_flag=True, help="Also read inputs from standard input.")
    @click.option("--dimensions", type=click.IntRange(1, 8192), help="Shorten the vectors (models that support it).")
    @click.option("-o", "--output", help="Write the full API response to this JSON file.")
    @pass_app
    def embed(app: App, texts, model: str, files, lines: bool, use_stdin: bool, dimensions: Optional[int], output: Optional[str]) -> None:
        """Turn text into embedding vectors, billed per input token."""
        app.require_login()
        inputs = _read_inputs(texts, files, lines, use_stdin)
        body: Dict[str, Any] = {"model": model, "input": inputs if len(inputs) > 1 else inputs[0]}
        if dimensions:
            body["dimensions"] = dimensions
        with ui.status("Embedding…"):
            resp = app.api.request("POST", "/v1/embeddings", json=body, timeout=120)
        payload = resp.json() or {}
        credits = cloud.credits_from(resp, payload)
        rows = sorted(payload.get("data") or [], key=lambda r: r.get("index", 0))
        if len(rows) != len(inputs):
            raise IpxError("The API returned an unexpected number of embeddings.", f"Expected {len(inputs)}, got {len(rows)}.")
        if output:
            with open(os.path.expanduser(output), "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

        def render(_: Any) -> None:
            usage = payload.get("usage") or {}
            table = []
            for i, row in enumerate(rows):
                vec = row.get("embedding") or []
                preview = ", ".join(f"{v:.4f}" for v in vec[:4]) + (", …" if len(vec) > 4 else "")
                shown = inputs[i].replace("\n", " ")
                table.append((i + 1, shown[:48] + ("…" if len(shown) > 48 else ""), len(vec), f"[{preview}]"))
            ui.console.print(ui.make_table(["#", "Input", "Dims", "Vector"], table, title=f"{payload.get('model') or model}", right=["Dims"]))
            bits = [f"{ui.fmt_num(usage.get('prompt_tokens') or usage.get('total_tokens') or 0)} tokens"]
            if credits is not None:
                bits.append(f"{ui.fmt_credits(credits)} credits charged")
            if output:
                bits.append(f"saved → {output}")
            ui.dim("  " + " · ".join(bits))

        app.out(payload, render)


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
    @click.option("--voice", help="Voice for spoken replies.")
    @click.option("--instructions", help="System instructions for the session.")
    @click.option("--audio-in", type=click.Path(exists=True, dir_okay=False), help="Send a WAV file instead of typing.")
    @click.option("--audio-out", help="Save the spoken reply to this WAV file.")
    @click.option("--session", "session_json", metavar="JSON", help="Replace the default session.update payload.")
    @click.option("--events", is_flag=True, help="Print every raw event to stderr (debugging).")
    @pass_app
    def chat(app: App, prompt, model, voice, instructions, audio_in, audio_out, session_json, events) -> None:
        """Talk to a realtime model. Replies stream to stdout as text; spoken audio can be saved."""
        app.require_login()
        default = {"modalities": ["text", "audio"], "input_audio_format": "pcm16", "output_audio_format": "pcm16"}
        if voice:
            default["voice"] = voice
        if instructions:
            default["instructions"] = instructions
        session = _session_arg(session_json, default)

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

        with rt.open_session(app, model, session) as conn:
            def converse(send: Any) -> None:
                send()
                _read_response(conn, audio, totals, events)

            try:
                if audio_in:
                    pcm = rt.pcm16_from_wav(audio_in)

                    def send_audio() -> None:
                        for c in rt.pcm_chunks(pcm):
                            conn.send_text(rt.append_event(c))
                        conn.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
                        conn.send_text(json.dumps({"type": "response.create"}))

                    converse(send_audio)
                elif messages is None:
                    while True:
                        try:
                            line = input("› ")
                        except EOFError:
                            break
                        if line.strip():
                            converse(lambda text=line: _send_text(conn, text))
                else:
                    for m in messages:
                        converse(lambda m=m: _send_text(conn, m))
            except wsmod.WebSocketClosed as e:
                raise rt.closed_error(e)

        if audio_out and audio:
            with open(os.path.expanduser(audio_out), "wb") as fh:
                fh.write(rt.wav_bytes(bytes(audio)))
            ui.ok(f"Saved reply audio → [bold]{audio_out}[/bold]")
        if not app.json:
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
    @click.option("--idle", type=click.FloatRange(0.5, 60), default=4.0, show_default=True,
                  help="Seconds of silence from the server after the last audio before finishing.")
    @click.option("--session", "session_json", metavar="JSON", help="Replace the default session.update payload.")
    @click.option("-o", "--output", help="Save the transcript to a file.")
    @click.option("--events", is_flag=True, help="Print every raw event to stderr (debugging).")
    @pass_app
    def transcribe(app: App, file, model, rate, pace, idle, session_json, output, events) -> None:
        """Stream a recording (WAV file, or raw PCM16 on stdin with `-`) and print the transcript."""
        app.require_login()
        if file == "-":
            pcm = rt.resample_pcm16(sys.stdin.buffer.read(), rate)
        else:
            pcm = rt.pcm16_from_wav(os.path.expanduser(file))
        if not pcm:
            raise IpxError("The audio is empty.")
        session = _session_arg(session_json, {"input_audio_format": "pcm16"})

        finals: List[str] = []
        partial: List[str] = []

        def on_event(ev: Dict[str, Any]) -> None:
            if events:
                ui.err.print(json.dumps(ev)[:400], highlight=False, markup=False)
            msg = rt.event_error(ev)
            if msg:
                ui.warn(msg)
            kind, text = rt.text_of(ev)
            if kind == "delta":
                partial.append(text)
            elif kind == "final" and text.strip():
                finals.append(text.strip())
                partial.clear()

        seconds = len(pcm) / 2 / rt.PCM_RATE
        with rt.open_session(app, model, session) as conn:
            try:
                started = time.monotonic()
                sent = 0.0
                for chunk in rt.pcm_chunks(pcm):
                    conn.send_text(rt.append_event(chunk))
                    sent += len(chunk) / 2 / rt.PCM_RATE
                    if pace:
                        wait = sent - (time.monotonic() - started)
                        if wait > 0:
                            time.sleep(wait)
                    for ev in rt.drain(conn):
                        on_event(ev)
                conn.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
                # Keep reading until the server has been quiet for `idle` seconds.
                while True:
                    got = rt.drain(conn, timeout=idle)
                    if not got:
                        break
                    for ev in got:
                        on_event(ev)
            except wsmod.WebSocketClosed as e:
                if e.code in rt.CLOSE_HINTS:
                    raise rt.closed_error(e)
                # A normal close after the transcript is complete is fine.

        if partial:
            finals.append("".join(partial).strip())
        text = "\n".join(f for f in finals if f)
        if output:
            with open(os.path.expanduser(output), "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            ui.ok(f"Saved transcript → [bold]{output}[/bold]")
        elif app.json:
            ui.emit_json({"text": text, "segments": finals, "audio_seconds": round(seconds, 2), "model": model})
        else:
            sys.stdout.write(text + "\n")
        if not app.json:
            ui.dim(f"  {seconds:.1f}s of audio streamed · exact charges: `ipx p2g ledger`")


def _send_text(conn: wsmod.WebSocket, text: str) -> None:
    conn.send_text(json.dumps({"type": "conversation.item.create",
                               "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
    conn.send_text(json.dumps({"type": "response.create"}))


def _read_response(conn: wsmod.WebSocket, audio: bytearray, totals: List[int], show_events: bool) -> None:
    """Print one response's text as it streams; collect spoken audio; stop at ``response.done``."""
    import base64

    streamed = False
    finals: List[str] = []
    while True:
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
            sys.stdout.write(text)
            sys.stdout.flush()
            streamed = True
        elif kind == "final" and text.strip():
            finals.append(text.strip())
        done = rt.usage_totals(ev)
        if done is not None or t == "response.done":
            if done:
                totals[0] += done[0]
                totals[1] += done[1]
            if streamed:
                sys.stdout.write("\n")
            elif finals:  # the model sent only completed text, no incremental deltas
                sys.stdout.write(finals[-1] + "\n")
            return
