"""Buckets discovery reuses global context without sending it as authority."""

import json

import pytest
from click.testing import CliRunner

from ipx.cli import _register, hoist_globals, root
from ipx.http import StreamResponse, Transport


def test_bucket_commands_use_public_catalog_and_preserve_org_flags(monkeypatch, home):
    calls = []
    payloads = {
        "/v1/buckets/status": {
            "status": "not_available",
            "endpoint": "https://api.inferenceport.ai/s3/",
            "sdk_compatibility": "unverified",
        },
        "/v1/buckets/pricing": {
            "status": "ok",
            "pricing": {
                "status": "verified",
                "currency": "USD",
                "rate_version": "2026-11-01",
                "meters": [{"meter": "storage", "unit": "GiB-month", "price": "0.02"}],
            },
        },
    }

    def get(self, path, **kwargs):
        calls.append((path, kwargs, self.org))
        return payloads[path]

    monkeypatch.setattr(Transport, "get", get)
    _register()
    runner = CliRunner()

    status = runner.invoke(root, hoist_globals(["bucket", "status", "--org", "acme", "--json"]))
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["endpoint"] == "https://api.inferenceport.ai/s3/"

    pricing = runner.invoke(root, hoist_globals(["bucket", "pricing", "--org", "acme", "--json"]))
    assert pricing.exit_code == 0, pricing.output
    catalog = json.loads(pricing.output)["pricing"]
    assert catalog["currency"] == "USD"
    assert catalog["rate_version"] == "2026-11-01"
    assert catalog["meters"] == [{"meter": "storage", "unit": "GiB-month", "price": "0.02"}]

    assert calls == [
        ("/v1/buckets/status", {"auth": False, "org": False}, "acme"),
        ("/v1/buckets/pricing", {"auth": False, "org": False}, "acme"),
    ]


def test_bucket_pricing_renders_meter_rates(monkeypatch, home):
    def get(self, path, **kwargs):
        return {
            "pricing": {
                "status": "verified",
                "currency": "USD",
                "rate_version": "2026-11-01",
                "meters": [{"meter": "storage", "unit": "GiB-month", "price": "0.02"}],
            }
        }

    monkeypatch.setattr(Transport, "get", get)
    _register()
    result = CliRunner().invoke(root, hoist_globals(["bucket", "pricing"]))
    assert result.exit_code == 0, result.output
    for expected in ("USD", "2026-11-01", "storage", "GiB-month", "0.02"):
        assert expected in result.output


def test_lookup_prefers_an_exact_bucket_name_over_an_id(monkeypatch):
    from ipx.commands.bucket import lookup

    class Api:
        def get(self, path, **kwargs):
            return {
                "buckets": [
                    {"id": "target-id", "name": "other"},
                    {"id": "other-id", "name": "target"},
                ]
            }

    class App:
        api = Api()

    assert lookup(App(), "target")["id"] == "other-id"


def test_download_discards_a_truncated_file(monkeypatch, home, tmp_path):
    class Raw:
        def read(self, size):
            if getattr(self, "sent", False):
                return b""
            self.sent = True
            return b"short"

        def close(self):
            pass

    def get(self, path, **kwargs):
        if path == "/v1/buckets/workspace":
            owner = "11111111-1111-4111-8111-111111111111"
            return {"owner_kind": "user", "owner_id": owner,
                    "endpoint": f"https://api.inferenceport.ai/{owner}/s3/"}
        return {"buckets": [{"id": "b1", "name": "bucket"}]}

    def request(self, method, path, **kwargs):
        assert path == "https://api.inferenceport.ai/11111111-1111-4111-8111-111111111111/s3/bucket/key"
        assert kwargs.get("same_origin_redirects") is True
        assert kwargs.get("timeout") == 600.0
        return StreamResponse(Raw(), 200, {"content-length": "10"})

    monkeypatch.setattr(Transport, "get", get)
    monkeypatch.setattr(Transport, "request", request)
    _register()
    destination = tmp_path / "download.bin"
    result = CliRunner().invoke(root, ["bucket", "download", "bucket", "key", str(destination)])
    assert result.exit_code == 1
    assert "ended after 5 of 10 bytes" in str(result.exception)
    assert not destination.exists()


def test_credentials_refuse_plain_http_to_a_remote_host(monkeypatch, home, tmp_path):
    monkeypatch.setenv("IPX_BASE_URL", "http://api.example.test")
    _register()
    destination = tmp_path / "credential.json"
    result = CliRunner().invoke(
        root, ["bucket", "credentials", "create", "--output", str(destination)]
    )
    assert result.exit_code == 1
    assert "Refusing to send S3 credentials" in str(result.exception)
    assert not destination.exists()


