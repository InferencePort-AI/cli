"""ipx usage - quotas, wallet and organization spend in one view."""

from __future__ import annotations

from typing import Any, Dict

import click
from rich.text import Text

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError

METRIC_LABELS = {
    "cloudChatDaily": "Cloud chat (daily)",
    "imagesDaily": "Images (daily)",
    "videosDaily": "Videos (daily)",
    "audioWeekly": "Audio (weekly)",
    "verifyTokenWithEmailDaily": "Host-server verifications (daily)",
}


@click.group("usage", cls=IpxGroup, section="Account", short_help="Quotas, credits and spend")
def group() -> None:
    """See what you have used and what is left."""


@group.command("get", examples=["ipx usage get", "ipx --json usage get", "ipx -O acme usage get"])
@click.option("--quota/--no-quota", default=True, help="Include subscription quotas.")
@click.option("--wallet/--no-wallet", default=True, help="Include the Pay-2-Go wallet.")
@pass_app
def get(app: App, quota: bool, wallet: bool) -> None:
    """Subscription quotas, Pay-2-Go wallet and (with an org context) organization spend."""
    app.require_login()
    result: Dict[str, Any] = {}
    errors = []
    if quota:
        try:
            result["quota"] = app.api.get("/usage", org=False)
        except IpxError as e:
            errors.append(f"quota: {e.message}")
    if wallet:
        try:
            result["wallet"] = app.api.get("/v1/me")
        except IpxError as e:
            errors.append(f"wallet: {e.message}")
    org = app.org
    if org:
        try:
            result["org"] = app.api.get(f"/v1/orgs/{org}/usage/breakdown")
        except IpxError as e:
            errors.append(f"org {org}: {e.message}")
    if not result:
        raise IpxError("Could not load usage.", "; ".join(errors))

    def render(d: Dict[str, Any]) -> None:
        q = d.get("quota")
        if q:
            rows = []
            for key, m in (q.get("usage") or {}).items():
                if isinstance(m, dict):
                    rows.append((METRIC_LABELS.get(key, key), ui.bar(m.get("used", 0), m.get("limit"))))
            ui.console.print(ui.kv_panel(f"Quota · {q.get('plan_name') or q.get('plan_key') or 'plan'}", rows,
                                         subtitle=f"as of {str(q.get('generated_at', ''))[:16].replace('T', ' ')} UTC"))
        w = d.get("wallet")
        if w:
            wal = w.get("wallet") or {}
            ui.console.print(ui.kv_panel("Pay-2-Go wallet", [
                ("Balance", Text.assemble((ui.fmt_credits(wal.get("balance_credits")), "bold " + ui.OK), (" credits", "muted"))),
                ("Spent", f"{ui.fmt_credits(wal.get('total_spent'))} credits"),
            ], subtitle=f"billing: {'org ' + org if org else 'personal'}"))
        o = d.get("org")
        if o:
            _render_org_usage(org or "", o)
        for e in errors:
            ui.warn(e)

    app.out(result, render)


def _render_org_usage(org: str, o: Dict[str, Any]) -> None:
    rows = []
    for k, v in o.items():
        if isinstance(v, (str, int, float)):
            rows.append((k.replace("_", " "), v))
    ui.console.print(ui.kv_panel(f"Organization · {org}", rows or [("usage", "no data yet")]))
    for key in ("members", "by_member", "models", "by_model", "by_key", "keys"):
        items = o.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            cols = [c for c in items[0].keys() if not isinstance(items[0][c], (dict, list))][:6]
            ui.console.print(ui.make_table([c.replace("_", " ") for c in cols], [[i.get(c) for c in cols] for i in items], title=key.replace("_", " ").title()))


@group.command("ledger", short_help="Pay-2-Go credit ledger")
@click.option("--limit", type=click.IntRange(1, 500), default=25, show_default=True)
@click.pass_context
def ledger(ctx: click.Context, limit: int) -> None:
    """Alias for `ipx p2g ledger`."""
    from ipx.commands.cloudgen import p2g_group

    ctx.invoke(p2g_group.commands["ledger"], limit=limit)


@group.command("tiers", short_help="Subscription plans and limits (no sign-in needed)")
@pass_app
def tiers(app: App) -> None:
    """Compare the subscription plans and their quotas."""
    d = app.api.get("/tier-config", auth=False, org=False) or {}

    def render(d: Dict[str, Any]) -> None:
        rows = []
        for p in d.get("plans") or []:
            lim = p.get("limits") or {}
            rows.append((str(p.get("name") or "").replace("InferencePort AI ", ""), f"${float(p.get('price') or 0):.2f}", lim.get("cloudChatDaily") or "∞", lim.get("imagesDaily") or "∞",
                         lim.get("videosDaily") or "∞", lim.get("audioWeekly") or "∞"))
        ui.console.print(ui.make_table(["Plan", "Price", "Chat/day", "Images/day", "Videos/day", "Audio/wk"], rows, title="Plans",
                                       right=["Price", "Chat/day", "Images/day", "Videos/day", "Audio/wk"]))

    app.out(d, render)


COMMANDS = [group]
