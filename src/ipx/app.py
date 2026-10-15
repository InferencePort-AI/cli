"""Shared runtime context handed to every command, plus small helpers commands reuse."""

from __future__ import annotations

from typing import Any, Optional

import click

from ipx import auth as auth_mod
from ipx import ui
from ipx.config import Config, Credentials
from ipx.errors import IpxError, NeedsOrg, NotSignedIn
from ipx.http import Transport


class App:
    """Lazy holder for config, credentials and the HTTP transport."""

    def __init__(self, *, json_out: bool = False, org: Optional[str] = None, personal: bool = False,
                 base_url: Optional[str] = None) -> None:
        self.json = json_out
        self._org_override = org.strip().lower() if org else None
        self.personal = personal
        self._base_url = base_url
        self.cfg = Config()
        self.creds = Credentials()
        self._transport: Optional[Transport] = None

    # -- org -----------------------------------------------------------------
    @property
    def org(self) -> Optional[str]:
        if self.personal:
            return None
        return self._org_override or self.cfg.org

    def require_org(self) -> str:
        org = self._org_override or self.cfg.org
        if not org:
            raise NeedsOrg()
        return org

    # -- http ----------------------------------------------------------------
    @property
    def api(self) -> Transport:
        if self._transport is None:
            self._transport = Transport(
                self.cfg, self.creds, org=self._org_override, personal=self.personal,
                base_url=self._base_url, refresher=auth_mod.refresh,
            )
        return self._transport

    def require_login(self) -> None:
        if not self.creds.signed_in:
            raise NotSignedIn()

    def require_jwt(self, what: str = "This command") -> None:
        self.require_login()
        if self.creds.auth_type == "api_key":
            raise IpxError(
                f"{what} needs a signed-in session, but you are using an API key.",
                "Run `ipx auth login` with email or OAuth. API keys only work for generation and org-scoped routes.",
            )

    # -- output --------------------------------------------------------------
    def out(self, data: Any, render=None) -> None:
        """Print `data` as JSON in --json mode, otherwise call `render(data)`."""
        if self.json:
            ui.emit_json(data)
        elif render is not None:
            render(data)


pass_app = click.make_pass_decorator(App)


def org_path(app: App, suffix: str = "") -> str:
    """/v1/orgs/<slug><suffix> for the active org context."""
    return f"/v1/orgs/{app.require_org()}{suffix}"
