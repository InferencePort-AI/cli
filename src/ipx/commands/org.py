"""ipx org - manage organizations. Everything except `list`, `create`, `plans` and `accept` acts on the org context."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

import click
from rich.text import Text

from ipx import ui
from ipx.app import App, org_path, pass_app
from ipx.clickx import IpxCommand, IpxGroup
from ipx.errors import IpxError

ROLES = ["admin", "billing_manager", "auditor", "member"]


# --------------------------------------------------------------------------- helpers


def items(d: Any, *keys: str) -> List[Dict[str, Any]]:
    """Pull the list out of a response regardless of the wrapper key."""
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        for k in keys:
            if isinstance(d.get(k), list):
                return [x for x in d[k] if isinstance(x, dict)]
        for v in d.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def scalar_rows(d: Dict[str, Any], skip=()) -> List[Any]:
    rows = []
    for k, v in d.items():
        if k in skip:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
            v = v if len(v) < 90 else v[:87] + "…"
        rows.append((k.replace("_", " "), v))
    return rows


def show(title: str, d: Dict[str, Any], **kw: Any) -> None:
    ui.console.print(ui.kv_panel(title, scalar_rows(d or {}, **kw)))


def table_of(title: str, rows: List[Dict[str, Any]], cols: List[str]) -> None:
    ui.console.print(ui.make_table([c.replace("_", " ").title() for c in cols],
                                   [[_cell(r.get(c)) for c in cols] for r in rows], title=f"{title} ({len(rows)})"))


def _cell(v: Any) -> Any:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)[:60]
    if isinstance(v, str) and len(v) > 19 and v[10:11] == "T":
        return v[:16].replace("T", " ")
    return v


def load_json(data: Optional[str], file: Optional[str]) -> Optional[Dict[str, Any]]:
    raw = None
    if file:
        raw = sys.stdin.read() if file == "-" else open(file, "r", encoding="utf-8").read()
    elif data:
        raw = data
    if raw is None:
        return None
    try:
        val = json.loads(raw)
    except ValueError as e:
        raise IpxError(f"Invalid JSON: {e}")
    if not isinstance(val, dict):
        raise IpxError("Expected a JSON object.")
    return val


def json_options(f):
    f = click.option("--file", "jfile", type=click.Path(), help="Read the JSON body from a file (or - for stdin).")(f)
    return click.option("--data", "jdata", help="JSON body as a string.")(f)


def csv_list(v: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in v.split(",") if x.strip()] if v is not None else None


def need_confirm(slug: str, action: str) -> None:
    if not ui.confirm(f"{action} [bold]{slug}[/bold]?"):
        raise click.Abort()


# --------------------------------------------------------------------------- root


@click.group("org", cls=IpxGroup, section="Account", short_help="Organizations: members, teams, keys, billing, policy",
             examples=["ipx org list", "ipx auth context acme", "ipx org members list", "ipx org invite create dev@acme.com --role admin",
                       "ipx org keys create ci --monthly-limit 25"])
def group() -> None:
    """Manage organizations you belong to.

    Most subcommands act on the organization chosen with `ipx auth context <org>` (or --org). Role-based
    permissions and plan limits are enforced by the server; you will see its message if an action is not allowed.
    """


@group.command("list", short_help="Organizations you belong to")
@pass_app
def list_(app: App) -> None:
    """List your organizations. The active context is marked."""
    app.require_jwt("Listing organizations")
    d = app.api.get("/v1/orgs", org=False) or {}
    orgs = items(d, "orgs")

    def render(_: Any) -> None:
        cur = app.cfg.org
        rows = [[("● " if o.get("slug") == cur else "") + str(o.get("slug")), o.get("name"), o.get("role"), o.get("effective_plan") or o.get("plan"), o.get("member_count")] for o in orgs]
        ui.console.print(ui.make_table(["Slug", "Name", "Role", "Plan", "Members"], rows, title=f"Organizations ({len(orgs)})", right=["Members"]))
        if cur:
            ui.dim("● = active context")

    app.out(d, render)


@group.command("plans", short_help="Organization plans and entitlements")
@pass_app
def plans(app: App) -> None:
    """The organization plan catalog ."""
    app.require_login()
    d = app.api.get("/v1/orgs/plans", org=False) or {}
    pl = items(d, "plans")
    app.out(d, lambda _: table_of("Organization plans", pl, [c for c in ("key", "name", "price", "seats") if any(c in p for p in pl)] or list(pl[0].keys())[:5]) if pl else show("Plans", d))


@group.command("create", examples=["ipx org create --name \"Acme Inc\" --slug acme"])
@click.option("--name", required=True, help="Display name (max 80 chars).")
@click.option("--slug", required=True, help="Unique URL-safe id (3-39 chars, lowercase letters, numbers, hyphens).")
@click.option("--billing-email", help="Billing contact (defaults to your email).")
@click.option("--use/--no-use", default=True, help="Switch the context to the new org.")
@pass_app
def create(app: App, name: str, slug: str, billing_email: Optional[str], use: bool) -> None:
    """Create an organization (you become its owner)."""
    app.require_jwt("Creating an organization")
    body: Dict[str, Any] = {"name": name, "slug": slug.lower()}
    if billing_email:
        body["billing_email"] = billing_email
    d = app.api.post("/v1/orgs", body, org=False) or {}
    if use:
        app.cfg.set_org(slug.lower())
    app.out(d, lambda _: ui.ok(f"Created organization [bold]{name}[/bold] ([accent]{slug.lower()}[/accent])" + (" and set it as your context." if use else ".")))


@group.command("info", short_help="Details of the active organization")
@pass_app
def info(app: App) -> None:
    """Details, plan and settings of the active organization."""
    d = app.api.get(org_path(app)) or {}
    o = d.get("org") if isinstance(d.get("org"), dict) else d

    def render(_: Any) -> None:
        show(f"{o.get('name', app.org)} · {o.get('slug', '')}", o, skip=("settings", "entitlements", "limits"))
        s = o.get("settings")
        if isinstance(s, dict) and s:
            show("Settings", {k: v for k, v in s.items() if k not in ("audit_webhook_secret",)})

    app.out(d, render)


@group.command("update", examples=["ipx org update --name \"Acme Corp\"", "ipx org update --data '{\"settings\":{\"default_member_role\":\"member\"}}'"])
@click.option("--name")
@click.option("--billing-email")
@json_options
@pass_app
def update(app: App, name, billing_email, jdata, jfile) -> None:
    """Rename the organization or change its settings."""
    body = load_json(jdata, jfile) or {}
    if name:
        body["name"] = name
    if billing_email:
        body["billing_email"] = billing_email
    if not body:
        raise IpxError("Nothing to update.", "Pass --name, --billing-email or --data/--file.")
    d = app.api.patch(org_path(app), body)
    app.out(d, lambda _: ui.ok(f"Updated [bold]{app.require_org()}[/bold]"))


@group.command("delete", short_help="Delete the active organization")
@click.option("--forfeit-credits", is_flag=True, help="Discard any remaining wallet credits.")
@pass_app
def delete(app: App, forfeit_credits: bool) -> None:
    """Permanently delete the active organization. You must confirm by typing its slug."""
    slug = app.require_org()
    if not ui.STATE["yes"]:
        typed = ui.ask(f"Type the organization slug ({slug}) to confirm deletion")
        if typed.strip().lower() != slug:
            raise IpxError("Slug did not match; nothing was deleted.")
    body: Dict[str, Any] = {"confirm": slug}
    if forfeit_credits:
        body["forfeit_credits"] = True
    app.api.request("DELETE", org_path(app), json=body)
    if app.cfg.org == slug:
        app.cfg.set_org(None)
    app.out({"deleted": slug}, lambda _: ui.ok(f"Deleted organization [bold]{slug}[/bold]"))


@group.command("transfer", short_help="Transfer ownership")
@click.argument("user_id")
@pass_app
def transfer(app: App, user_id: str) -> None:
    """Transfer ownership of the organization to another member (by user id)."""
    need_confirm(app.require_org(), "Transfer ownership of")
    d = app.api.post(org_path(app, "/transfer"), {"user_id": user_id})
    app.out(d, lambda _: ui.ok("Ownership transferred."))


# --------------------------------------------------------------------------- members


@group.group("members", cls=IpxGroup, short_help="Members, roles, limits and suspension")
def members() -> None:
    """Manage who is in the organization."""


@members.command("list")
@pass_app
def members_list(app: App) -> None:
    """List members and roles."""
    d = app.api.get(org_path(app, "/members")) or {}
    ms = items(d, "members")
    app.out(d, lambda _: table_of("Members", ms, [c for c in ("email", "role", "user_id", "teams", "suspended", "joined_at") if any(c in m for m in ms)] or ["email", "role"]))


@members.command("role", examples=["ipx org members role 8a1f… admin"])
@click.argument("user_id")
@click.argument("role", type=click.Choice(ROLES))
@pass_app
def members_role(app: App, user_id: str, role: str) -> None:
    """Change a member's role."""
    d = app.api.patch(org_path(app, f"/members/{user_id}"), {"role": role})
    app.out(d, lambda _: ui.ok(f"Role set to [bold]{role}[/bold]"))


