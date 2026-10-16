"""ipx agent - InferencePort managed agents: a backing model + system prompt (+ MCP servers) callable as `agent/<user>/<id>`."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

import click
from rich.text import Text

from ipx import chat as chatmod
from ipx import cloud, ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.cloud import P2G
from ipx.errors import IpxError

ROLES = ["user", "editor", "inviter", "administrator"]


# --------------------------------------------------------------------------- helpers


def rows_of(d: Any, key: str = "data") -> List[Dict[str, Any]]:
    rows = d.get(key) if isinstance(d, dict) else d
    return [x for x in (rows or []) if isinstance(x, dict)]


def split_ref(app: App, ref: str) -> Tuple[str, str]:
    """`user/agent`, `agent/user/agent` or a bare agent id (yours) -> (username, agent_id)."""
    parts = [p for p in ref.strip().split("/") if p]
    if parts and parts[0] == "agent":
        parts = parts[1:]
    if len(parts) == 2:
        return parts[0].lower(), parts[1].lower()
    if len(parts) == 1:
        d = app.api.get("/v1/agents/profile", org=False) or {}
        if not d.get("username"):
            raise IpxError("You have no agent username yet.", "Set one with `ipx agent profile set <username>`, or use the full user/agent reference.")
        return d["username"], parts[0].lower()
    raise IpxError(f"'{ref}' is not an agent reference.", "Use <username>/<agent-id>, e.g. `acme-bot/support`.")


def path(app: App, ref: str, suffix: str = "") -> str:
    user, aid = split_ref(app, ref)
    return f"/v1/agents/{user}/{aid}{suffix}"


def api(app: App, method: str, p: str, body: Any = None, **kw: Any) -> Any:
    kw.setdefault("org", False)  # agents belong to people; only chat bills an organization
    r = app.api.request(method, p, json=body, **kw)
    return r.json() if r.body else {}


def read_text(value: Optional[str], file: Optional[str]) -> Optional[str]:
    if file:
        return sys.stdin.read() if file == "-" else open(file, "r", encoding="utf-8").read()
    return value


def parse_mcp(pairs: Tuple[str, ...], files: Tuple[str, ...]) -> Optional[List[Dict[str, Any]]]:
    out: List[Dict[str, Any]] = []
    for f in files:
        try:
            val = json.loads(sys.stdin.read() if f == "-" else open(f, "r", encoding="utf-8").read())
        except ValueError as e:
            raise IpxError(f"Invalid JSON in {f}: {e}")
        out.extend(val if isinstance(val, list) else [val])
    for p in pairs:
        name, sep, url = p.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise IpxError(f"Bad --mcp value '{p}'.", "Use NAME=URL, e.g. --mcp docs=https://mcp.example.com/mcp. For auth, pass a JSON file with --mcp-file.")
        out.append({"name": name.strip(), "url": url.strip(), "transport": "auto", "auth_type": "none"})
    return out or None


def vis_text(v: Any) -> Text:
    return Text(str(v or "—"), style="ok" if v == "public" else "muted")


def show_agent(a: Dict[str, Any]) -> None:
    rows = [
        ("Model id", a.get("id")), ("Name", a.get("name")), ("Description", a.get("description")),
        ("Backing model", a.get("backing_model")), ("Visibility", vis_text(a.get("visibility"))),
        ("Registry", a.get("registry")), ("Billing", a.get("payer_label") or a.get("payer")),
        ("Your role", a.get("role")), ("MCP servers", ", ".join(f"{s.get('name')} ({s.get('url')})" for s in a.get("mcp_servers") or []) or None),
        ("Updated", str(a.get("updated_at") or "")[:16].replace("T", " ") or None),
    ]
    ui.console.print(ui.kv_panel(str(a.get("name") or a.get("id") or "Agent"), [r for r in rows if r[1] not in (None, "")]))
    if a.get("system_prompt"):
        ui.console.print(ui.kv_panel("System prompt", [("", a["system_prompt"])]))
    members = a.get("members") or []
    if members:
        ui.console.print(ui.make_table(["User", "Email", "Role"], [(m.get("user_id"), m.get("email"), m.get("role")) for m in members], title="Members"))
    for inv in a.get("invites") or []:
        ui.console.print(f"  pending invite: {inv.get('email') or inv.get('invitee_email') or inv.get('user_id')} as {inv.get('role')} · {inv.get('invite_url')}")


def agent_table(rows: List[Dict[str, Any]], title: str, owner: bool = True) -> None:
    ui.console.print(ui.make_table(
        ["Agent", "Name", "Backing model", "Visibility", "Billing"] + (["Role"] if owner else ["Owner"]),
        [(str(x.get("id") or "").removeprefix("agent/"), x.get("name"), x.get("backing_model"), vis_text(x.get("visibility")), "owner pays" if x.get("payer") == "owner" else "caller pays",
          (x.get("role") or ("—")) if owner else x.get("owner_username")) for x in rows],
        title=f"{title} ({len(rows)})"))


def agent_options(f):
    f = click.option("--mcp-file", "mcp_files", multiple=True, type=click.Path(), help="JSON file (or - for stdin) with MCP server objects/a list (name, url, transport, auth_type, token…).")(f)
    f = click.option("--mcp", "mcp_pairs", multiple=True, metavar="NAME=URL", help="Attach an MCP server without auth (repeatable).")(f)
    f = click.option("--payer", type=click.Choice(["caller", "owner"]), help="Who pays for each call (default: caller).")(f)
    f = click.option("--registry", type=click.Choice(["published", "unlisted"]), help="Public agents: list in the discovery registry or keep unlisted.")(f)
    f = click.option("--visibility", type=click.Choice(["private", "public"]), help="Who may call it (default: private).")(f)
    f = click.option("--system-file", help="Read the system prompt from a file (or - for stdin).")(f)
    f = click.option("--system", help="System prompt.")(f)
    f = click.option("--description", help="Short description (max 500 characters).")(f)
    return f


def body_from(name, model, description, system, system_file, visibility, registry, payer, mcp_pairs, mcp_files) -> Dict[str, Any]:
    b: Dict[str, Any] = {}
    for k, v in (("name", name), ("model", model), ("description", description), ("system_prompt", read_text(system, system_file)),
                 ("visibility", visibility), ("registry", registry), ("payer", payer)):
        if v is not None:
            b[k] = v
    mcp = parse_mcp(mcp_pairs, mcp_files)
    if mcp is not None:
        b["mcp_servers"] = mcp
    return b


# --------------------------------------------------------------------------- root


@click.group("agent", cls=IpxGroup, section="Cloud", short_help="Managed agents: create, share and chat",
             examples=["ipx agent profile set acme-bot", "ipx agent create support --name \"Support\" --model lightning --system \"Be kind.\"",
                       "ipx agent list", "ipx agent chat acme-bot/support", "ipx agent invite create acme-bot/support dev@acme.com --role editor"])
def group() -> None:
    """Build and share AI agents.

    An agent is a backing model plus a system prompt and optional MCP servers, called anywhere as
    `agent/<username>/<agent-id>`. Calls are billed at standard per-token rates to the caller or, if you choose
    `--payer owner`, to you. Set an organization context to bill chats with an agent to the organization.
    """


@group.command("list", short_help="Agents you can call")
@click.option("--mine", is_flag=True, help="Only agents you own.")
@pass_app
def list_(app: App, mine: bool) -> None:
    """Your agents, agents shared with you, and public agents you have used."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/models") or {}
    rows = rows_of(d)
    if mine:
        rows = [r for r in rows if r.get("is_owner")]

    def render(_: Any) -> None:
        if not rows:
            ui.info("No agents yet. Create one with `ipx agent create`, or browse `ipx agent registry`.")
            return
        agent_table(rows, "Agents")

    app.out({**d, "data": rows} if isinstance(d, dict) else rows, render)


