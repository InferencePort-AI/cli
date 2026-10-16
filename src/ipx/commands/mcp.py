"""ipx mcp - InferencePort's hosted MCP servers. Today: the Council of LLMs (`ipx mcp council`)."""

from __future__ import annotations

import json
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

import click
from rich.markup import escape

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError
from ipx.mcp import McpClient, council_url


def council_client(app: App) -> McpClient:
    app.require_login()
    with ui.status("Connecting to the council…") if not app.json else _null():
        return McpClient(app, council_url(app.api.base_url)).connect()


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> None:
        return None


def _load(tool_result: Any) -> Any:
    return tool_result if not isinstance(tool_result, str) else {"text": tool_result}


@click.group("mcp", cls=IpxGroup, section="Cloud", short_help="Hosted MCP servers (Council of LLMs)",
             examples=["ipx mcp council tiers", "ipx mcp council reason \"Is P = NP provable with current methods?\" --tier basic",
                       "ipx -O acme mcp council reason -f question.md --rounds 2", "ipx mcp council connect claude-code"])
def group() -> None:
    """Use InferencePort's hosted Model Context Protocol servers from the terminal."""


@group.group("council", cls=IpxGroup, short_help="Council of LLMs: multi-model reasoning")
def council() -> None:
    """Send a problem to a council of models that answer, cross-review each other, and have judges synthesize a result.

    Each model call is billed at normal per-token rates plus a flat council surcharge, to your wallet or - with an
    organization context (`ipx auth context <org>` or --org) - the organization's. Works with a signed-in session
    or an API key.
    """


@council.command("tiers", short_help="Council tiers and their cost")
@pass_app
def tiers(app: App) -> None:
    """List the council tiers with model counts, judges and the flat surcharge in credits."""
    c = council_client(app)
    d = c.call_json("council_list_tiers")

    def render(x: Dict[str, Any]) -> None:
        ui.console.print(ui.make_table(
            ["Tier", "Name", "Models", "Judges", "Surcharge", "Description"],
            [(k, v.get("name"), v.get("model_count"), v.get("judge_count"), f"{ui.fmt_credits(v.get('surcharge_credits'))} cr", v.get("description")) for k, v in x.items()],
            title="Council tiers", right=["Models", "Judges", "Surcharge"]))

    app.out(d, render)


@council.command("models", short_help="Models in a tier")
@click.option("-t", "--tier", default="basic", show_default=True, help="Tier name (see `ipx mcp council tiers`).")
@pass_app
def models(app: App, tier: str) -> None:
    """The council members, judges and fallbacks of a tier."""
    d = council_client(app).call_json("council_list_models", {"tier": tier})

    def render(x: Dict[str, Any]) -> None:
        ui.console.print(ui.make_table(["Role", "Model"], [("member", m) for m in x.get("models") or []] + [("judge", m) for m in x.get("judge_models") or []]
                                       + [("fallback", m) for m in x.get("fallback_pool") or []], title=f"{x.get('name') or tier} council"))

    app.out(d, render)


@council.command("reason", short_help="Ask the council", examples=[
    "ipx mcp council reason \"Design a rate limiter for 10k rps\"",
    "ipx mcp council reason -f spec.md --tier pro --rounds 2",
    "cat bug.txt | ipx mcp council reason --stdin \"What is the root cause?\"",
    "ipx mcp council reason \"…\" --model openai/gpt-4o --model anthropic/claude-sonnet-4 --judge openai/gpt-4o"])