@members.command("remove")
@click.argument("user_id")
@pass_app
def members_remove(app: App, user_id: str) -> None:
    """Remove a member from the organization."""
    need_confirm(user_id, "Remove member")
    app.api.delete(org_path(app, f"/members/{user_id}"))
    app.out({"removed": user_id}, lambda _: ui.ok("Member removed."))


@members.command("suspend")
@click.argument("user_id")
@pass_app
def members_suspend(app: App, user_id: str) -> None:
    """Suspend a member (blocks org usage until unsuspended)."""
    app.api.post(org_path(app, f"/members/{user_id}/suspend"), {})
    app.out({"suspended": user_id}, lambda _: ui.ok("Member suspended."))


@members.command("unsuspend")
@click.argument("user_id")
@pass_app
def members_unsuspend(app: App, user_id: str) -> None:
    """Restore a suspended member."""
    app.api.post(org_path(app, f"/members/{user_id}/unsuspend"), {})
    app.out({"unsuspended": user_id}, lambda _: ui.ok("Member restored."))


@members.command("limits", examples=["ipx org members limits 8a1f… --monthly-credits 20 --models gpt-4o,lightning --modalities text,image"])
@click.argument("user_id")
@click.option("--monthly-credits", type=float, help="Monthly credit cap (use 0 to block, omit to keep).")
@click.option("--no-cap", is_flag=True, help="Remove the monthly cap.")
@click.option("--models", help="Comma-separated allowed model ids ('' = any).")
@click.option("--modalities", help="Comma-separated allowed modalities: text,image,video,audio.")
@pass_app
def members_limits(app: App, user_id, monthly_credits, no_cap, models, modalities) -> None:
    """Set a member's spend cap and model/modality allow-lists."""
    body: Dict[str, Any] = {}
    if no_cap:
        body["monthly_credit_limit"] = None
    elif monthly_credits is not None:
        body["monthly_credit_limit"] = monthly_credits
    if models is not None:
        body["allowed_models"] = csv_list(models) or None
    if modalities is not None:
        body["allowed_modalities"] = csv_list(modalities) or None
    if not body:
        raise IpxError("Nothing to change.")
    d = app.api.patch(org_path(app, f"/members/{user_id}/limits"), body)
    app.out(d, lambda _: ui.ok("Member limits updated."))


