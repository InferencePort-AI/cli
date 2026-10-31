"""ipx bucket: discovery for the gated InferencePort Buckets rollout."""

from __future__ import annotations

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup


@click.group("bucket", cls=IpxGroup, section="Cloud", short_help="Buckets availability and retail pricing")
def group() -> None:
    """Inspect Buckets availability and the published Azure retail catalog.

    Object storage is not available yet. Creation, transfers and S3 credential
    issuance will be added after server security and billing validation.
    """


@group.command("status")
@pass_app
def status(app: App) -> None:
    """Check availability without creating buckets or credentials."""
    data = app.api.get("/v1/buckets/status", auth=False, org=False)
    app.out(data, lambda d: ui.console.print(ui.kv_panel("InferencePort Buckets", [
        ("Availability", str(d.get("status", "unknown")).replace("_", " ")),
        ("S3 endpoint", d.get("endpoint", "unavailable")),
        ("SDK compatibility", d.get("sdk_compatibility", "unverified")),
    ])))


@group.command("pricing")
@pass_app
def pricing(app: App) -> None:
    """Read rate availability; no estimated or fallback prices are supplied."""
    data = app.api.get("/v1/buckets/pricing", auth=False, org=False)

    def render(d: dict) -> None:
        catalog = d.get("pricing") or {}
        ui.console.print(ui.kv_panel("Buckets retail catalog", [
            ("Availability", catalog.get("status", "unavailable")),
            ("Currency", catalog.get("currency", "USD")),
            ("Active rate version", catalog.get("rate_version") or "unavailable"),
        ]))
        if not catalog.get("meters"):
            ui.console.print("No verified Azure retail rates are published yet.")

    app.out(data, render)


COMMANDS = [group]
