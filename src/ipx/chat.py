"""Streaming chat: the interactive REPL and one-shot text generation, backend-agnostic.

A backend is any callable ``stream(messages, model) -> Iterator[event]`` where an event is one of
  ("text", str)        a content delta
  ("think", str)       a reasoning delta (shown dimmed)
  ("meta", dict)       e.g. {"model_name": "..."} from the Lightning router
  ("usage", dict)      {"credits": float, "tokens": int, ...} at the end of the stream
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from ipx import ui
from ipx.errors import IpxError

Event = Tuple[str, Any]
Backend = Callable[[List[Dict[str, Any]], Optional[str]], Iterator[Event]]

HELP = [
    ("/help", "show this help"),
    ("/exit, /quit", "leave the chat (or press Ctrl+D)"),
    ("/clear", "forget the conversation so far"),
    ("/system <text>", "set the system prompt (clears history)"),
    ("/model [name]", "show or switch the model"),
    ("/models", "list models you can use"),
    ("/retry", "regenerate the last answer"),
    ("/save [file]", "save the transcript as Markdown"),
    ("/usage", "tokens and credits used in this chat"),
    ('"""', "start/end a multi-line message (or end a line with \\)"),
]


class Totals:
    def __init__(self) -> None:
        self.credits = 0.0
        self.tokens = 0
        self.turns = 0

    def add(self, u: Dict[str, Any]) -> None:
        self.credits += float(u.get("credits") or 0)
        self.tokens += int(u.get("tokens") or 0)


def _stream_turn(backend: Backend, messages: List[Dict[str, Any]], model: Optional[str], totals: Totals,
                 live_render: bool = True) -> Tuple[str, Dict[str, Any]]:
    """Run one assistant turn, rendering Markdown live. Returns (text, info)."""
    text, think = "", ""
    info: Dict[str, Any] = {}
    start = time.time()
    first = None

    def view() -> Group:
        parts: List[Any] = []
        if think:
            tail = think.strip().splitlines()[-3:]
            parts.append(Text("💭 " + " ".join(tail)[:300], style="dim italic"))
        if text:
            parts.append(Markdown(text, code_theme="monokai"))
        elif not think:
            parts.append(Text("●●●", style=ui.BRAND))
        return Group(*parts)

    stream = backend(messages, model)
    try:
        with Live(view(), console=ui.console, refresh_per_second=14, transient=False, vertical_overflow="visible") as live:
            try:
                for kind, payload in stream:
                    if kind == "text":
                        first = first or time.time()
                        text += payload
                    elif kind == "think":
                        think += payload
                    elif kind == "meta":
                        info.update(payload)
                    elif kind == "usage":
                        info.update(payload)
                        totals.add(payload)
                    live.update(view())
            except KeyboardInterrupt:
                info["interrupted"] = True
            live.update(Group(Markdown(text, code_theme="monokai")) if text else Text(""))
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    info["elapsed"] = time.time() - start
    return text, info


def _footer(info: Dict[str, Any], model: Optional[str], to=None) -> None:
    out = to or ui.console
    bits = []
    used = info.get("model_name") or model
    if used:
        bits.append(str(used))
    if info.get("tokens"):
        bits.append(f"{info['tokens']} tok")
    if info.get("credits"):
        bits.append(f"{ui.fmt_credits(info['credits'])} credits")
    if info.get("elapsed"):
        bits.append(f"{info['elapsed']:.1f}s")
    if info.get("interrupted"):
        bits.append("stopped")
    if bits:
        out.print(Text("  " + " · ".join(bits), style="dim"))


def _read_message() -> Optional[str]:
    """Read a (possibly multi-line) message. Returns None on EOF."""
    try:
        line = ui.console.input("[user]you[/user] [muted]›[/muted] ")
    except EOFError:
        return None
    if line.strip() == '"""':
        lines = []
        while True:
            try:
                nxt = ui.console.input("[muted]… [/muted]")
            except EOFError:
                break
            if nxt.strip() == '"""':
                break
            lines.append(nxt)
        return "\n".join(lines)
    while line.endswith("\\"):
        try:
            line = line[:-1] + "\n" + ui.console.input("[muted]… [/muted]")
        except EOFError:
            break
    return line