# --------------------------------------------------------------------------- invitations


@group.group("invite", cls=IpxGroup, short_help="Invitations")
def invite() -> None:
    """Invite people to the organization."""


@invite.command("create", examples=["ipx org invite create dev@acme.com --role admin --team backend-team-id"])
@click.argument("email")
@click.option("--role", type=click.Choice(ROLES), default="member", show_default=True)
@click.option("--team", "teams", multiple=True, help="Team id to add them to (repeatable).")
@click.option("--no-email", is_flag=True, help="Do not send an email; share the link yourself.")
@pass_app
def invite_create(app: App, email, role, teams, no_email) -> None:
    """Invite someone by email."""
    body: Dict[str, Any] = {"email": email, "role": role, "send_email": not no_email}
    if teams:
        body["team_ids"] = list(teams)
    d = app.api.post(org_path(app, "/invitations"), body) or {}

    def render(d: Dict[str, Any]) -> None:
        ui.ok(f"Invited [bold]{email}[/bold] as {role}" + ("" if no_email else " — email sent"))
        link = d.get("invite_url") or (d.get("invitation") or {}).get("invite_url") or d.get("url")
        if link:
            ui.console.print(f"  [muted]link:[/muted] {link}")

    app.out(d, render)


@invite.command("list")
@pass_app
def invite_list(app: App) -> None:
    """Pending invitations."""
    d = app.api.get(org_path(app, "/invitations")) or {}
    rows = items(d, "invitations")
    app.out(d, lambda _: table_of("Invitations", rows, [c for c in ("id", "email", "role", "expires_at", "created_at") if any(c in r for r in rows)] or ["email"]))


@invite.command("revoke")
@click.argument("invitation_id")
@pass_app
def invite_revoke(app: App, invitation_id: str) -> None:
    """Cancel a pending invitation."""
    app.api.delete(org_path(app, f"/invitations/{invitation_id}"))
    app.out({"revoked": invitation_id}, lambda _: ui.ok("Invitation revoked."))


@invite.command("resend")
@click.argument("invitation_id")
@pass_app
def invite_resend(app: App, invitation_id: str) -> None:
    """Send an invitation email again."""
    app.api.post(org_path(app, f"/invitations/{invitation_id}/resend"), {})
    app.out({"resent": invitation_id}, lambda _: ui.ok("Invitation re-sent."))


