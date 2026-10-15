"""ipx auth - sign in, sign out, inspect the session and choose an organization context."""

from __future__ import annotations

import datetime as dt
import sys
import threading
from typing import Any, Dict, List, Optional

import click
from rich.text import Text

from ipx import auth as A
from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import ApiError, IpxError


@click.group("auth", cls=IpxGroup, section="Account", short_help="Sign in/out, session status and organization context")
def group() -> None:
    """Manage your InferencePort AI identity.

    Sign in with email and password, a browser OAuth flow (GitHub, Google, Microsoft, Hugging Face), your
    organization's single sign-on, or a Lightning API key. `ipx auth context` selects the organization that
    `ipx org ...` commands act on and that cloud usage is billed to.
    """


def _read_password(from_stdin: bool, given: Optional[str]) -> str:
    if given:
        return given
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return ui.ask("Password", password=True)


@group.command("login", examples=[
    "ipx auth login",
    "ipx auth login --email me@example.com",
    "ipx auth login --provider github",
    "ipx auth login --sso me@company.com",
    "ipx auth login --api-key ipa_live_xxxxxxxx",
    "echo $PASSWORD | ipx auth login --email me@example.com --password-stdin",
])
@click.option("--email", help="Email address for password sign-in.")
@click.option("--password", help="Password (prefer --password-stdin; avoid shell history).")
@click.option("--password-stdin", is_flag=True, help="Read the password from standard input.")
@click.option("--api-key", help="Sign in with a Lightning API key (ipa_live_...).")
@click.option("--provider", type=click.Choice(sorted(A.OAUTH_PROVIDERS)), help="OAuth provider (opens your browser).")
@click.option("--sso", "sso_email", metavar="EMAIL", help="Single sign-on using your work email.")
@click.option("--paste", is_flag=True, help="Paste the browser redirect URL instead of using the local listener.")
@click.option("--no-browser", is_flag=True, help="Print the sign-in URL instead of opening a browser.")
@pass_app
def login(app: App, email, password, password_stdin, api_key, provider, sso_email, paste, no_browser) -> None:
    """Sign in to InferencePort AI."""
    method = None
    if api_key:
        method = "api_key"
    elif provider:
        method = "oauth"
    elif sso_email:
        method = "sso"
    elif email or password_stdin or password:
        method = "password"
    else:
        ui.print_banner("sign in")
        method = ui.choose("How would you like to sign in?", [
            ("password", "Email & password"),
            ("github", "GitHub"),
            ("google", "Google"),
            ("microsoft", "Microsoft"),
            ("huggingface", "Hugging Face"),
            ("sso", "Single sign-on (work email)"),
            ("api_key", "API key"),
        ])
        if method in A.OAUTH_PROVIDERS:
            provider, method = method, "oauth"

    if method == "api_key":
        key = api_key or ui.ask("API key (ipa_live_…)", password=True)
        with ui.status("Verifying API key…"):
            me = A.api_key_login(app.cfg, app.creds, key)
        who = ((me.get("identity") or {}).get("email")) or "your account"
        _signed_in(app, who, "API key")
        return

    if method == "password":
        email = email or ui.ask("Email")
        pw = _read_password(password_stdin, password)
        with ui.status("Signing in…"):
            session = A.password_login(app.cfg, app.creds, email, pw)
        _signed_in(app, (session.get("user") or {}).get("email") or email, "Email & password")
        return

    # browser based: oauth / sso
    if method == "sso":
        sso_email = sso_email or ui.ask("Work email")
        build = lambda redirect: A.sso_url(app.cfg, sso_email, redirect)  # noqa: E731
        label = "Single sign-on"
    else:
        build = lambda redirect: A.oauth_url(app.cfg, provider, redirect)  # noqa: E731
        label = A.OAUTH_PROVIDERS[provider]

    if paste:
        url = build(A.CALLBACK_PAGE)
        ui.console.print(f"\n[accent]1.[/accent] Open this link and sign in:\n\n   [underline]{url}[/underline]\n")
        ui.console.print("[accent]2.[/accent] When the page finishes, copy the [bold]full address[/bold] from your browser and paste it here.\n")
        session = A.parse_callback(ui.ask("Redirect URL"))
    else:
        def show(url: str) -> None:
            ui.console.print(f"[muted]Opening your browser to sign in with {label}…[/muted]")
            ui.console.print(f"[muted]If it does not open, visit:[/muted]\n  [underline]{url}[/underline]\n")

        def nudge() -> None:
            ui.console.print("[muted]Still waiting. If the browser is on another machine or never returns here, press Ctrl+C and "
                             "run again with --paste.[/muted]")

        timer = threading.Timer(30.0, nudge)
        timer.daemon = True
        timer.start()
        try:
            with ui.status(f"Waiting for {label} sign-in in your browser… (Ctrl+C to cancel)"):
                session = A.browser_login(app.cfg, build, open_browser=not no_browser, on_url=show)
        finally:
            timer.cancel()
    with ui.status("Finishing sign-in…"):
        user = A.complete_session(app.cfg, app.creds, session)
    _signed_in(app, user.get("email") or "your account", label)


def _signed_in(app: App, who: str, via: str) -> None:
    if app.json:
        ui.emit_json({"signed_in": True, "email": who, "method": via})
        return
    ui.ok(f"Signed in as [bold]{who}[/bold] [muted]via {via}[/muted]")
    if not app.cfg.org:
        ui.dim("Tip: pick an organization with `ipx auth context <org>` · try `ipx gen chat` to start chatting.")


