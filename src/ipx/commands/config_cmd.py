"""ipx config - read and write persisted preferences."""

from __future__ import annotations

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.config import DEFAULT_BASE_URL, DEFAULT_OLLAMA_URL, config_dir
from ipx.errors import IpxError

KNOWN = {
    "base_url": f"API endpoint (default {DEFAULT_BASE_URL})",
    "ollama_url": f"Local Ollama endpoint (default {DEFAULT_OLLAMA_URL})",
    "org": "Active organization slug (prefer `ipx auth context`)",
    "default_gen_model": "Default model for `ipx gen chat/text`",
    "default_p2g_model": "Default model for `ipx p2g chat/text`",
    "default_local_model": "Default model for `ipx local chat/text`",
    "output_dir": "Folder generated media is saved to (default: current directory)",
}


@click.group("config", cls=IpxGroup, section="Tools", short_help="Read and change preferences")
def group() -> None:
    """Persisted preferences, stored in the ipx config folder (see `ipx config path`)."""


@group.command("list")
@pass_app
def list_(app: App) -> None:
    """Show every setting with its current value."""
    data = {k: app.cfg.get(k) for k in KNOWN}
    data["base_url"] = app.cfg.base_url
    data["ollama_url"] = app.cfg.ollama_url
    data["org"] = app.cfg.org

    def render(d):
        rows = [(k, d[k] if d[k] is not None else ui.Text("not set", style="muted")) for k in KNOWN]
        ui.console.print(ui.kv_panel("Settings", rows, subtitle=str(app.cfg.path)))

    app.out(data, render)


@group.command("get")
@click.argument("key")
@pass_app
def get(app: App, key: str) -> None:
    """Print one setting's value."""
    val = {"base_url": app.cfg.base_url, "ollama_url": app.cfg.ollama_url, "org": app.cfg.org}.get(key, app.cfg.get(key))
    if val is None:
        raise IpxError(f"“{key}” is not set.")
    click.echo(val)


@group.command("set")
@click.argument("key")
@click.argument("value")
@pass_app
def set_(app: App, key: str, value: str) -> None:
    """Set a preference. Known keys: base_url, ollama_url, default_gen_model, default_p2g_model, default_local_model, output_dir."""
    if key not in KNOWN:
        raise IpxError(f"Unknown setting “{key}”.", "Known: " + ", ".join(KNOWN))
    if key in ("base_url", "ollama_url") and not value.startswith(("http://", "https://")):
        raise IpxError("URL must start with http:// or https://")
    app.cfg.set(key, value.rstrip("/") if key.endswith("_url") else value)
    ui.ok(f"[key]{key}[/key] = {value}")


@group.command("unset")
@click.argument("key")
@pass_app
def unset(app: App, key: str) -> None:
    """Remove a preference (revert to its default)."""
    app.cfg.set(key, None)
    ui.ok(f"Unset [key]{key}[/key]")


@group.command("path")
def path() -> None:
    """Print the config directory."""
    click.echo(str(config_dir()))


COMMANDS = [group]
