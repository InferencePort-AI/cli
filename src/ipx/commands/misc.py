"""Top-level utility commands: status, doctor, api, rewards, completion."""

from __future__ import annotations

import json
import socket
import sys
from typing import Any, Dict, List, Optional

import click
from rich.text import Text

from ipx import __version__, ui
from ipx.app import App, pass_app
from ipx.clickx import IpxCommand, IpxGroup
from ipx.config import DEFAULT_SERVER_PORT, desktop_data_dir
from ipx.errors import IpxError

_STATE_STYLE = {"operational": "ok", "up": "ok", "ok": "ok", "degraded": "warn", "partial": "warn", "down": "bad", "outage": "bad"}


def _dot(state: Any) -> Text:
    s = str(state).lower()
    style = _STATE_STYLE.get(s, "muted")
    return Text(f"● {state}", style=style)


@click.command("status", cls=IpxCommand, section="Tools", short_help="InferencePort service health")
@pass_app
def status(app: App) -> None:
    """Show the health of the Lightning API and each generation service (no sign-in needed)."""
    data = app.api.get("/status", auth=False, org=False) or {}

    def render(d: Dict[str, Any]) -> None:
        rows: List[Any] = [("Overall", _dot(d.get("state", "unknown")))]
        for name, val in (d.get("services") or {}).items():
            state = val.get("state") or val.get("status") if isinstance(val, dict) else val
            rows.append((name, _dot(state)))
        ui.console.print(ui.kv_panel("InferencePort status", rows, subtitle=app.cfg.base_url))
        for n in d.get("notifications") or []:
            ui.warn(n if isinstance(n, str) else n.get("message") or json.dumps(n))

    app.out(data, render)


@click.command("doctor", cls=IpxCommand, section="Tools", short_help="Diagnose your setup")
@pass_app
def doctor(app: App) -> None:
    """Check connectivity, sign-in, organization context, local Ollama and the host-server port."""
    from ipx.local import ollama_version

    checks: List[Dict[str, Any]] = []

    def add(name: str, ok_: Optional[bool], detail: str) -> None:
        checks.append({"check": name, "ok": ok_, "detail": detail})

    add("ipx version", True, __version__)
    try:
        st = app.api.get("/status", auth=False, org=False) or {}
        add("API reachable", True, f"{app.cfg.base_url} · {st.get('state', 'ok')}")
    except IpxError as e:
        add("API reachable", False, e.message)
    if app.creds.signed_in:
        try:
            sub = app.api.get("/subscription", org=False) or {}
            add("Signed in", True, f"{sub.get('email') or app.creds.email} · {sub.get('plan_name', 'plan unknown')}")
        except IpxError as e:
            add("Signed in", False, e.message)
    else:
        add("Signed in", None, "not signed in — run `ipx auth login`")
    add("Organization context", None if not app.cfg.org else True, app.cfg.org or "personal account")
    v = ollama_version(app.cfg.ollama_url)
    add("Local Ollama", bool(v), f"{app.cfg.ollama_url} · v{v}" if v else "not running — install from https://ollama.com and run `ollama serve`")
    with socket.socket() as s:
        s.settimeout(0.3)
        busy = s.connect_ex(("127.0.0.1", DEFAULT_SERVER_PORT)) == 0
    add("Host server port", None, f"{DEFAULT_SERVER_PORT} in use (server may be running)" if busy else f"{DEFAULT_SERVER_PORT} is free")
    dd = desktop_data_dir()
    add("Desktop app data", True if dd.exists() else None, str(dd) if dd.exists() else "desktop app not found (imports will create the folder)")

    def render(rows: List[Dict[str, Any]]) -> None:
        for r in rows:
            mark = {True: "[ok]✔[/ok]", False: "[bad]✖[/bad]", None: "[warn]•[/warn]"}[r["ok"]]
            ui.console.print(f" {mark} [bold]{r['check']:<22}[/bold] [muted]{r['detail']}[/muted]")

    app.out(checks, render)
    if any(r["ok"] is False for r in checks):
        sys.exit(1)


@click.command("api", cls=IpxCommand, section="Tools", short_help="Make a raw authenticated API request",
               examples=["ipx api GET /v1/me", "ipx api POST /v1/chat/completions -d '{\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"])
@click.argument("method")
@click.argument("path")
@click.option("-d", "--data", help="JSON request body (or @file.json).")
@click.option("-q", "--query", "queries", multiple=True, help="Query parameter key=value (repeatable).")
@click.option("--no-auth", is_flag=True, help="Do not send credentials.")
@pass_app
def api(app: App, method: str, path: str, data: Optional[str], queries, no_auth: bool) -> None:
    """Call any endpoint with your credentials and print the response. Escape hatch for scripting."""
    body: Any = None
    if data:
        raw = open(data[1:], "r", encoding="utf-8").read() if data.startswith("@") else data
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise IpxError(f"--data is not valid JSON: {e}")
    params = dict(q.split("=", 1) for q in queries if "=" in q)
    r = app.api.request(method.upper(), path, json=body, params=params, auth=not no_auth)
    if r.content_type.startswith(("application/json",)) or r.body[:1] in (b"{", b"["):
        try:
            ui.console.print_json(r.text) if ui.console.is_terminal and not app.json else click.echo(r.text)
            return
        except Exception:
            pass
    sys.stdout.buffer.write(r.body)


@click.group("rewards", cls=IpxGroup, section="Tools", short_help="Rewards and discounts")
def rewards() -> None:
    """Earn and claim InferencePort rewards."""


@rewards.command("list")
@pass_app
def rewards_list(app: App) -> None:
    """List available rewards."""
    data = app.api.get("/v1/rewards", org=False) or {}
    items = data.get("rewards") or data.get("items") or (data if isinstance(data, list) else [])

    def render(_: Any) -> None:
        rows = [(r.get("id"), r.get("title") or r.get("name"), r.get("description") or "") for r in items if isinstance(r, dict)]
        ui.console.print(ui.make_table(["ID", "Reward", "Description"], rows, title="Rewards"))

    app.out(data, render)


@rewards.command("summary")
@pass_app
def rewards_summary(app: App) -> None:
    """Your reward progress at a glance."""
    data = app.api.get("/v1/rewards/summary", org=False) or {}
    app.out(data, lambda d: ui.console.print(ui.kv_panel("Rewards", [(k, v) for k, v in d.items() if not isinstance(v, (dict, list))])))


@rewards.command("claim")
@click.argument("reward_id")
@pass_app
def rewards_claim(app: App, reward_id: str) -> None:
    """Claim an earned reward."""
    data = app.api.post(f"/v1/rewards/claim/{reward_id}", {}, org=False)
    app.out(data, lambda _: ui.ok(f"Claimed reward [bold]{reward_id}[/bold]"))


@click.command("completion", cls=IpxCommand, section="Tools", short_help="Print a shell completion script",
               examples=["ipx completion bash >> ~/.bashrc", "ipx completion zsh > ~/.zfunc/_ipx", "ipx completion fish > ~/.config/fish/completions/ipx.fish"])
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Print a completion script for your shell."""
    from click.shell_completion import get_completion_class

    from ipx.cli import root

    cls = get_completion_class(shell)
    click.echo(cls(root, {}, "ipx", "_IPX_COMPLETE").source())


COMMANDS = [status, doctor, api, rewards, completion]