@group.command("logout", short_help="Sign out and forget stored credentials")
@pass_app
def logout(app: App) -> None:
    """Sign out and remove stored credentials (the org context is kept)."""
    if not app.creds.data:
        ui.info("You were not signed in.")
        return
    with ui.status("Signing out…"):
        A.sign_out(app.cfg, app.creds)
    app.out({"signed_in": False}, lambda _: ui.ok("Signed out. Local credentials removed."))


@group.command("status", short_help="Show who you are signed in as")
@pass_app
def status(app: App) -> None:
    """Show the active identity, session expiry, plan and organization context."""
    c = app.creds
    if not c.signed_in:
        app.out({"signed_in": False}, lambda _: ui.fail("Not signed in.", "Run `ipx auth login`."))
        sys.exit(1)
    plan: Dict[str, Any] = {}
    try:
        plan = app.api.get("/subscription", org=False) or {}
    except IpxError:
        pass
    exp = None
    if c.auth_type == "jwt" and c.expires_at:
        exp = dt.datetime.fromtimestamp(c.expires_at).strftime("%Y-%m-%d %H:%M")
    data = {
        "signed_in": True,
        "email": plan.get("email") or c.email,
        "auth_type": c.auth_type,
        "plan": plan.get("plan_name") or plan.get("plan_key"),
        "session_expires": exp,
        "org": app.org,
        "api": app.cfg.base_url,
    }

    def render(d: Dict[str, Any]) -> None:
        rows = [
            ("Account", d["email"]),
            ("Sign-in", "API key" if d["auth_type"] == "api_key" else (c.data.get("provider") or "session").title()),
            ("Plan", d["plan"]),
            ("Session", f"refreshes automatically · token valid until {exp}" if exp else ("never expires (revoke in console)" if d["auth_type"] == "api_key" else None)),
            ("Organization", Text(d["org"], style="bold " + ui.VIOLET) if d["org"] else Text("personal (no org context)", style="muted")),
            ("API", d["api"]),
        ]
        ui.console.print(ui.kv_panel("Session", rows))

    app.out(data, render)


group.alias("whoami", "status")
group.alias("signout", "logout")


@group.command("token", short_help="Print the current access token")
@pass_app
def token(app: App) -> None:
    """Print the bearer token for use with curl or other tools. Treat it like a password."""
    app.require_login()
    if app.creds.expiring():
        A.refresh(app.cfg, app.creds)
    sys.stdout.write((app.creds.access_token or "") + "\n")


def _list_orgs(app: App) -> List[Dict[str, Any]]:
    app.require_jwt("Listing organizations")
    data = app.api.get("/v1/orgs", org=False) or {}
    return list(data.get("orgs") or [])


def resolve_org(app: App, ref: str) -> Dict[str, Any]:
    ref_l = ref.strip().lower()
    orgs = _list_orgs(app)
    for o in orgs:
        if ref_l in (str(o.get("slug", "")).lower(), str(o.get("id", "")).lower()):
            return o
    for o in orgs:
        if ref_l == str(o.get("name", "")).lower():
            return o
    names = ", ".join(o.get("slug", "?") for o in orgs) or "none"
    raise IpxError(f"You are not a member of an organization matching “{ref}”.", f"Your organizations: {names}")


@group.command("context", examples=["ipx auth context", "ipx auth context acme", "ipx auth context --clear"],
               short_help="Show or set the active organization")
@click.argument("org", required=False)
@click.option("--clear", is_flag=True, help="Return to your personal account.")
@pass_app
def context(app: App, org: Optional[str], clear: bool) -> None:
    """Show or set the organization that org commands and cloud usage apply to.

    ORG may be a slug, id or name. With no argument, shows the current context (and offers a picker in a terminal).
    """
    if clear:
        app.cfg.set_org(None)
        app.out({"org": None}, lambda _: ui.ok("Context cleared — using your personal account."))
        return
    if not org:
        current = app.cfg.org
        if app.json or not ui.is_tty() or not app.creds.signed_in or app.creds.auth_type == "api_key":
            app.out({"org": current}, lambda _: ui.console.print(
                f"Organization context: [bold {ui.VIOLET}]{current}[/]" if current else "[muted]No organization context (personal account).[/muted]"))
            return
        orgs = _list_orgs(app)
        if not orgs:
            ui.info("You are not a member of any organization yet. Create one with `ipx org create`.")
            return
        opts = [("", "Personal account")] + [(o["slug"], f"{o['name']} [muted]({o['slug']} · {o.get('role')})[/muted]") for o in orgs]
        default = next((i for i, (v, _) in enumerate(opts) if v == current), 0)
        org = ui.choose("Which context should ipx use?", opts, default)
        if not org:
            app.cfg.set_org(None)
            ui.ok("Using your personal account.")
            return
    if app.creds.auth_type == "api_key":
        try:
            app.api.get(f"/v1/orgs/{org.lower()}", org=False)
        except ApiError as e:
            raise IpxError(f"Could not access organization “{org}”: {e.message}", "Check the slug and that your API key belongs to it.")
        slug = org.lower()
        info: Dict[str, Any] = {"slug": slug, "name": slug}
    else:
        info = resolve_org(app, org)
        slug = info["slug"]
    app.cfg.set_org(slug)
    app.out({"org": slug, "role": info.get("role")}, lambda _: ui.ok(
        f"Context set to [bold {ui.VIOLET}]{info.get('name', slug)}[/] [muted]({slug}{' · ' + info['role'] if info.get('role') else ''})[/muted]"))