@group.command("registry", short_help="Browse public agents")
@click.option("-q", "--query", "q", help="Search by name, id or description.")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@pass_app
def registry(app: App, q: Optional[str], limit: int) -> None:
    """Public agents that opted into the discovery registry."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/registry", params={"q": q, "limit": limit}) or {}
    rows = rows_of(d)
    app.out(d, lambda _: agent_table(rows, "Agent registry", owner=False) if rows else ui.info("No public agents match."))


@group.command("get", short_help="Show an agent")
@click.argument("agent")
@pass_app
def get(app: App, agent: str) -> None:
    """Details of AGENT (`username/agent-id`). Owners and editors also see the system prompt and members."""
    app.require_login()
    d = api(app, "GET", path(app, agent)) or {}
    app.out(d, lambda x: show_agent(x.get("agent") or x))


@group.command("create", short_help="Create an agent", examples=[
    "ipx agent create support --name Support --model lightning --system-file prompt.md --visibility public",
    "ipx agent create docs-bot --name \"Docs bot\" --model openai/gpt-4o --mcp docs=https://mcp.example.com/mcp"])
@click.argument("agent_id")
@click.option("--name", required=True, help="Display name.")
@click.option("-m", "--model", required=True, help="Backing catalog model (see `ipx p2g models`).")
@agent_options
@pass_app
def create(app: App, agent_id, name, model, description, system, system_file, visibility, registry, payer, mcp_pairs, mcp_files) -> None:
    """Create AGENT_ID (3+ lowercase letters, numbers, hyphens) under your agent username."""
    app.require_login()
    body = body_from(name, model, description, system, system_file, visibility, registry, payer, mcp_pairs, mcp_files)
    body["agent_id"] = agent_id.lower()
    d = api(app, "POST", "/v1/agents", body) or {}

    def render(x: Dict[str, Any]) -> None:
        ui.ok(f"Created agent [bold]{x.get('model_id')}[/bold]")
        ui.dim(f"Chat with it: ipx agent chat {str(x.get('model_id') or '').removeprefix('agent/')}")

    app.out(d, render)


@group.command("update", short_help="Change an agent")
@click.argument("agent")
@click.option("--name", help="New display name.")
@click.option("-m", "--model", help="New backing model.")
@agent_options
@pass_app
def update(app: App, agent, name, model, description, system, system_file, visibility, registry, payer, mcp_pairs, mcp_files) -> None:
    """Update only the fields you pass. `--mcp`/`--mcp-file` replace the whole MCP server list."""
    app.require_login()
    body = body_from(name, model, description, system, system_file, visibility, registry, payer, mcp_pairs, mcp_files)
    if not body:
        raise IpxError("Nothing to change.", "Pass at least one option, e.g. --name or --system.")
    d = api(app, "PATCH", path(app, agent), body) or {}
    app.out(d, lambda x: ui.ok(f"Updated [bold]{x.get('model_id') or agent}[/bold]"))


@group.command("delete", short_help="Delete an agent")
@click.argument("agent")
@pass_app
def delete(app: App, agent: str) -> None:
    """Permanently delete AGENT and revoke everyone's access to it."""
    app.require_login()
    p = path(app, agent)
    if not ui.confirm(f"Delete agent [bold]{agent}[/bold]? This cannot be undone."):
        raise click.Abort()
    d = api(app, "DELETE", p) or {}
    app.out(d, lambda _: ui.ok(f"Deleted [bold]{agent}[/bold]"))