def run_repl(*, title: str, subtitle: str, backend: Backend, model: Optional[str], system: Optional[str] = None,
             list_models: Optional[Callable[[], List[str]]] = None, footer_note: Optional[str] = None) -> None:
    if not ui.is_tty():
        raise IpxError("Interactive chat needs a terminal.", "For scripts use the one-shot `text` command, e.g. `echo hi | ipx gen text`.")
    totals = Totals()
    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})

    ui.console.print()
    ui.console.print(Panel(
        Text.assemble((title, "bold white"), ("  " + subtitle, "muted"), "\n",
                      ("model ", "muted"), (model or "auto", f"bold {ui.ACCENT}"),
                      ("   ·   /help for commands   ·   Ctrl+D to exit", "muted")),
        border_style=ui.DIM, padding=(0, 2)))
    if footer_note:
        ui.dim(footer_note)
    ui.console.print()

    while True:
        try:
            msg = _read_message()
        except KeyboardInterrupt:
            ui.console.print()
            break
        if msg is None:
            ui.console.print()
            break
        msg = msg.strip()
        if not msg:
            continue
        if msg.startswith("/"):
            cmd, _, arg = msg.partition(" ")
            cmd, arg = cmd.lower(), arg.strip()
            if cmd in ("/exit", "/quit", "/q"):
                break
            if cmd == "/help":
                for k, v in HELP:
                    ui.console.print(f"  [key]{k:<16}[/key] [muted]{v}[/muted]")
            elif cmd == "/clear":
                messages = [m for m in messages if m["role"] == "system"]
                ui.ok("Conversation cleared.")
            elif cmd == "/system":
                messages = [{"role": "system", "content": arg}] if arg else []
                ui.ok("System prompt updated; history cleared." if arg else "System prompt removed.")
            elif cmd == "/model":
                if arg:
                    model = arg
                    ui.ok(f"Model set to [bold]{model}[/bold]")
                else:
                    ui.console.print(f"Model: [bold]{model or 'auto'}[/bold]")
            elif cmd == "/models":
                if list_models:
                    try:
                        for m in list_models():
                            ui.console.print(f"  [accent]•[/accent] {m}")
                    except IpxError as e:
                        ui.render_error(e)
                else:
                    ui.dim("Model listing is not available here.")
            elif cmd == "/usage":
                ui.console.print(f"  {totals.turns} turns · {totals.tokens} tokens · {ui.fmt_credits(totals.credits)} credits")
            elif cmd == "/save":
                path = arg or f"chat-{datetime.now():%Y%m%d-%H%M%S}.md"
                with open(path, "w", encoding="utf-8") as fh:
                    for m in messages:
                        who = {"user": "You", "assistant": "Assistant", "system": "System"}.get(m["role"], m["role"])
                        fh.write(f"## {who}\n\n{m['content']}\n\n")
                ui.ok(f"Saved transcript to [bold]{path}[/bold]")
            elif cmd == "/retry":
                while messages and messages[-1]["role"] == "assistant":
                    messages.pop()
                if not messages or messages[-1]["role"] != "user":
                    ui.warn("Nothing to retry yet.")
                else:
                    _answer(backend, messages, model, totals)
            else:
                ui.warn(f"Unknown command {cmd}. Type /help.")
            continue
        messages.append({"role": "user", "content": msg})
        if not _answer(backend, messages, model, totals):
            messages.pop()


def _answer(backend: Backend, messages: List[Dict[str, Any]], model: Optional[str], totals: Totals) -> bool:
    ui.console.print(Text("assistant", style="bot"))
    try:
        text, info = _stream_turn(backend, messages, model, totals)
    except IpxError as e:
        ui.render_error(e)
        return False
    if text or info.get("interrupted"):
        messages.append({"role": "assistant", "content": text})
        totals.turns += 1
    _footer(info, model)
    ui.console.print()
    return True


def one_shot(*, backend: Backend, prompt: str, model: Optional[str], system: Optional[str], as_json: bool,
             stream_out: bool = True) -> None:
    """Generate once. Streams plain text to stdout (pipe friendly); footer details go to stderr."""
    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    out, info, totals = [], {}, Totals()
    start = time.time()
    for kind, payload in backend(messages, model):
        if kind == "text":
            out.append(payload)
            if stream_out and not as_json:
                sys.stdout.write(payload)
                sys.stdout.flush()
        elif kind == "meta":
            info.update(payload)
        elif kind == "usage":
            info.update(payload)
            totals.add(payload)
    text = "".join(out)
    if as_json:
        ui.emit_json({"text": text, "model": info.get("model_name") or model, "usage": info})
        return
    if stream_out:
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    else:
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))
    if not ui.STATE["quiet"] and sys.stderr.isatty():
        info["elapsed"] = time.time() - start
        _footer(info, model, to=ui.err)


def parse_openai_sse(stream) -> Iterator[Event]:
    """Translate an OpenAI-style (plus InferencePort extras) SSE stream into chat events."""
    for event, data in stream.iter_sse():
        if event == "payg-usage":
            try:
                u = json.loads(data)
            except ValueError:
                continue
            yield "usage", {
                "credits": u.get("payg_credits_charged"),
                "tokens": u.get("payg_total_tokens"),
                "input_tokens": u.get("payg_input_tokens"),
                "output_tokens": u.get("payg_output_tokens"),
            }
            continue
        if data.strip() == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("error"):
            err = obj["error"]
            raise IpxError(err if isinstance(err, str) else err.get("message", "The model returned an error."))
        rm = obj.get("router_metadata")
        if isinstance(rm, dict):
            yield "meta", {"model_name": rm.get("model_name")}
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens"):
            yield "usage", {"tokens": usage.get("total_tokens"), "credits": usage.get("payg_credits_charged")}
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("reasoning_content"):
                yield "think", delta["reasoning_content"]
            if delta.get("content"):
                yield "text", delta["content"]
