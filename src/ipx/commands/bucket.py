"""ipx bucket: discovery for the gated InferencePort Buckets rollout."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import fromstring

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.errors import ApiError, IpxError

PART_SIZE = 64 * 1024**2
MAX_PARTS = 10_000
TRANSFER_TIMEOUT = 600.0


@click.group("bucket", cls=IpxGroup, section="Cloud", short_help="Buckets availability and pricing")
def group() -> None:
    """Manage private Buckets, stream files and issue scoped S3 credentials.

    Uses your saved account and the global --org / --personal context.
    Requires a server with Buckets enabled.
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
        meters = catalog.get("meters") or []
        ui.console.print(
            ui.kv_panel(
                "Buckets pricing",
                [
                    (
                        "Availability",
                        catalog.get("status", "verified" if meters else "unavailable"),
                    ),
                    ("Currency", catalog.get("currency", "USD")),
                    ("Active rate version", catalog.get("rate_version") or "unavailable"),
                ],
            )
        )
        if meters:
            rows = []
            for meter in meters:
                if not isinstance(meter, dict):
                    rows.append((str(meter), ""))
                    continue
                label = meter.get("meter") or meter.get("name") or meter.get("id") or "meter"
                details = ", ".join(
                    f"{key.replace('_', ' ')}: {value}"
                    for key, value in meter.items()
                    if key not in ("meter", "name", "id")
                )
                rows.append((str(label), details or "—"))
            ui.console.print(ui.make_table(("Meter", "Rate"), rows, title="Meter rates"))
        else:
            ui.console.print("Storage pricing is currently unavailable.")
    app.out(data, render)


def output(app, value):
    app.out(value, lambda data: ui.console.print_json(json.dumps(data)))


def lookup(app, name):
    """Resolve an exact bucket name first, then fall back to a bucket id."""
    token = None
    id_match = None
    while True:
        page = app.api.get("/v1/buckets", params={"continuation_token": token})
        for row in page["buckets"]:
            if row["name"] == name:
                return row
            if id_match is None and row["id"] == name:
                id_match = row
        token = page.get("continuation_token")
        if not token:
            break
    if id_match is not None:
        return id_match
    raise IpxError("Bucket not found in the active workspace.")


def _require_secure_credentials(app: App) -> None:
    url = getattr(app.api, "base_url", "") or ""
    parts = urlsplit(url)
    if parts.scheme != "https" and (parts.hostname or "") not in {"localhost", "127.0.0.1", "::1"}:
        raise IpxError(
            f"Refusing to send S3 credentials to {url or 'the API'} over plain HTTP.",
            "Use an https:// base URL (`ipx config set base_url https://…`, IPX_BASE_URL or --base-url).",
        )