@invite.command("accept", examples=["ipx org invite accept <token-from-link>"])
@click.argument("token")
@click.option("--preview", is_flag=True, help="Show what you would be joining without accepting.")
@pass_app
def invite_accept(app: App, token: str, preview: bool) -> None:
    """Accept an invitation (the token is the `org_invite=` value in the link)."""
    token = token.split("org_invite=")[-1]
    if preview:
        d = app.api.get("/v1/orgs/invitations/preview", params={"token": token}, org=False) or {}
        app.out(d, lambda _: show("Invitation", d))
        return
    app.require_jwt("Accepting an invitation")
    d = app.api.post("/v1/orgs/invitations/accept", {"token": token}, org=False) or {}
    slug = (d.get("org") or {}).get("slug") or d.get("slug")
    app.out(d, lambda _: ui.ok("Invitation accepted" + (f" — joined [bold]{slug}[/bold]. Use `ipx auth context {slug}`." if slug else ".")))


# --------------------------------------------------------------------------- teams


@group.group("teams", cls=IpxGroup, short_help="Teams")
def teams() -> None:
    """Organize members into teams."""


@teams.command("list")
@pass_app
def teams_list(app: App) -> None:
    """List teams."""
    d = app.api.get(org_path(app, "/teams")) or {}
    rows = items(d, "teams")
    app.out(d, lambda _: table_of("Teams", rows, [c for c in ("id", "slug", "name", "description", "member_count") if any(c in r for r in rows)] or ["slug"]))


@teams.command("create")
@click.option("--name", required=True)
@click.option("--slug", required=True)
@click.option("--description", default="")
@pass_app
def teams_create(app: App, name, slug, description) -> None:
    """Create a team."""
    d = app.api.post(org_path(app, "/teams"), {"name": name, "slug": slug.lower(), "description": description})
    app.out(d, lambda _: ui.ok(f"Created team [bold]{name}[/bold]"))


@teams.command("update")
@click.argument("team_id")
@click.option("--name")
@click.option("--description")
@pass_app
def teams_update(app: App, team_id, name, description) -> None:
    """Rename a team or change its description."""
    body = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
    if not body:
        raise IpxError("Nothing to update.")
    d = app.api.patch(org_path(app, f"/teams/{team_id}"), body)
    app.out(d, lambda _: ui.ok("Team updated."))


@teams.command("delete")
@click.argument("team_id")
@pass_app
def teams_delete(app: App, team_id: str) -> None:
    """Delete a team."""
    need_confirm(team_id, "Delete team")
    app.api.delete(org_path(app, f"/teams/{team_id}"))
    app.out({"deleted": team_id}, lambda _: ui.ok("Team deleted."))


@teams.command("members")
@click.argument("team_id")
@pass_app
def teams_members(app: App, team_id: str) -> None:
    """Members of a team."""
    d = app.api.get(org_path(app, f"/teams/{team_id}/members")) or {}
    rows = items(d, "members")
    app.out(d, lambda _: table_of("Team members", rows, [c for c in ("user_id", "email", "role") if any(c in r for r in rows)] or ["user_id"]))


@teams.command("add")
@click.argument("team_id")
@click.argument("user_id")
@click.option("--role", default="member", show_default=True)
@pass_app
def teams_add(app: App, team_id, user_id, role) -> None:
    """Add a member to a team."""
    d = app.api.put(org_path(app, f"/teams/{team_id}/members/{user_id}"), {"role": role})
    app.out(d, lambda _: ui.ok("Added to team."))


@teams.command("remove")
@click.argument("team_id")
@click.argument("user_id")
@pass_app
def teams_remove(app: App, team_id, user_id) -> None:
    """Remove a member from a team."""
    app.api.delete(org_path(app, f"/teams/{team_id}/members/{user_id}"))
    app.out({"removed": user_id}, lambda _: ui.ok("Removed from team."))


# --------------------------------------------------------------------------- API keys


@group.group("keys", cls=IpxGroup, short_help="Organization API keys")
def keys() -> None:
    """Organization-scoped API keys with optional spend caps and restrictions."""


@keys.command("list")
@pass_app
def keys_list(app: App) -> None:
    """List organization API keys."""
    d = app.api.get(org_path(app, "/api-keys")) or {}
    rows = items(d, "items", "keys")
    app.out(d, lambda _: table_of("Organization API keys", rows, [c for c in ("id", "name", "keyPrefix", "monthly_credit_limit", "lastUsedAt", "isRevoked") if any(c in r for r in rows)] or ["name"]))


