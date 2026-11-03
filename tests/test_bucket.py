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
        return {"buckets": [{"id": "b1", "name": "bucket"}]}

    def request(self, method, path, **kwargs):
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
    assert result.exit_code == 0 and "Manage private Buckets" in result.output
    assert "pricing" in result.output and "status" in result.output