@group.command("chat", short_help="Chat with an agent", examples=["ipx agent chat acme-bot/support"])
@click.argument("agent")
@pass_app
def chat_(app: App, agent: str) -> None:
    """Interactive chat with AGENT (billing: you, or the organization context)."""
    app.require_login()
    user, aid = split_ref(app, agent)
    model = f"agent/{user}/{aid}"
    chatmod.run_repl(
        title=f"Agent · {user}/{aid}", subtitle=f"billed to {'org ' + app.org if app.org else 'credits'}", backend=cloud.chat_backend(app, P2G),
        model=model, system=None, list_models=lambda: [r["id"] for r in rows_of(api(app, "GET", "/v1/agents/models")) if r.get("id")],
        footer_note=f"Billed to organization {app.org}." if app.org else None)


@group.command("ask", short_help="One-off question to an agent")
@click.argument("agent")
@click.argument("prompt", required=False)
@click.option("--no-stream", is_flag=True, help="Wait for the whole answer before printing.")
@pass_app
def ask(app: App, agent: str, prompt: Optional[str], no_stream: bool) -> None:
    """Send PROMPT (or standard input) to AGENT and print the answer."""
    app.require_login()
    text = prompt if prompt and prompt != "-" else (sys.stdin.read().strip() if not sys.stdin.isatty() else None)
    if not text:
        raise IpxError("No prompt given.", "Pass it as an argument or pipe it in.")
    user, aid = split_ref(app, agent)
    chatmod.one_shot(backend=cloud.chat_backend(app, P2G), prompt=text, model=f"agent/{user}/{aid}", system=None,
                     as_json=app.json, stream_out=not no_stream)


