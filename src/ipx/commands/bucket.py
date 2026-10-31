"""ipx bucket: discovery for the gated InferencePort Buckets rollout."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote
from xml.etree.ElementTree import fromstring

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import IpxError


@click.group("bucket", cls=IpxGroup, section="Cloud", short_help="Buckets availability and retail pricing")
def group() -> None:
    """Manage private Buckets, stream files and issue scoped S3 credentials.

    Uses your saved account and the global --org / --personal context.
    Requires a server with Buckets beta access enabled.
    """


@group.command("status")
@pass_app
def status(app: App) -> None:
    """Check availability without creating buckets or credentials."""
    data = app.api.get("/v1/buckets/status", auth=False, org=False)
    app.out(
        data,
        lambda d: ui.console.print(
            ui.kv_panel(
                "InferencePort Buckets",
                [
                    ("Availability", str(d.get("status", "unknown")).replace("_", " ")),
                    ("S3 endpoint", d.get("endpoint", "unavailable")),
                    ("SDK compatibility", d.get("sdk_compatibility", "unverified")),
                ],
            )
        ),
    )


@group.command("pricing")
@pass_app
def pricing(app: App) -> None:
    """Read rate availability; no estimated or fallback prices are supplied."""
    data = app.api.get("/v1/buckets/pricing", auth=False, org=False)

    def render(d: dict) -> None:
        catalog = d.get("pricing") or {}
        ui.console.print(
            ui.kv_panel(
                "Buckets retail catalog",
                [
                    (
                        "Availability",
                        catalog.get("status", "verified" if catalog.get("meters") else "unavailable"),
                    ),
                    ("Currency", catalog.get("currency", "USD")),
                    ("Active rate version", catalog.get("rate_version") or "unavailable"),
                ],
            )
        )
        if not catalog.get("meters"):
            ui.console.print("No verified Azure retail rates are published yet.")

    app.out(data, render)


def output(app, value):
    app.out(value, lambda data: ui.console.print_json(json.dumps(data)))


def lookup(app, name):
    token = None
    while True:
        page = app.api.get("/v1/buckets", params={"continuation_token": token})
        for row in page["buckets"]:
            if row["name"] == name or row["id"] == name:
                return row
        token = page.get("continuation_token")
        if not token:
            break
    raise IpxError("Bucket not found in the active workspace.")


def target(name, key=""):
    return "/s3/" + quote(name, safe="") + ("/" + quote(key, safe="").replace(".", "%2E") if key else "")


@group.command("create")
@click.argument("name")
@click.option("--quota-bytes", type=click.IntRange(1, 1024**4), default=10 * 1024**3)
@pass_app
def create(app, name, quota_bytes):
    """Create a logical bucket in the active workspace."""
    output(app, app.api.post("/v1/buckets", {"name": name, "quota_bytes": quota_bytes}))


@group.command("list")
@click.option("--continuation-token")
@click.option("--limit", type=click.IntRange(1, 1000), default=100)
@pass_app
def list_buckets(app, continuation_token, limit):
    """List buckets visible to the active account or organization."""
    output(app, app.api.get("/v1/buckets", params={"limit": limit, "continuation_token": continuation_token}))


@group.command("info")
@click.argument("name")
@pass_app
def info(app, name):
    """Show bucket metadata and quota."""
    output(app, lookup(app, name))


@group.command("delete")
@click.argument("name")
@pass_app
def delete(app, name):
    """Delete an empty logical bucket; active uploads prevent deletion."""
    bucket = lookup(app, name)
    if not ui.confirm("Delete empty bucket " + bucket["name"] + "?"):
        return
    app.api.delete("/v1/buckets/" + bucket["id"])
    output(app, {"deleted": bucket["name"]})


@group.command("usage")
@click.argument("name")
@pass_app
def usage(app, name):
    """Read storage, Azure operation counters and settled costs."""
    bucket = lookup(app, name)
    output(app, app.api.get("/v1/buckets/" + bucket["id"] + "/usage"))


@group.command("ls")
@click.argument("name")
@click.option("--prefix", default="")
@click.option("--delimiter", default="")
@click.option("--limit", type=click.IntRange(1, 1000), default=100)
@click.option("--continuation-token")
@pass_app
def ls(app, name, prefix, delimiter, limit, continuation_token):
    """List one page of objects with an opaque continuation token."""
    bucket = lookup(app, name)
    output(
        app,
        app.api.get(
            "/v1/buckets/" + bucket["id"] + "/objects",
            params={
                "prefix": prefix,
                "delimiter": delimiter,
                "max_keys": limit,
                "continuation_token": continuation_token,
            },
        ),
    )


@group.command("upload")
@click.argument("name")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--key", help="Opaque object key (default: filename).")
@pass_app
def upload(app, name, file, key):
    """Upload with bounded 64 MiB parts; incomplete multipart uploads are aborted."""
    from ipx.http import guess_type

    bucket = lookup(app, name)
    key = key or file.name
    path = target(bucket["name"], key)
    size = file.stat().st_size
    chunk_size = 64 * 1024**2

    def field(root, name):
        return next((node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == name), None)

    with file.open("rb") as source:
        if size <= chunk_size:
            response = app.api.request(
                "PUT", path, data=source.read(), headers={"Content-Type": guess_type(str(file))}
            )
            output(app, {"key": key, "bytes": size, "etag": response.headers.get("etag")})
            return
        initiated = app.api.request(
            "POST", path + "?uploads", headers={"Content-Type": guess_type(str(file))}
        )
        identifier = field(fromstring(initiated.body), "UploadId")
        parts = []
        try:
            number = 1
            while True:
                data = source.read(chunk_size)
                if not data:
                    break
                response = app.api.request(
                    "PUT",
                    path,
                    params={"uploadId": identifier, "partNumber": number},
                    data=data,
                    headers={"Content-Length": str(len(data))},
                    timeout=600,
                )
                parts.append((number, response.headers["etag"]))
                number += 1
            from xml.sax.saxutils import escape

            body = (
                "<CompleteMultipartUpload>"
                + "".join(
                    f"<Part><PartNumber>{n}</PartNumber><ETag>{escape(etag)}</ETag></Part>"
                    for n, etag in parts
                )
                + "</CompleteMultipartUpload>"
            )
            response = app.api.request(
                "POST",
                path,
                params={"uploadId": identifier},
                data=body.encode(),
                headers={"Content-Type": "application/xml"},
            )
            output(app, {"key": key, "bytes": size, "etag": field(fromstring(response.body), "ETag")})
        except BaseException:
            app.api.request("DELETE", path, params={"uploadId": identifier})
            raise


@group.command("download")
@click.argument("name")
@click.argument("key")
@click.argument("destination", type=click.Path(dir_okay=False, path_type=Path))
@pass_app
def download(app, name, key, destination):
    """Stream an object into a new file without buffering it in memory."""
    if destination.exists():
        raise IpxError("Destination already exists; choose a new path.")
    bucket = lookup(app, name)
    response = app.api.request(
        "GET", target(bucket["name"], key), stream=True, headers={"Accept": "application/octet-stream"}
    )
    created = False
    try:
        with destination.open("xb") as file:
            created = True
            for chunk in response.iter_bytes():
                file.write(chunk)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        response.close()
    output(app, {"downloaded": str(destination)})


@group.command("rm")
@click.argument("name")
@click.argument("key")
@pass_app
def rm(app, name, key):
    """Remove one object key; retained Azure versions can remain billable."""
    bucket = lookup(app, name)
    app.api.request("DELETE", target(bucket["name"], key))
    output(app, {"deleted": key})


@group.group("credentials")
def credentials():
    """Issue, list and revoke separate S3 signing credentials."""


@credentials.command("list")
@pass_app
def credential_list(app):
    output(app, app.api.get("/v1/buckets/s3-credentials"))


@credentials.command("create")
@click.option("--read-only", is_flag=True)
@click.option("--bucket-id", multiple=True)
@click.option("--prefix", multiple=True)
@click.option("--expires-in", type=click.IntRange(60, 90 * 86400), default=30 * 86400)
@click.option(
    "--output",
    "destination",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="New private JSON file for the reveal-once secret.",
)
@pass_app
def credential_create(app, read_only, bucket_id, prefix, expires_in, destination):
    # Reserve the private file before issuance, so an existing file never loses a secret.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = app.api.post(
            "/v1/buckets/s3-credentials",
            {
                "permissions": ["storage.read"] if read_only else ["storage.read", "storage.write"],
                "bucket_ids": list(bucket_id) or None,
                "key_prefixes": list(prefix) or None,
                "expires_in": expires_in,
            },
        )
        with os.fdopen(fd, "w") as file:
            fd = None
            json.dump(data, file, indent=2)
        output(app, {"id": data["id"], "saved_to": str(destination), "expires_at": data["expires_at"]})
    except BaseException:
        if fd is not None:
            os.close(fd)
        destination.unlink(missing_ok=True)
        raise


@credentials.command("rotate")
@click.argument("identifier")
@click.option(
    "--revoke-previous",
    is_flag=True,
    help="Immediately revoke the old credential after issuing its replacement.",
)
@click.option("--output", "destination", required=True, type=click.Path(dir_okay=False, path_type=Path))
@pass_app
def credential_rotate(app, identifier, revoke_previous, destination):
    """Save a reveal-once replacement with the same permissions and bucket scopes."""
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = app.api.post(
            "/v1/buckets/s3-credentials/" + quote(identifier, safe="") + "/rotate",
            {"revoke_previous": revoke_previous},
        )
        with os.fdopen(fd, "w") as file:
            fd = None
            json.dump(data, file, indent=2)
        output(
            app,
            {"id": data["id"], "saved_to": str(destination), "previous_revoked": data["previous_revoked"]},
        )
    except BaseException:
        if fd is not None:
            os.close(fd)
        destination.unlink(missing_ok=True)
        raise


@credentials.command("revoke")
@click.argument("identifier")
@pass_app
def credential_revoke(app, identifier):
    app.api.delete("/v1/buckets/s3-credentials/" + quote(identifier, safe=""))
    output(app, {"revoked": identifier})


COMMANDS = [group]