@keys.command("create", examples=["ipx org keys create ci --monthly-limit 25 --models lightning --ip 203.0.113.0/24"])
@click.argument("name")
@click.option("--description")
@click.option("--expires", help="ISO-8601 date/time.")
@click.option("--monthly-limit", type=float, help="Monthly credit cap.")
@click.option("--models", help="Comma-separated allowed model ids.")
@click.option("--modalities", help="Comma-separated allowed modalities.")
@click.option("--ip", "ips", help="Comma-separated CIDR allow-list.")
@click.option("--rpm", type=int, help="Requests per minute limit.")
@pass_app
def keys_create(app: App, name, description, expires, monthly_limit, models, modalities, ips, rpm) -> None:
    """Create an organization API key. The secret is shown once."""
    body: Dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if expires:
        body["expiresAt"] = expires if "T" in expires else expires + "T00:00:00Z"
    for k, v in (("monthly_credit_limit", monthly_limit), ("allowed_models", csv_list(models)), ("allowed_modalities", csv_list(modalities)),
                 ("ip_allowlist", csv_list(ips)), ("rate_limit_rpm", rpm)):
        if v is not None:
            body[k] = v
    d = app.api.post(org_path(app, "/api-keys"), body) or {}

    def render(d: Dict[str, Any]) -> None:
        ui.ok(f"Created key [bold]{name}[/bold] for [accent]{app.require_org()}[/accent]")
        ui.console.print(ui.kv_panel("Save this key now — it is shown only once", [("Key", Text(d.get("rawKey", ""), style="bold " + ui.WARN))]))

    app.out(d, render)


@keys.command("update")
@click.argument("key_id")
@json_options
@pass_app
def keys_update(app: App, key_id, jdata, jfile) -> None:
    """Change a key's restrictions with a JSON body (name, monthly_credit_limit, allowed_models, ip_allowlist, rate_limit_rpm…)."""
    body = load_json(jdata, jfile)
    if not body:
        raise IpxError("Pass the changes with --data or --file.")
    d = app.api.patch(org_path(app, f"/api-keys/{key_id}"), body)
    app.out(d, lambda _: ui.ok("Key updated."))


@keys.command("revoke")
@click.argument("key_id")
@pass_app
def keys_revoke(app: App, key_id: str) -> None:
    """Revoke an organization API key."""
    need_confirm(key_id, "Revoke key")
    app.api.delete(org_path(app, f"/api-keys/{key_id}"))
    app.out({"revoked": key_id}, lambda _: ui.ok("Key revoked."))


# --------------------------------------------------------------------------- billing


@group.group("billing", cls=IpxGroup, short_help="Wallet, plan, invoices")
def billing() -> None:
    """Organization billing: wallet credits, plan changes and invoices."""


@billing.command("info")
@pass_app
def billing_info(app: App) -> None:
    """Plan, wallet balance and billing settings."""
    d = app.api.get(org_path(app, "/billing")) or {}
    app.out(d, lambda _: show(f"Billing · {app.require_org()}", d))


@billing.command("ledger")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@pass_app
def billing_ledger(app: App, limit: int) -> None:
    """Organization credit ledger."""
    d = app.api.get(org_path(app, "/billing/ledger"), params={"limit": limit}) or {}
    rows = items(d, "entries")
    app.out(d, lambda _: table_of("Org ledger", rows, [c for c in ("created_at", "entry_type", "usage_kind", "delta_credits", "source") if any(c in r for r in rows)] or ["entry_type"]))


@billing.command("plan", examples=["ipx org billing plan team"])
@click.argument("plan")
@pass_app
def billing_plan(app: App, plan: str) -> None:
    """Change the organization plan (see `ipx org plans`)."""
    d = app.api.post(org_path(app, "/billing/plan"), {"plan": plan.lower()}) or {}

    def render(d: Dict[str, Any]) -> None:
        url = d.get("url") or d.get("checkout_url")
        ui.ok(f"Plan change requested: [bold]{plan}[/bold]")
        if url:
            ui.console.print(f"  Complete it in your browser: [underline]{url}[/underline]")

    app.out(d, render)


def _url_cmd(name: str, path: str, summary: str, body_fn=None, options=()) -> None:
    @billing.command(name, short_help=summary)
    @pass_app
    def _c(app: App, **kw: Any) -> None:
        d = app.api.post(org_path(app, path), body_fn(kw) if body_fn else {}) or {}
        url = d.get("url") or d.get("checkout_url") or d.get("portal_url")
        app.out(d, lambda _: (ui.ok(summary), ui.console.print(f"  [underline]{url}[/underline]") if url else None))

    _c.__doc__ = summary + "."


