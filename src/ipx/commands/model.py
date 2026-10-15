"""ipx model - manage models on this machine (the Ollama runtime used by the desktop app)."""

from __future__ import annotations

import copy as _copy
import os
import re
from typing import Any, Dict, Optional

import click
from rich.text import Text

from ipx import policy, ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.local import Ollama


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def client(app: App) -> Ollama:
    return Ollama(app.cfg.ollama_url)


@click.group("model", cls=IpxGroup, section="Local", short_help="Pull, list, inspect and remove local models",
             examples=["ipx model list", "ipx model pull llama3.2", "ipx model search coder", "ipx model rm llama3.2:1b",
                       "ipx model import gguf ./my-model.gguf --name mine", "ipx model import hf bartowski/Llama-3.2-3B-Instruct-GGUF"])
def group() -> None:
    """Manage local models on your machine.

    Uses the same Ollama runtime as the InferencePort desktop app, so models you pull here appear there (and vice
    versa). Organization controls on pulling and importing apply when an org context is active.
    """


group_alias_name = "models"


@group.command("list", short_help="Installed models")
@pass_app
def list_(app: App) -> None:
    """List models installed locally."""
    models = client(app).list()

    def render(_: Any) -> None:
        if not models:
            ui.info("No local models yet. Try `ipx model pull llama3.2`.")
            return
        rows = []
        for m in sorted(models, key=lambda x: x.get("name", "")):
            d = m.get("details") or {}
            rows.append((m.get("name"), ui.fmt_bytes(m.get("size", 0)), d.get("parameter_size"), d.get("quantization_level"),
                         d.get("family"), (m.get("modified_at") or "")[:10]))
        ui.console.print(ui.make_table(["Model", "Size", "Params", "Quant", "Family", "Modified"], rows,
                                       title=f"Local models ({len(rows)})", right=["Size"]))

    app.out(models, render)


@group.command("pull", examples=["ipx model pull llama3.2", "ipx model pull qwen2.5-coder:7b"])
@click.argument("names", nargs=-1, required=True)
@pass_app
def pull(app: App, names) -> None:
    """Download one or more models with a live progress bar."""
    oll = client(app)
    for name in names:
        policy.assert_pull(app, name)
        _pull_one(app, oll, name)


def _pull_one(app: App, oll: Ollama, name: str) -> None:
    last: Dict[str, Any] = {}
    if app.json:
        for ev in oll.pull(name):
            last = ev
        ui.emit_json({"model": name, "status": last.get("status")})
        return
    tasks: Dict[str, int] = {}
    with ui.download_progress() as prog:
        status_task = prog.add_task(f"Pulling {name}", total=None)
        for ev in oll.pull(name):
            last = ev
            st = str(ev.get("status", ""))
            digest = ev.get("digest")
            if digest and ev.get("total"):
                tid = tasks.get(digest)
                if tid is None:
                    tid = prog.add_task(f"layer {digest.split(':')[-1][:10]}", total=ev["total"])
                    tasks[digest] = tid
                prog.update(tid, completed=ev.get("completed", 0), total=ev["total"])
            else:
                prog.update(status_task, description=f"{name} · {st}")
        prog.update(status_task, total=1, completed=1, description=f"{name} · done")
    if last.get("status") == "success" or last:
        ui.ok(f"Pulled [bold]{name}[/bold]")


@group.command("rm", short_help="Remove models")
@click.argument("names", nargs=-1, required=True)
@pass_app
def rm(app: App, names) -> None:
    """Delete local models and free the disk space."""
    oll = client(app)
    if not ui.confirm(f"Delete {', '.join(names)}?"):
        return
    for n in names:
        oll.delete(n)
        ui.ok(f"Removed [bold]{n}[/bold]")


@group.command("show", short_help="Details about a model")
@click.argument("name")
@pass_app
def show_(app: App, name: str) -> None:
    """Show a model's parameters, template and capabilities."""
    d = client(app).show(name)

    def render(d: Dict[str, Any]) -> None:
        det = d.get("details") or {}
        info = d.get("model_info") or {}
        rows = [("Family", det.get("family")), ("Parameters", det.get("parameter_size")), ("Quantization", det.get("quantization_level")),
                ("Format", det.get("format")), ("Capabilities", ", ".join(d.get("capabilities") or []) or None),
                ("Context length", next((v for k, v in info.items() if k.endswith("context_length")), None))]
        ui.console.print(ui.kv_panel(name, rows))
        if d.get("system"):
            ui.console.print(Text("System prompt", style="key"))
            ui.console.print(Text(d["system"].strip(), style="muted"))
        if d.get("modelfile") and not app.json:
            ui.dim("Use `--json` for the full Modelfile and template.")

    app.out(d, render)