# --------------------------------------------------------------------------- profile / settings


@group.group("profile", cls=IpxGroup, short_help="Your public agent username")
def profile() -> None:
    """The public username that prefixes every agent you own."""


@profile.command("show")
@pass_app
def profile_show(app: App) -> None:
    """Show your agent username."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/profile") or {}
    app.out(d, lambda x: ui.console.print(ui.kv_panel("Agent profile", [("Username", x.get("username") or "not set")])))


@profile.command("set")
@click.argument("username")
@pass_app
def profile_set(app: App, username: str) -> None:
    """Set USERNAME (3-30 lowercase letters, numbers, hyphens, underscores)."""
    app.require_login()
    d = api(app, "PUT", "/v1/agents/profile", {"username": username}) or {}
    app.out(d, lambda x: ui.ok(f"Agent username is now [bold]{x.get('username')}[/bold]"))


@group.group("settings", cls=IpxGroup, short_help="Agent preferences")
def settings() -> None:
    """Preferences for how agents appear to you."""


@settings.command("show")
@pass_app
def settings_show(app: App) -> None:
    """Show agent settings and the role capability matrix."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/settings") or {}

    def render(x: Dict[str, Any]) -> None:
        s = x.get("settings") or {}
        ui.console.print(ui.kv_panel("Agent settings", [("Include agents in /v1/models", "yes" if s.get("include_in_models") else "no")]))
        ui.console.print(ui.make_table(["Role", "Can"], [(r.get("role"), ", ".join(r.get("capabilities") or [])) for r in x.get("roles") or []], title="Roles"))

    app.out(d, render)


@settings.command("set")
@click.option("--include-in-models/--no-include-in-models", default=None, help="List your agents in the OpenAI-style /v1/models response.")
@pass_app
def settings_set(app: App, include_in_models: Optional[bool]) -> None:
    """Change agent settings."""
    app.require_login()
    if include_in_models is None:
        raise IpxError("Nothing to change.", "Pass --include-in-models or --no-include-in-models.")
    d = api(app, "PUT", "/v1/agents/settings", {"include_in_models": include_in_models}) or {}
    app.out(d, lambda _: ui.ok("Settings saved."))