def test_rotate_preserves_the_secret_file_when_saving_fails(monkeypatch, tmp_path):
    from ipx.commands import bucket

    class Api:
        base_url = "https://api.example.test"

        def delete(self, path):
            raise AssertionError("rotation must not revoke its replacement")

    class App:
        api = Api()

    monkeypatch.setattr(bucket, "_write_all", lambda fd, payload: (_ for _ in ()).throw(OSError("disk full")))
    destination = tmp_path / "rotated.json"

    def issue():
        return {"id": "new-id", "previous_revoked": True, "secret_access_key": "secret"}

    with pytest.raises(OSError):
        bucket._save_credential(App(), destination, issue, lambda data: {}, revoke_on_failure=False)
    assert destination.exists()


def test_create_revokes_the_new_credential_when_saving_fails(monkeypatch, tmp_path):
    from ipx.commands import bucket

    revoked = []

    class Api:
        base_url = "https://api.example.test"

        def delete(self, path):
            revoked.append(path)

    class App:
        api = Api()

    monkeypatch.setattr(bucket, "_write_all", lambda fd, payload: (_ for _ in ()).throw(OSError("disk full")))
    destination = tmp_path / "credential.json"

    def issue():
        return {"id": "new-id", "expires_at": "later", "secret_access_key": "secret"}

    with pytest.raises(OSError):
        bucket._save_credential(App(), destination, issue, lambda data: {}, revoke_on_failure=True)
    assert revoked == ["/v1/buckets/s3-credentials/new-id"]
    assert not destination.exists()


