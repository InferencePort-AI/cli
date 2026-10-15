"""ipx local - chat with and prompt models running on this machine."""

from __future__ import annotations

import sys
from typing import Any, Dict, Iterator, List, Optional

import click

from ipx import chat as chatmod
from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError
from ipx.local import Ollama


def _backend(oll: Ollama):
    def backend(messages: List[Dict[str, Any]], model: Optional[str]) -> Iterator[chatmod.Event]:
        total = 0
        for ev in oll.chat(model or "", messages):
            m = ev.get("message") or {}
            if m.get("thinking"):
                yield "think", m["thinking"]
            if m.get("content"):
                yield "text", m["content"]
            if ev.get("done"):
                total = int(ev.get("prompt_eval_count") or 0) + int(ev.get("eval_count") or 0)
                yield "usage", {"tokens": total}

    return backend


def _pick_model(app: App, oll: Ollama, model: Optional[str]) -> str:
    if model:
        return model
    default = app.cfg.get("default_local_model")
    installed = [m["name"] for m in oll.list()]
    if default and default in installed:
        return default
    if not installed:
        raise IpxError("No local models installed.", "Pull one first: `ipx model pull llama3.2`.")
    if len(installed) == 1:
        return installed[0]
    if ui.is_tty():
        return ui.choose("Which model?", [(n, n) for n in installed])
    raise IpxError("Several models are installed; specify one.", "Installed: " + ", ".join(installed))


@click.group("local", cls=IpxGroup, section="Local", short_help="Chat with local models",
             examples=["ipx local chat llama3.2", "ipx local text llama3.2 \"Explain monads in one sentence\""])
def group() -> None:
    """Run chat and one-off prompts against models on this machine. Free, private and offline."""


@group.command("chat")
@click.argument("model", required=False)
@click.option("--system", help="System prompt.")
@pass_app
def chat_(app: App, model: Optional[str], system: Optional[str]) -> None:
    """Interactive chat with a local model."""
    oll = Ollama(app.cfg.ollama_url)
    model = _pick_model(app, oll, model)
    chatmod.run_repl(title="Local chat", subtitle="running on this machine · private", backend=_backend(oll), model=model,
                     system=system, list_models=lambda: [m["name"] for m in oll.list()])


@group.command("text")
@click.argument("model")
@click.argument("prompt", required=False)
@click.option("--system", help="System prompt.")
@click.option("--stdin", "use_stdin", is_flag=True, help="Append standard input to the prompt.")
@pass_app
def text_(app: App, model: str, prompt: Optional[str], system: Optional[str], use_stdin: bool) -> None:
    """One-off generation. Pipe-friendly: the answer goes to stdout."""
    parts = []
    if use_stdin or prompt == "-":
        parts.append(sys.stdin.read().strip())
    if prompt and prompt != "-":
        parts.append(prompt)
    full = "\n\n".join(p for p in parts if p)
    if not full:
        raise IpxError("No prompt given.", "Pass it as an argument or pipe it in with `-`.")
    oll = Ollama(app.cfg.ollama_url)
    chatmod.one_shot(backend=_backend(oll), prompt=full, model=model, system=system, as_json=app.json)


COMMANDS = [group]