@group.command("roles", short_help="What each sharing role can do")
@pass_app
def roles(app: App) -> None:
    """Roles you can grant when inviting people to an agent."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/settings") or {}
    app.out(d.get("roles") or [], lambda rs: ui.console.print(ui.make_table(["Role", "Can"], [(r.get("role"), ", ".join(r.get("capabilities") or [])) for r in rs], title="Agent roles")))


# --------------------------------------------------------------------------- invites


@group.group("invite", cls=IpxGroup, short_help="Share an agent by invite")
def invite() -> None:
    """Invite people to an agent, and answer invites you received. An invite grants nothing until accepted."""


@invite.command("create")
@click.argument("agent")
@click.argument("who", metavar="EMAIL_OR_USER_ID")
@click.option("--role", type=click.Choice(ROLES), default="user", show_default=True)
@pass_app
def invite_create(app: App, agent: str, who: str, role: str) -> None:
    """Invite WHO to AGENT. Send them the printed link."""
    app.require_login()
    key = "user_id" if "@" not in who and len(who) >= 32 else "email"
    d = api(app, "POST", path(app, agent).replace("/v1/agents/", "/v1/agents/invite/", 1), {key: who, "role": role}) or {}

    def render(x: Dict[str, Any]) -> None:
        ui.ok(f"Invited [bold]{who}[/bold] as {x.get('role_label') or role}")
        ui.console.print(f"  {x.get('invite_url')}")
        if x.get("note"):
            ui.dim(x["note"])

    app.out(d, render)


@invite.command("list", short_help="Invites you received")
@pass_app
def invite_list(app: App) -> None:
    """Invites sent to you, pending first."""
    app.require_login()
    d = api(app, "GET", "/v1/agents/invites") or {}
    rows = rows_of(d, "invites")
    app.out(d, lambda _: ui.console.print(ui.make_table(
        ["Token", "Agent", "Role", "Status"],
        [(i.get("invite_token"), f"{i.get('owner_username')}/{i.get('agent_id')}", i.get("role"), i.get("status")) for i in rows],
        title=f"Agent invites ({len(rows)})")) if rows else ui.info("No invites."))


@invite.command("preview")
@click.argument("token")
@pass_app
def invite_preview(app: App, token: str) -> None:
    """See what an invite TOKEN (or link) grants, and who pays, before accepting."""
    app.require_login()
    d = api(app, "GET", f"/v1/agents/invite/{token.rsplit('=', 1)[-1].rsplit('/', 1)[-1]}") or {}
    app.out(d, lambda x: ui.console.print(ui.kv_panel("Agent invite", [(k.replace("_", " "), v) for k, v in x.items() if not isinstance(v, (dict, list))])))


for _name, _verb, _past in (("accept", "Accept", "Invite accepted."), ("deny", "Deny", "Invite denied.")):
    def _make(name: str = _name, verb: str = _verb, past: str = _past) -> None:
        @invite.command(name, short_help=f"{verb} an invite")
        @click.argument("token")
        @pass_app
        def _cmd(app: App, token: str) -> None:
            app.require_login()
            d = api(app, "POST", f"/v1/agents/invite/{token.rsplit('=', 1)[-1].rsplit('/', 1)[-1]}/{name}", {}) or {}
            app.out(d, lambda _: ui.ok(past))

        _cmd.__doc__ = f"{verb} the invite TOKEN (the token or the full invite link)."

    _make()


@invite.command("revoke")
@click.argument("token")
@pass_app
def invite_revoke(app: App, token: str) -> None:
    """Withdraw a pending invite you sent."""
    app.require_login()
    d = api(app, "DELETE", f"/v1/agents/invite/{token.rsplit('=', 1)[-1].rsplit('/', 1)[-1]}") or {}
    app.out(d, lambda _: ui.ok("Invite revoked."))


# --------------------------------------------------------------------------- members


@group.group("members", cls=IpxGroup, short_help="People with access to an agent")
def members() -> None:
    """List and manage who can use an agent."""


@members.command("list")
@click.argument("agent")
@pass_app
def members_list(app: App, agent: str) -> None:
    """Members of AGENT and their roles."""
    app.require_login()
    d = api(app, "GET", path(app, agent, "/members")) or {}
    rows = rows_of(d, "members")
    app.out(d, lambda _: ui.console.print(ui.make_table(["User", "Email", "Role", "Since"], [(m.get("user_id"), m.get("email"), m.get("role"), str(m.get("granted_at") or "")[:10]) for m in rows],
                                                        title=f"Members ({len(rows)})")) if rows else ui.info("No members yet."))


@members.command("role")
@click.argument("agent")
@click.argument("user_id")
@click.argument("role", type=click.Choice(ROLES))
@pass_app
def members_role(app: App, agent: str, user_id: str, role: str) -> None:
    """Change a member's role."""
    app.require_login()
    d = api(app, "PATCH", path(app, agent, f"/members/{user_id}"), {"role": role}) or {}
    app.out(d, lambda _: ui.ok(f"{user_id} is now {role}"))


@members.command("remove")
@click.argument("agent")
@click.argument("user_id")
@pass_app
def members_remove(app: App, agent: str, user_id: str) -> None:
    """Remove a member from AGENT."""
    app.require_login()
    if not ui.confirm(f"Remove {user_id} from {agent}?"):
        raise click.Abort()
    d = api(app, "DELETE", path(app, agent, f"/members/{user_id}")) or {}
    app.out(d, lambda _: ui.ok("Member removed."))


COMMANDS = [group]