def _restrict_private_file(path: Path) -> None:
    """Apply an owner-only ACL on Windows, where 0o600 is not enforced."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME") or getpass.getuser()
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise IpxError(f"Could not restrict access to {path}: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "icacls failed").strip()[:300]
        raise IpxError(f"Could not restrict access to {path}.", detail)


def _reserve_private_file(destination: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(destination, flags, 0o600)
    try:
        _restrict_private_file(destination)
    except BaseException:
        _close_fd(fd)
        destination.unlink(missing_ok=True)
        raise
    return fd


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def _save_credential(app, destination, issue, describe, *, revoke_on_failure):
    """Issue a credential and save its reveal-once secret to a new private file."""
    _require_secure_credentials(app)
    fd = _reserve_private_file(destination)
    data = None
    saved = False
    try:
        data = issue()
        _write_all(fd, json.dumps(data, indent=2).encode())
        saved = True
        _close_fd(fd)
        fd = None
        output(app, describe(data))
    except BaseException:
        if fd is not None:
            _close_fd(fd)
        if data is None:
            destination.unlink(missing_ok=True)
        elif not saved:
            if revoke_on_failure:
                try:
                    app.api.delete("/v1/buckets/s3-credentials/" + quote(str(data["id"]), safe=""))
                except BaseException:
                    ui.warn(
                        f"The new credential could not be revoked; its secret may be incomplete at {destination}."
                    )
                else:
                    destination.unlink(missing_ok=True)
            else:
                ui.warn(
                    f"The replacement credential was issued but its secret could not be saved completely; "
                    f"inspect {destination} before retrying."
                )
        raise


def _part_size(size: int) -> int:
    """Keep multipart uploads at or below S3's 10,000-part limit."""
    needed = max(1, -(-size // MAX_PARTS))
    mib = 1024**2
    return max(PART_SIZE, -(-needed // mib) * mib)


def _find_object(app, bucket, key):
    token = None
    while True:
        page = app.api.get(
            "/v1/buckets/" + quote(str(bucket["id"]), safe="") + "/objects",
            params={"prefix": key, "max_keys": 1000, "continuation_token": token},
        )
        for obj in page.get("objects") or []:
            if obj.get("key") == key:
                return obj
        token = page.get("continuation_token")
        if not token:
            return None


def _committed_object(app, bucket, key, size=None):
    try:
        obj = _find_object(app, bucket, key)
    except IpxError:
        return None
    if obj is not None and size is not None and obj.get("size") not in (None, size):
        return None
    return obj


def _report_object(app, key, size, obj):
    output(app, {"key": key, "bytes": obj.get("size", size), "etag": obj.get("etag")})


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
    """Read storage, operation counters and settled costs."""
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
    """Upload with bounded parts; incomplete multipart uploads are aborted."""
    from ipx.http import guess_type

    bucket = lookup(app, name)
    key = key or file.name
    path = target(bucket["name"], key)
    size = file.stat().st_size
    chunk_size = _part_size(size)

    def field(root, name):
        return next((node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == name), None)

    with file.open("rb") as source:
        if size <= chunk_size:
            try:
                response = app.api.request(
                    "PUT",
                    path,
                    data=source.read(),
                    headers={"Content-Type": guess_type(str(file))},
                    timeout=TRANSFER_TIMEOUT,
                )
            except IpxError as exc:
                if isinstance(exc, ApiError) and 400 <= exc.status < 500:
                    raise
                committed = _committed_object(app, bucket, key, size)
                if committed is None:
                    raise
                _report_object(app, key, size, committed)
                return
            output(app, {"key": key, "bytes": size, "etag": response.headers.get("etag")})
            return
        initiated = app.api.request(
            "POST",
            path + "?uploads",
            headers={"Content-Type": guess_type(str(file))},
            timeout=TRANSFER_TIMEOUT,
        )
        identifier = None
        committed = False
        try:
            try:
                identifier = field(fromstring(initiated.body), "UploadId")
            except Exception as e:
                raise IpxError(
                    "The bucket service returned a malformed multipart upload response.",
                    "Retry the upload; an incomplete upload may remain on the service.",
                ) from e
            if not identifier:
                raise IpxError(
                    "The bucket service did not return a multipart upload id.",
                    "Retry the upload; an incomplete upload may remain on the service.",
                )
            parts = []
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
                    timeout=TRANSFER_TIMEOUT,
                )
                etag = response.headers.get("etag")
                if not etag:
                    raise IpxError("The bucket service returned a part response without an ETag.")
                parts.append((number, etag))
                number += 1
            if not parts:
                raise IpxError("The file could not be read for upload.")
            from xml.sax.saxutils import escape

            body = (
                "<CompleteMultipartUpload>"
                + "".join(
                    f"<Part><PartNumber>{n}</PartNumber><ETag>{escape(etag)}</ETag></Part>"
                    for n, etag in parts
                )
                + "</CompleteMultipartUpload>"
            )
            try:
                response = app.api.request(
                    "POST",
                    path,
                    params={"uploadId": identifier},
                    data=body.encode(),
                    headers={"Content-Type": "application/xml"},
                    timeout=TRANSFER_TIMEOUT,
                )
            except IpxError as exc:
                if isinstance(exc, ApiError) and 400 <= exc.status < 500:
                    raise
                completed = _committed_object(app, bucket, key, size)
                if completed is None:
                    raise
                committed = True
                _report_object(app, key, size, completed)
                return
            root = fromstring(response.body)
            if root.tag.rsplit("}", 1)[-1] == "Error":
                message = field(root, "Message") or field(root, "Code") or "unknown error"
                raise IpxError(f"Multipart upload failed: {message}")
            etag = field(root, "ETag")
            if not etag:
                raise IpxError("The bucket service did not confirm the multipart upload.")
            committed = True
            output(app, {"key": key, "bytes": size, "etag": etag})
        except BaseException:
            if identifier and not committed:
                try:
                    app.api.request(
                        "DELETE", path, params={"uploadId": identifier}, timeout=TRANSFER_TIMEOUT
                    )
                except BaseException as cleanup_error:
                    ui.warn(
                        f"Could not abort the incomplete multipart upload {identifier}: {cleanup_error}. "
                        "Remove it before deleting the bucket."
                    )
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
        "GET",
        target(bucket["name"], key),
        stream=True,
        headers={"Accept": "application/octet-stream"},
        timeout=TRANSFER_TIMEOUT,
        same_origin_redirects=True,
    )
    advertised = response.headers.get("content-length")
    expected = int(advertised) if advertised and advertised.isdigit() else None
    written = 0
    created = False
    try:
        with destination.open("xb") as file:
            created = True
            for chunk in response.iter_bytes():
                file.write(chunk)
                written += len(chunk)
        if expected is not None and written != expected:
            raise IpxError(
                f"The download ended after {written} of {expected} bytes.",
                "The object may be truncated; retry the download.",
            )
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
    """Remove one object key; retained versions can remain billable."""
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
    _save_credential(
        app,
        destination,
        lambda: app.api.post(
            "/v1/buckets/s3-credentials",
            {
                "permissions": ["storage.read"] if read_only else ["storage.read", "storage.write"],
                "bucket_ids": list(bucket_id) or None,
                "key_prefixes": list(prefix) or None,
                "expires_in": expires_in,
            },
        ),
        lambda data: {"id": data["id"], "saved_to": str(destination), "expires_at": data["expires_at"]},
        revoke_on_failure=True,
    )


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
    _save_credential(
        app,
        destination,
        lambda: app.api.post(
            "/v1/buckets/s3-credentials/" + quote(identifier, safe="") + "/rotate",
            {"revoke_previous": revoke_previous},
        ),
        lambda data: {
            "id": data["id"],
            "saved_to": str(destination),
            "previous_revoked": data["previous_revoked"],
        },
        revoke_on_failure=False,
    )


@credentials.command("revoke")
@click.argument("identifier")
@pass_app
def credential_revoke(app, identifier):
    app.api.delete("/v1/buckets/s3-credentials/" + quote(identifier, safe=""))
    output(app, {"revoked": identifier})


COMMANDS = [group]