@group.command("ps", short_help="Models loaded in memory")
@pass_app
def ps(app: App) -> None:
    """Show which models are currently loaded."""
    run = client(app).running()
    rows = [(m.get("name"), ui.fmt_bytes(m.get("size", 0)), ui.fmt_bytes(m.get("size_vram", 0)), (m.get("expires_at") or "")[:19].replace("T", " ")) for m in run]
    app.out(run, lambda _: ui.console.print(ui.make_table(["Model", "Size", "VRAM", "Unloads at"], rows, title="Running models", right=["Size", "VRAM"])) if rows else ui.info("No models are loaded right now."))


@group.command("copy", short_help="Copy a model to a new name")
@click.argument("source")
@click.argument("destination")
@pass_app
def copy(app: App, source: str, destination: str) -> None:
    """Duplicate a model under another name."""
    client(app).copy(source, destination)
    ui.ok(f"Copied [bold]{source}[/bold] → [bold]{destination}[/bold]")


@group.command("create", examples=["ipx model create my-assistant -f Modelfile"])
@click.argument("name")
@click.option("-f", "--file", "modelfile", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to a Modelfile.")
@pass_app
def create(app: App, name: str, modelfile: str) -> None:
    """Create a model from a Modelfile."""
    policy.assert_import(app)
    content = open(modelfile, "r", encoding="utf-8").read()
    _create(app, name, modelfile=content)


def _create(app: App, name: str, **body: Any) -> None:
    last = ""
    with ui.status("Creating model…") as st:
        for ev in client(app).create(name, **body):
            last = str(ev.get("status", ""))
            if st:
                st.update(f"[accent]{last}[/accent]")
    ui.ok(f"Created [bold]{name}[/bold] [muted]({last})[/muted]")


@group.command("search", examples=["ipx model search vision", "ipx model search llama --limit 5"])
@click.argument("query", required=False)
@click.option("--limit", type=int, default=15, show_default=True)
@pass_app
def search(app: App, query: Optional[str], limit: int) -> None:
    """Search the public model catalog (no sign-in needed)."""
    with ui.status("Searching the catalog…"):
        cat = app.api.get("/models", auth=False, org=False) or []
    q = (query or "").lower()
    hits = [m for m in cat if isinstance(m, dict) and (not q or q in str(m.get("name", "")).lower() or q in str(m.get("description", "")).lower()
                                                        or any(q in str(t).lower() for t in m.get("tags") or []))]
    hits = hits[:limit]

    def render(_: Any) -> None:
        rows = [(m.get("name"), ", ".join(m.get("sizes") or []), m.get("pulls"), ", ".join(t for t in m.get("tags") or [] if not re.fullmatch(r"[\d.]+[bm]", str(t).lower())), _clip(m.get("description") or "", 64)) for m in hits]
        ui.console.print(ui.make_table(["Model", "Sizes", "Pulls", "Tags", "About"], rows, title=f"Catalog results ({len(hits)})"))
        ui.dim("Install with `ipx model pull <name>`")

    app.out(hits, render)


@group.group("import", cls=IpxGroup, short_help="Import GGUF files or Hugging Face models")
def import_() -> None:
    """Bring your own models: a local GGUF file or a Hugging Face repository."""


@import_.command("gguf", examples=["ipx model import gguf ./model.Q4_K_M.gguf --name my-model --system \"You are concise.\""])
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", required=True, help="Name for the imported model.")
@click.option("--system", help="Optional system prompt baked into the model.")
@click.option("--template", help="Optional chat template.")
@pass_app
def import_gguf(app: App, path: str, name: str, system: Optional[str], template: Optional[str]) -> None:
    """Import a local .gguf file as a model."""
    policy.assert_import(app)
    lines = [f'FROM {os.path.abspath(path)}']
    if template:
        lines.append(f'TEMPLATE """{template}"""')
    if system:
        lines.append(f'SYSTEM """{system}"""')
    _create(app, name, modelfile="\n".join(lines))


@import_.command("hf", examples=["ipx model import hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M"])
@click.argument("repo")
@pass_app
def import_hf(app: App, repo: str) -> None:
    """Pull a GGUF model straight from Hugging Face (user/repo[:quant])."""
    policy.assert_import(app)
    ref = repo if repo.startswith("hf.co/") else f"hf.co/{repo}"
    _pull_one(app, client(app), ref)


_alias = _copy.copy(group)
_alias.name = "models"
_alias.hidden = True

COMMANDS = [group, _alias]
