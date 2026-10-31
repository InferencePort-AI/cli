"""Opt-in CLI against the isolated real HTTP gateway (no production endpoints)."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("BUCKETS_RUN_E2E") != "1", reason="sandbox E2E is opt-in")


def test_full_cli_bucket_lifecycle_and_private_credential_file(tmp_path):
    work = Path(
        os.getenv(
            "BUCKETS_SERVER_WORK",
            str(Path(__file__).resolve().parents[2] / "InferencePort-Lightning/work/buckets-sandbox"),
        )
    )
    tokens = json.loads((work / "credentials.json").read_text())
    env = {
        **os.environ,
        "IPX_HOME": str(tmp_path / "ipx"),
        "IPX_API_KEY": tokens["user1_personal"],
        "IPX_BASE_URL": "https://localhost:58080",
        "IPX_CA_BUNDLE": str(work / "localhost.pem"),
        "REQUESTS_CA_BUNDLE": str(work / "localhost.pem"),
        "NO_COLOR": "1",
    }

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "ipx", "--json", *args], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    name = "cli-e2e-" + uuid4().hex[:12]
    cli("bucket", "create", name)
    assert cli("bucket", "info", name)["name"] == name
    source = tmp_path / "model.bin"
    source.write_bytes(b"A" * (17 * 1024**2))
    assert (
        cli("bucket", "upload", name, str(source), "--key", "models/模型//../literal%2Fkey")["bytes"]
        == source.stat().st_size
    )
    assert cli("bucket", "ls", name, "--prefix", "models/")["objects"]
    destination = tmp_path / "download.bin"
    cli("bucket", "download", name, "models/模型//../literal%2Fkey", str(destination))
    assert destination.read_bytes() == source.read_bytes()
    assert cli("bucket", "usage", name)["meters"]
    credential_file = tmp_path / "s3-credential.json"
    result = cli("bucket", "credentials", "create", "--read-only", "--output", str(credential_file))
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert "secret_access_key" not in result
    saved = json.loads(credential_file.read_text())
    rotated_file = tmp_path / "rotated.json"
    rotated = cli(
        "bucket", "credentials", "rotate", saved["id"], "--revoke-previous", "--output", str(rotated_file)
    )
    assert rotated["previous_revoked"] is True and rotated_file.stat().st_mode & 0o777 == 0o600
    cli("bucket", "credentials", "revoke", rotated["id"])
    cli("bucket", "rm", name, "models/模型//../literal%2Fkey")
    cli("--yes", "bucket", "delete", name)
    assert all(row["name"] != name for row in cli("bucket", "list")["buckets"])