@click.argument("problem", required=False)
@click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False), help="Include a text file's contents (repeatable).")
@click.option("--stdin", "use_stdin", is_flag=True, help="Append standard input to the problem.")
@click.option("-t", "--tier", default="basic", show_default=True, help="Council tier; higher tiers use more and better models.")
@click.option("-r", "--rounds", type=click.IntRange(1, 3), default=1, show_default=True, help="Review rounds (models critique each other).")
@click.option("--model", "custom_models", multiple=True, help="Override the council members (repeatable).")
@click.option("--judge", "custom_judges", multiple=True, help="Override the judges (repeatable).")
@click.option("--log", "show_log", is_flag=True, help="Also print the per-round iteration log.")
@pass_app
def reason(app: App, problem: Optional[str], files: Tuple[str, ...], use_stdin: bool, tier: str, rounds: int,
           custom_models: Tuple[str, ...], custom_judges: Tuple[str, ...], show_log: bool) -> None:
    """Run PROBLEM through the council and print the synthesized answer (stdout) with a usage summary (stderr)."""
    app.require_login()
    parts: List[str] = [problem] if problem and problem != "-" else []
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            parts.append(f"--- {f} ---\n{fh.read()}")
    if use_stdin or problem == "-":
        parts.append(sys.stdin.read().strip())
    text = "\n\n".join(p for p in parts if p).strip()
    if not text:
        raise IpxError("No problem given.", "Pass it as an argument, with -f FILE, or pipe it in with --stdin.")
    args: Dict[str, Any] = {"problem": text, "tier": tier, "iteration_rounds": rounds}
    if custom_models:
        args["custom_models"] = list(custom_models)
    if custom_judges:
        args["custom_judges"] = list(custom_judges)
    c = council_client(app)
    if not app.json:
        ui.err.print(f"[muted]Convening the {tier} council · billed to {'org ' + app.org if app.org else 'your wallet'} · this can take a few minutes…[/muted]")
    d = c.call_json("council_reason", args)
    if not isinstance(d, dict):
        d = {"answer": d}
    if app.json:
        if not show_log:
            d.pop("iteration_log", None)
        ui.emit_json(d)
        return
    sys.stdout.write(str(d.get("answer", "")).rstrip() + "\n")
    info, bill, use = d.get("council") or {}, d.get("billing") or {}, d.get("usage") or {}
    if not ui.STATE["quiet"]:
        failed = info.get("failed_models") or []
        line = [f"{len(info.get('models_used') or [])} models", f"{info.get('iterations_used', '?')} round(s)"]
        if bill.get("surcharge_credits"):
            line.append(f"surcharge {ui.fmt_credits(bill['surcharge_credits'])} cr" + ("" if bill.get("charged") else " (not charged)"))
        if isinstance(use, dict) and use.get("total_tokens"):
            line.append(f"{ui.fmt_num(use['total_tokens'])} tokens")
        ui.err.print("[muted]" + " · ".join(line) + ("" if not failed else f" · {len(failed)} member(s) failed") + "[/muted]")
    if show_log and d.get("iteration_log"):
        ui.err.print_json(json.dumps(d["iteration_log"], default=str))


@council.command("tools", short_help="Tools the council server exposes")
@pass_app
def tools(app: App) -> None:
    """List the MCP tools offered by the council server."""
    ts = council_client(app).list_tools()
    app.out(ts, lambda rows: ui.console.print(ui.make_table(["Tool", "Description"], [(t.get("name"), (t.get("description") or "").strip().split("\n")[0]) for t in rows], title="Council MCP tools")))


@council.command("test", short_help="Check the connection")
@pass_app
def test(app: App) -> None:
    """Run the MCP handshake with your sign-in, like an MCP client would."""
    c = council_client(app)
    info = {"url": c.url, "server": c.server_info.get("name"), "version": c.server_info.get("version"), "org": app.org}
    app.out(info, lambda x: ui.ok(f"Connected to [bold]{x['server']}[/bold] at {x['url']}" + (f" as org {x['org']}" if x["org"] else "")))


CLIENTS = ["claude-code", "claude-desktop", "cursor", "vscode", "json"]


@council.command("connect", short_help="Config snippet for an MCP client")
@click.argument("client", type=click.Choice(CLIENTS), default="claude-code")
@pass_app
def connect(app: App, client: str) -> None:
    """Print the setup for CLIENT to use the council server. Uses OAuth, or paste an API key (ipa_live_…) as the token."""
    url = council_url(app.api.base_url)
    headers = {"X-InferencePort-Org": app.org} if app.org else {}
    if client == "claude-code":
        hdr = "".join(f" --header {shlex.quote(f'{k}: {v}')}" for k, v in headers.items())
        text = f"claude mcp add --transport http council {shlex.quote(url)}{hdr}"
    elif client == "claude-desktop":
        text = json.dumps({"mcpServers": {"council": {"command": "npx", "args": ["-y", "mcp-remote", url] + [x for k, v in headers.items() for x in ("--header", f"{k}:{v}")]}}}, indent=2)
    else:
        server: Dict[str, Any] = {"type": "http", "url": url} if client == "vscode" else {"url": url}
        if headers:
            server["headers"] = headers
        text = json.dumps({"servers" if client == "vscode" else "mcpServers": {"council": server}}, indent=2)
    app.out({"client": client, "url": url, "config": text}, lambda _: (ui.err.print(f"[muted]{client} · billed to {'org ' + escape(app.org) if app.org else 'your personal wallet'}[/muted]", markup=True, highlight=False) if not ui.STATE["quiet"] else None, sys.stdout.write(text + "\n")))


COMMANDS = [group]