_url_cmd("portal", "/billing/portal", "Open the Stripe billing portal link")


@billing.command("credits", examples=["ipx org billing credits pack_25"])
@click.argument("pack_id")
@pass_app
def billing_credits(app: App, pack_id: str) -> None:
    """Buy a credit pack for the organization wallet. Prints a checkout link."""
    d = app.api.post(org_path(app, "/billing/credits/checkout"), {"pack_id": pack_id}) or {}
    url = d.get("url") or d.get("checkout_url")
    app.out(d, lambda _: (ui.ok(f"Checkout ready for [bold]{pack_id}[/bold]"), ui.console.print(f"  [underline]{url}[/underline]") if url else None))


@billing.command("promo")
@click.argument("code")
@pass_app
def billing_promo(app: App, code: str) -> None:
    """Redeem a promo code."""
    d = app.api.post(org_path(app, "/billing/promo"), {"code": code})
    app.out(d, lambda _: ui.ok("Promo code applied."))


@billing.command("contact-sales")
@click.argument("message")
@pass_app
def billing_sales(app: App, message: str) -> None:
    """Send a message to the InferencePort sales team."""
    d = app.api.post(org_path(app, "/billing/contact-sales"), {"message": message})
    app.out(d, lambda _: ui.ok("Message sent. We will be in touch."))


@billing.command("invoices")
@click.option("--limit", type=click.IntRange(1, 50), default=12, show_default=True)
@click.option("--status")
@click.option("--upcoming", is_flag=True, help="Show the upcoming invoice instead.")
@pass_app
def billing_invoices(app: App, limit, status, upcoming) -> None:
    """List invoices."""
    if upcoming:
        d = app.api.get(org_path(app, "/billing/invoices/upcoming")) or {}
        app.out(d, lambda _: show("Upcoming invoice", d))
        return
    d = app.api.get(org_path(app, "/billing/invoices"), params={"limit": limit, "status": status}) or {}
    rows = items(d, "invoices", "items")
    app.out(d, lambda _: table_of("Invoices", rows, [c for c in ("id", "number", "status", "amount_due", "total", "created") if any(c in r for r in rows)] or ["id"]))


@billing.command("details")
@json_options
@pass_app
def billing_details(app: App, jdata, jfile) -> None:
    """Show billing details, or replace them with --data/--file."""
    body = load_json(jdata, jfile)
    if body is None:
        d = app.api.get(org_path(app, "/billing/details")) or {}
        app.out(d, lambda _: show("Billing details", d))
    else:
        d = app.api.put(org_path(app, "/billing/details"), body)
        app.out(d, lambda _: ui.ok("Billing details saved."))


# --------------------------------------------------------------------------- usage / audit


@group.group("usage", cls=IpxGroup, short_help="Org usage and exports")
def usage() -> None:
    """Organization usage reports."""


@usage.command("breakdown")
@click.option("--days", type=click.IntRange(1, 365), default=30, show_default=True)
@click.option("--by", type=click.Choice(["user", "model", "key", "modality"]), default="user", show_default=True)
@pass_app
def usage_breakdown(app: App, days: int, by: str) -> None:
    """Spend broken down by user, model, key or modality."""
    d = app.api.get(org_path(app, "/usage/breakdown"), params={"days": days, "by": by}) or {}
    rows = items(d, "rows", "items", "breakdown", "usage")
    app.out(d, lambda _: table_of(f"Usage by {by} · last {days}d", rows, list(rows[0].keys())[:6]) if rows else show("Usage", d))