def test_part_size_stays_within_the_multipart_limit():
    from ipx.commands.bucket import MAX_PARTS, PART_SIZE, _part_size

    assert _part_size(10) == PART_SIZE
    size = 700 * 1024**3
    part_size = _part_size(size)
    assert -(-size // part_size) <= MAX_PARTS


def test_existing_commands_and_bucket_help_remain_registered(home):
    _register()
    assert {"bucket", "auth", "org", "gen", "p2g", "model"} <= set(root.commands)
    result = CliRunner().invoke(root, ["bucket", "--help"])
    assert result.exit_code == 0 and "Manage Buckets" in result.output
    assert "pricing" in result.output and "status" in result.output


def test_connection_uses_authenticated_org_context(monkeypatch, home):
    calls = []
    def get(self, path, **kwargs):
        calls.append((path, kwargs, self.org))
        return {"owner_kind": "org", "owner_id": "11111111-1111-4111-8111-111111111111",
                "endpoint": "https://api.inferenceport.ai/11111111-1111-4111-8111-111111111111/s3/"}
    monkeypatch.setattr(Transport, "get", get)
    _register()
    result = CliRunner().invoke(root, hoist_globals(["bucket", "connection", "--org", "acme", "--json"]))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["endpoint"] == "https://api.inferenceport.ai/11111111-1111-4111-8111-111111111111/s3/"
    assert calls == [("/v1/buckets/workspace", {}, "acme")]


def test_transfer_paths_use_receipt_and_preserve_opaque_keys():
    from types import SimpleNamespace

    from ipx.commands.bucket import target
    workspace_id = "11111111-1111-4111-8111-111111111111"
    calls = []
    endpoint = f"https://storage.example.test/{workspace_id}/s3/"
    def get(path):
        calls.append(path)
        return {"owner_id": workspace_id, "endpoint": endpoint}
    app = SimpleNamespace(api=SimpleNamespace(get=get))
    assert target(app, "models", "a/../literal%2F") == endpoint + "models/a%2F%2E%2E%2Fliteral%252F"
    assert calls == ["/v1/buckets/workspace"]




@pytest.mark.parametrize("endpoint", [
    "http://storage.example.test/11111111-1111-4111-8111-111111111111/s3/",
    "https://other.example.test/22222222-2222-4222-8222-222222222222/s3/",
    "https://user:secret@storage.example.test/11111111-1111-4111-8111-111111111111/s3/",
    "https://storage.example.test/11111111-1111-4111-8111-111111111111/s3/?token=1",
    "https://storage.example.test/11111111-1111-4111-8111-111111111111/s3/#fragment",
    "https://storage.example.test/11111111-1111-4111-8111-111111111111/s3/extra/",
])
def test_reject_untrusted_transfer_endpoint(endpoint):
    from types import SimpleNamespace

    from ipx.commands.bucket import target
    from ipx.errors import IpxError

    app = SimpleNamespace(api=SimpleNamespace(get=lambda path: {
        "owner_id": "11111111-1111-4111-8111-111111111111",
        "endpoint": endpoint,
    }))
    with pytest.raises(IpxError, match="invalid workspace S3 endpoint"):
        target(app, "models", "key")


def test_loopback_sandbox_endpoint_remains_supported():
    from types import SimpleNamespace

    from ipx.commands.bucket import target

    endpoint = "http://127.0.0.1:53001/11111111-1111-4111-8111-111111111111/s3/"
    app = SimpleNamespace(api=SimpleNamespace(get=lambda path: {
        "owner_id": "11111111-1111-4111-8111-111111111111",
        "endpoint": endpoint,
    }))
    assert target(app, "models", "test") == endpoint + "models/test"

@pytest.mark.parametrize("data", [{}, {"owner_id": "not-a-uuid"}, {"owner_id": None}, []])
def test_invalid_workspace_discovery_is_a_friendly_error(data):
    from types import SimpleNamespace

    from ipx.commands.bucket import target
    from ipx.errors import IpxError
    app = SimpleNamespace(api=SimpleNamespace(get=lambda path: data))
    with pytest.raises(IpxError, match="invalid workspace ID"):
        target(app, "models", "file")


@pytest.mark.parametrize("status", [404, 405, 501, 403])
def test_workspace_discovery_compatibility_and_auth_errors(status):
    from types import SimpleNamespace

    from ipx.commands.bucket import target
    from ipx.errors import ApiError, IpxError
    def get(path):
        raise ApiError(status, "Original error")
    app = SimpleNamespace(api=SimpleNamespace(get=get))
    with pytest.raises(IpxError) as error:
        target(app, "models", "file")
    if status == 403:
        assert isinstance(error.value, ApiError) and error.value.status == 403
    else:
        assert "does not support" in str(error.value)
        assert "Upgrade the API server" in error.value.hint


def test_update_preserves_workspace_and_sends_only_requested_changes(monkeypatch, home):
    calls = []
    def get(self, path, **kwargs):
        return {"buckets": [{"id":"fixture-bucket", "name":"models"}]}
    def patch(self, path, body, **kwargs):
        calls.append((self.org, path, body))
        return {"id":"fixture-bucket", **body}
    monkeypatch.setattr(Transport, "get", get)
    monkeypatch.setattr(Transport, "patch", patch)
    _register()
    result = CliRunner().invoke(root, hoist_globals(["bucket", "update", "models", "--name", "weights", "--quota-bytes", "1000", "--status", "read_only", "--org", "acme", "--json"]))
    assert result.exit_code == 0, result.output
    assert calls == [("acme", "/v1/buckets/fixture-bucket", {"name":"weights", "quota_bytes":1000, "status":"read_only"})]


def test_credential_network_retry_reuses_intent_without_retrying_authorization():
    from types import SimpleNamespace

    from ipx.commands.bucket import credential_request
    from ipx.errors import ApiError, IpxError
    calls = []
    def post(path, body, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise IpxError("network failure")
        return {"id":"same-credential"}
    app = SimpleNamespace(api=SimpleNamespace(post=post))
    key = "11111111-1111-4111-8111-111111111111"
    assert credential_request(app, "/issue", {}, key) == {"id":"same-credential"}
    assert calls == [{"headers":{"Idempotency-Key":key}}] * 2
    def forbidden(*args, **kwargs):
        raise ApiError(403, "Denied")
    app.api.post = forbidden
    with pytest.raises(ApiError, match="Denied"):
        credential_request(app, "/issue", {}, key)


def test_bucket_public_access_and_urls_preserve_org_context(monkeypatch, home):
    calls = []
    def get(self, path, **kwargs):
        assert self.org == "acme"
        if path.endswith("/objects"):
            return {"objects": [{"object_key": "a/../模型", "public_url": "https://api.example.test/public/buckets/b/object?key=a%2F..%2F%E6%A8%A1%E5%9E%8B"}]}
        return {"buckets": [{"id": "b", "name": "weights", "visibility": "public"}]}
    def post(self, path, body, **kwargs):
        calls.append((self.org, path, body)); return body
    def patch(self, path, body, **kwargs):
        calls.append((self.org, path, body)); return body
    monkeypatch.setattr(Transport, "get", get)
    monkeypatch.setattr(Transport, "post", post)
    monkeypatch.setattr(Transport, "patch", patch)
    _register()
    runner = CliRunner()
    for args in [["create", "weights", "--visibility", "public"], ["update", "weights", "--visibility", "private"], ["public-url", "weights", "a/../模型"]]:
        result = runner.invoke(root, hoist_globals(["bucket", *args, "--org", "acme", "--json"]))
        assert result.exit_code == 0, result.output
    assert calls == [("acme", "/v1/buckets", {"name": "weights", "quota_bytes": 10 * 1024**3, "visibility": "public"}), ("acme", "/v1/buckets/b", {"visibility": "private"})]
    assert json.loads(result.output)["public_url"].startswith("https://api.example.test/public/")


def test_public_url_refuses_private_bucket(monkeypatch, home):
    monkeypatch.setattr(Transport, "get", lambda *a, **kw: {"buckets": [{"id": "b", "name": "weights", "visibility": "private"}]})
    _register()
    result = CliRunner().invoke(root, ["bucket", "public-url", "weights", "file"])
    assert result.exit_code != 0 and "private" in str(result.exception)
