"""ipx featherless - Featherless model deployments, personal or for an organization.

In an organization context (`ipx auth context <org>` or --org) every command acts on the organization's
deployments: the fee is billed to the org wallet and every member that may spend on the org can call the model.
Use --personal to work on your own deployments instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import click
from rich.text import Text

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError

STATE_STYLE = {"warm": "ok", "loading": "warn", "cold": "bad"}
LIVE = ("active", "cancelled_pending")


def hf(model: str) -> str:
    """Model ids are written `Author/Model` or `hf/Author/Model`; the API wants the prefixed form."""
    m = model.strip()
    return m if m.startswith("hf/") else f"hf/{m}"


def bare(model: str) -> str:
    m = model.strip()
    return m[3:] if m.startswith("hf/") else m


def base(app: App) -> str:
    """Deployments endpoint for the active context: the organization's, or the personal one."""
    org = app.org
    return f"/v1/orgs/{org}/deployments" if org else "/v1/deploy"


def auth(app: App) -> None:
    """Organization routes need an interactive session; personal ones also accept a personal API key."""
    app.require_jwt("Organization deployments") if app.org else app.require_login()


def owner_label(app: App) -> str:
    return f"organization {app.org}" if app.org else "your account"


def money(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def date(v: Any) -> str:
    return str(v)[:10] if v else "—"


def status_text(s: Any) -> Text:
    s = str(s or "")
    style = {"active": "ok", "cancelled_pending": "warn"}.get(s, "muted")
    return Text("canceling" if s == "cancelled_pending" else s or "—", style=style)


def state_text(s: Any) -> Text:
    s = str(s or "")
    label = {"warm": "warm", "loading": "initializing", "cold": "cold"}.get(s, s or "—")
    return Text(label, style=STATE_STYLE.get(s, "muted"))


def deployments_of(d: Any) -> List[Dict[str, Any]]:
    """Rows from a list response, whatever the envelope (bare list, or data/deployments/models/items)."""
    rows = d
    if isinstance(d, dict):
        rows = next((d[k] for k in ("data", "deployments", "models", "items") if isinstance(d.get(k), list)), [])
    return [x for x in (rows or []) if isinstance(x, dict)]


def meta_of(d: Any) -> Dict[str, Any]:
    return d if isinstance(d, dict) else {}


def show_deployment(x: Dict[str, Any], title: Optional[str] = None) -> None:
    rows = [
        ("Model", x.get("featherless_model_id")),
        ("Call as", x.get("display_model_id")),
        ("Status", status_text(x.get("status"))),
        ("Model state", state_text(x.get("model_state")) if x.get("model_state") else None),
        ("Context", f"{int(x['context_length']):,} tokens" if x.get("context_length") else None),
        ("Monthly fee", money(x.get("monthly_fee"))),
        ("Prompt / completion", f"{money(x.get('pricing_prompt_per_million'))} / {money(x.get('pricing_completion_per_million'))} per M tokens"),
        ("Cycle", f"{date(x.get('billing_cycle_start'))} → {date(x.get('billing_cycle_end'))}"),
        ("Owner", f"org {x['org_id']}" if x.get("org_id") else "personal"),
        ("ID", x.get("id")),
    ]
    ui.console.print(ui.kv_panel(title or str(x.get("featherless_model_id") or "Deployment"), [r for r in rows if r[1] is not None]))


@click.group("featherless", cls=IpxGroup, section="Cloud", short_help="Featherless deployments (personal or organization)",
             examples=["ipx featherless models --search qwen", "ipx featherless deploy Qwen/Qwen2.5-7B-Instruct",
                       "ipx featherless list", "ipx -O acme featherless deploy meta-llama/Llama-3.1-8B-Instruct",
                       "ipx featherless cancel 3f2a…"])
def group() -> None:
    """Deploy any model from Featherless AI and manage your deployments.

    A deployment costs $0.50/month and token usage is billed at Featherless rates plus a small markup, always from
    paid credits. With an organization context the deployment belongs to the organization and bills its wallet;
    only owners and admins can deploy, cancel, restore and terminate. Call a deployed model with the id shown by
    `ipx featherless list` (hf/Author/Model) through any chat command.
    """


@group.command("models", short_help="Browse the Featherless catalog")
@click.option("-s", "--search", help="Only models whose id contains this text.")
@click.option("--page", type=click.IntRange(1), default=1, show_default=True)
@click.option("--limit", type=click.IntRange(1, 200), default=25, show_default=True)
@pass_app
def models(app: App, search: Optional[str], page: int, limit: int) -> None:
    """Search the catalog of deployable models with their prices (no sign-in needed)."""
    d = app.api.get("/v1/deploy/models", params={"search": search, "page": page, "limit": limit}, auth=False, org=False) or {}

    def render(d: Any) -> None:
        rows = []
        meta = meta_of(d)
        for m in deployments_of(d):
            ours = m.get("our_pricing_per_million") or {}
            rows.append((m.get("featherless_model_id") or m.get("id"), f"{int(m['context_length']):,}" if m.get("context_length") else "—",
                         money(ours.get("input")), money(ours.get("output"))))
        ui.console.print(ui.make_table(["Model", "Context", "In $/M", "Out $/M"], rows,
                                       title=f"Featherless models · {meta.get('total', len(rows)):,} found · page {meta.get('page', page)}/{meta.get('total_pages', 1)}",
                                       right=["Context", "In $/M", "Out $/M"]))

    app.out(d, render)


@group.command("status", short_help="Is a model warm or cold?")
@click.argument("model")
@pass_app
def status(app: App, model: str) -> None:
    """Whether MODEL is loaded (warm), loading, or cold. Cold models take 5-15 minutes on the first request."""
    d = app.api.get(f"/v1/deploy/model-status/{bare(model)}", auth=False, org=False) or {}
    app.out(d, lambda x: ui.console.print(ui.kv_panel(bare(model), [("State", state_text(x.get("state")))] +
                                                      [(k, v) for k, v in x.items() if k != "state" and not isinstance(v, (dict, list))])))


@group.command("list", short_help="Deployments in the current context")
@click.option("--all", "show_all", is_flag=True, help="Include cancelled, expired and removed deployments.")
@pass_app
def list_(app: App, show_all: bool) -> None:
    """List deployments for the active context (organization, or personal with --personal)."""
    auth(app)
    d = app.api.get(base(app), org=False) or {}
    rows = deployments_of(d)
    shown = rows if show_all else [x for x in rows if x.get("status") in LIVE]

    def render(_: Any) -> None:
        if not shown:
            ui.info(f"No {'' if show_all else 'active '}deployments for {owner_label(app)}. Deploy one with `ipx featherless deploy <model>`.")
            return
        ui.console.print(ui.make_table(
            ["ID", "Call as", "Status", "State", "Fee", "Renews"],
            [(x.get("id"), x.get("display_model_id") or hf(str(x.get("featherless_model_id") or "")), status_text(x.get("status")), state_text(x.get("model_state")) if x.get("model_state") else "—",
              money(x.get("monthly_fee")), date(x.get("billing_cycle_end"))) for x in shown],
            title=f"Featherless deployments · {owner_label(app)} ({len(shown)})"))

    app.out(d if show_all else {**meta_of(d), "data": shown, "total": len(shown)}, render)


@group.command("get", short_help="Show one deployment")
@click.argument("deployment_id")
@pass_app
def get(app: App, deployment_id: str) -> None:
    """Details of a deployment: pricing, billing cycle and model state."""
    auth(app)
    d = app.api.get(f"{base(app)}/{deployment_id}", org=False) or {}
    app.out(d, show_deployment)


@group.command("deploy", short_help="Deploy a model", examples=["ipx featherless deploy Qwen/Qwen2.5-7B-Instruct", "ipx -O acme featherless deploy hf/Qwen/Qwen2.5-7B-Instruct -y"])
@click.argument("model")
@pass_app
def deploy(app: App, model: str) -> None:
    """Deploy MODEL (Author/Model) for $0.50/month, charged now to your wallet or the organization's."""
    auth(app)
    mid = hf(model)
    state = None
    try:
        state = (app.api.get(f"/v1/deploy/model-status/{bare(mid)}", auth=False, org=False) or {}).get("state")
    except IpxError:
        pass
    if not app.json:
        ui.console.print(f"Deploying [bold]{bare(mid)}[/bold] for {owner_label(app)}"
                         + (f" · currently [bold]{state}[/bold]" if state else "") + ".")
        if state in ("cold", "loading"):
            ui.warn("This model is not loaded yet; the first request can take 5-15 minutes.")
    if not ui.confirm(f"Charge the $0.50 monthly fee to {owner_label(app)}?", default=False):
        raise click.Abort()
    d = app.api.post(base(app), {"model_id": mid}, org=False, timeout=180) or {}

    def render(x: Dict[str, Any]) -> None:
        ui.ok(x.get("message") or f"Deployed {bare(mid)}.")
        if x.get("deployment"):
            show_deployment(x["deployment"])
        if x.get("disclaimer"):
            ui.dim(x["disclaimer"])

    app.out(d, render)


def _action(name: str, verb: str, method: str, suffix: str, past: str, confirm: Optional[str] = None) -> None:
    @group.command(name, short_help=f"{verb} a deployment")
    @click.argument("deployment_id")
    @pass_app
    def _cmd(app: App, deployment_id: str) -> None:
        auth(app)
        if confirm and not ui.confirm(confirm.format(id=deployment_id, owner=owner_label(app))):
            raise click.Abort()
        r = app.api.request(method, f"{base(app)}/{deployment_id}{suffix}", json={} if method == "POST" else None, org=False)
        d = r.json() if r.body else {}
        app.out(d, lambda x: ui.ok((x or {}).get("message") or past))

    _cmd.__doc__ = {
        "cancel": "Stop renewing a deployment. Access continues until the end of the billing cycle.",
        "restore": "Undo a cancel while the deployment is still in its paid cycle.",
        "terminate": "End a deployment immediately. This cannot be undone and the fee is not refunded.",
    }[name]


_action("cancel", "Cancel", "POST", "/cancel", "Deployment cancelled.", "Cancel deployment {id} for {owner} at the end of its cycle?")
_action("restore", "Restore", "POST", "/uncancel", "Deployment restored.")
_action("terminate", "Terminate", "DELETE", "", "Deployment terminated.", "Terminate deployment {id} for {owner} right now? This cannot be undone.")
group.alias("uncancel", "restore")
group.alias("delete", "terminate")


@group.command("usage", short_help="Tokens and credits used by a deployment")
@click.argument("deployment_id")
@pass_app
def usage(app: App, deployment_id: str) -> None:
    """Token usage and credits charged for a deployment in its current history."""
    auth(app)
    d = app.api.get(f"{base(app)}/{deployment_id}/usage", org=False) or {}
    app.out(d, lambda x: ui.console.print(ui.kv_panel(x.get("featherless_model_id") or "Deployment usage", [
        ("Call as", x.get("display_model_id")), ("Status", status_text(x.get("status"))),
        ("Input tokens", ui.fmt_num(x.get("total_input_tokens"))), ("Output tokens", ui.fmt_num(x.get("total_output_tokens"))),
        ("Credits charged", ui.fmt_credits(x.get("total_credits_charged"))),
        ("Cycle", f"{date(x.get('billing_cycle_start'))} → {date(x.get('billing_cycle_end'))}")])))


COMMANDS = [group]