@usage.command("export")
@click.option("--days", type=click.IntRange(1, 365), default=30, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv")
@click.option("-o", "--output", help="Write to a file instead of stdout.")
@pass_app
def usage_export(app: App, days, fmt, output) -> None:
    """Export usage as CSV or JSON."""
    r = app.api.request("GET", org_path(app, "/usage/export"), params={"days": days, "format": fmt}, headers={"Accept": "*/*"})
    _write(r.body, output)


@usage.command("report")
@pass_app
def usage_report(app: App) -> None:
    """Email the usage report to configured recipients now."""
    d = app.api.post(org_path(app, "/usage/report"), {})
    app.out(d, lambda _: ui.ok("Usage report sent."))


def _write(body: bytes, output: Optional[str]) -> None:
    if output:
        with open(output, "wb") as fh:
            fh.write(body)
        ui.ok(f"Saved → [bold]{output}[/bold] [muted]({ui.fmt_bytes(len(body))})[/muted]")
    else:
        sys.stdout.buffer.write(body)


@group.group("audit", cls=IpxGroup, short_help="Audit log")
def audit() -> None:
    """The organization audit log."""


@audit.command("list")
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
@click.option("--action")
@click.option("--actor", help="Filter by actor email.")
@click.option("--since", help="ISO-8601 start time.")
@pass_app
def audit_list(app: App, limit, action, actor, since) -> None:
    """Recent audit events."""
    d = app.api.get(org_path(app, "/audit"), params={"limit": limit, "action": action, "actor": actor, "since": since}) or {}
    rows = items(d, "events", "entries", "items")
    app.out(d, lambda _: table_of("Audit log", rows, [c for c in ("created_at", "actor_email", "action", "target", "ip") if any(c in r for r in rows)] or list(rows[0].keys())[:5] if rows else ["action"]))


@audit.command("export")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv")
@click.option("--action")
@click.option("--actor")
@click.option("--since")
@click.option("-o", "--output")
@pass_app
def audit_export(app: App, fmt, action, actor, since, output) -> None:
    """Export the audit log."""
    r = app.api.request("GET", org_path(app, "/audit/export"), params={"format": fmt, "action": action, "actor": actor, "since": since}, headers={"Accept": "*/*"})
    _write(r.body, output)


@audit.command("webhook", short_help="Stream audit events to a webhook")
@click.option("--url", help="HTTPS endpoint (omit with --remove).")
@click.option("--secret", help="Signing secret.")
@click.option("--remove", is_flag=True)
@pass_app
def audit_webhook(app: App, url, secret, remove) -> None:
    """Configure (or remove) the audit-log webhook."""
    body: Dict[str, Any] = {"url": None} if remove else {"url": url, "secret": secret}
    if not remove and not url:
        raise IpxError("Pass --url or --remove.")
    d = app.api.put(org_path(app, "/audit/webhook"), body)
    app.out(d, lambda _: ui.ok("Audit webhook " + ("removed." if remove else "saved.")))


# --------------------------------------------------------------------------- domains / sso


@group.group("domains", cls=IpxGroup, short_help="Verified domains")
def domains() -> None:
    """Verify email domains you own (required for SSO)."""


@domains.command("list")
@pass_app
def domains_list(app: App) -> None:
    """Domains and verification state."""
    d = app.api.get(org_path(app, "/domains")) or {}
    rows = items(d, "domains")
    app.out(d, lambda _: table_of("Domains", rows, [c for c in ("id", "domain", "verified", "verified_at", "verification_token") if any(c in r for r in rows)] or ["domain"]))


@domains.command("add")
@click.argument("domain")
@pass_app
def domains_add(app: App, domain: str) -> None:
    """Add a domain; prints the DNS record to publish."""
    d = app.api.post(org_path(app, "/domains"), {"domain": domain.lower()}) or {}
    app.out(d, lambda _: (ui.ok(f"Added [bold]{domain}[/bold]"), show("DNS verification", d.get("domain") if isinstance(d.get("domain"), dict) else d)))


@domains.command("verify")
@click.argument("domain_id")
@pass_app
def domains_verify(app: App, domain_id: str) -> None:
    """Check DNS and mark the domain verified."""
    d = app.api.post(org_path(app, f"/domains/{domain_id}/verify"), {})
    app.out(d, lambda _: ui.ok("Domain verified."))


@domains.command("remove")
@click.argument("domain_id")
@pass_app
def domains_remove(app: App, domain_id: str) -> None:
    """Remove a domain."""
    need_confirm(domain_id, "Remove domain")
    app.api.delete(org_path(app, f"/domains/{domain_id}"))
    app.out({"removed": domain_id}, lambda _: ui.ok("Domain removed."))


@group.group("sso", cls=IpxGroup, short_help="Single sign-on (SAML)")
def sso() -> None:
    """Single sign-on configuration."""


@sso.command("get")
@pass_app
def sso_get(app: App) -> None:
    """Show the SSO configuration."""
    d = app.api.get(org_path(app, "/sso")) or {}
    app.out(d, lambda _: show("SSO", d))


@sso.command("set", examples=["ipx org sso set --file saml.json"])
@json_options
@pass_app
def sso_set(app: App, jdata, jfile) -> None:
    """Create or update the SSO connection from a JSON body (see the SSO docs for fields)."""
    body = load_json(jdata, jfile)
    if not body:
        raise IpxError("Pass the configuration with --data or --file.")
    d = app.api.put(org_path(app, "/sso"), body)
    app.out(d, lambda _: ui.ok("SSO saved."))


@sso.command("remove")
@pass_app
def sso_remove(app: App) -> None:
    """Delete the SSO connection."""
    need_confirm(app.require_org(), "Remove SSO for")
    app.api.delete(org_path(app, "/sso"))
    app.out({"removed": True}, lambda _: ui.ok("SSO removed."))


# --------------------------------------------------------------------------- policy


@group.group("policy", cls=IpxGroup, short_help="Model/modality and security policy")
def policy() -> None:
    """Organization-wide generation policy."""


@policy.command("get")
@pass_app
def policy_get(app: App) -> None:
    """Show the policy."""
    d = app.api.get(org_path(app, "/policies")) or {}
    app.out(d, lambda _: show("Policy", d.get("policies") if isinstance(d.get("policies"), dict) else d))


@policy.command("set", examples=["ipx org policy set --data '{\"allowed_models\":[\"lightning\"]}'"])
@json_options
@pass_app
def policy_set(app: App, jdata, jfile) -> None:
    """Update the policy with a JSON body."""
    body = load_json(jdata, jfile)
    if not body:
        raise IpxError("Pass the policy with --data or --file.")
    d = app.api.put(org_path(app, "/policies"), body)
    app.out(d, lambda _: ui.ok("Policy saved."))


@group.group("desktop-policy", cls=IpxGroup, short_help="What members may do in the desktop app & ipx")
def desktop_policy() -> None:
    """Controls for the desktop app and CLI: cloud use, model pulls, imports and server hosting."""


@desktop_policy.command("get")
@pass_app
def dp_get(app: App) -> None:
    """Show the stored desktop policy."""
    d = app.api.get(org_path(app, "/desktop-policy")) or {}
    app.out(d, lambda _: show("Desktop policy", d.get("desktop") if isinstance(d.get("desktop"), dict) else d))


@desktop_policy.command("set", examples=["ipx org desktop-policy set --pull-mode allowlist --pull-models llama3.2,qwen2.5 --server-hosting admins"])
@click.option("--lightning", type=click.Choice(["allow", "local_only"]), help="Allow cloud models or restrict to local only.")
@click.option("--lightning-billing", type=click.Choice(["personal", "org"]))
@click.option("--pull-mode", type=click.Choice(["any", "allowlist", "blocked"]))
@click.option("--pull-models", help="Comma-separated allow-list (with --pull-mode allowlist).")
@click.option("--allow-import/--block-import", "allow_import", default=None, help="Allow model imports (GGUF / Hugging Face).")
@click.option("--allow-tools/--block-tools", "allow_tools", default=None, help="Allow custom tools.")
@click.option("--server-hosting", type=click.Choice(["any", "admins", "disabled"]))
@click.option("--server-access", type=click.Choice(["manual", "org", "teams"]))
@json_options
@pass_app
def dp_set(app: App, lightning, lightning_billing, pull_mode, pull_models, allow_import, allow_tools, server_hosting, server_access, jdata, jfile) -> None:
    """Change desktop/CLI controls (partial update)."""
    body = load_json(jdata, jfile) or {}
    for k, v in (("lightning", lightning), ("lightning_billing", lightning_billing), ("pull_mode", pull_mode),
                 ("pull_models", csv_list(pull_models)), ("allow_model_import", allow_import), ("allow_custom_tools", allow_tools),
                 ("server_hosting", server_hosting), ("server_access", server_access)):
        if v is not None:
            body[k] = v
    if not body:
        raise IpxError("Nothing to change.")
    d = app.api.put(org_path(app, "/desktop-policy"), body)
    from ipx.policy import clear_cache

    clear_cache()
    app.out(d, lambda _: ui.ok("Desktop policy saved."))


@group.group("notifications", cls=IpxGroup, short_help="Budget and usage alerts")
def notifications() -> None:
    """Email/webhook notifications for the organization."""


@notifications.command("get")
@pass_app
def notif_get(app: App) -> None:
    """Show notification settings."""
    d = app.api.get(org_path(app, "/notifications")) or {}
    app.out(d, lambda _: show("Notifications", d))


@notifications.command("set")
@json_options
@pass_app
def notif_set(app: App, jdata, jfile) -> None:
    """Update notification settings with a JSON body."""
    body = load_json(jdata, jfile)
    if not body:
        raise IpxError("Pass the settings with --data or --file.")
    d = app.api.put(org_path(app, "/notifications"), body)
    app.out(d, lambda _: ui.ok("Notification settings saved."))


@notifications.command("test")
@pass_app
def notif_test(app: App) -> None:
    """Send a test notification."""
    d = app.api.post(org_path(app, "/notifications/test"), {})
    app.out(d, lambda _: ui.ok("Test notification sent."))


COMMANDS = [group]
