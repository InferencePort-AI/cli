"""Tests for the manual CLI version bump."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ["src/ipx/__init__.py", "packaging/cargo/Cargo.toml", "packaging/cargo/Cargo.lock",
         "packaging/npm/ipx/package.json", "install.sh", "install.ps1"]

@pytest.fixture
def checkout(tmp_path):
    for file in [*FILES, "packaging/bump_version.py"]:
        target = tmp_path / file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / file, target)
    return tmp_path

def run(root, version):
    return subprocess.run([sys.executable, str(root / "packaging/bump_version.py"), version],
                          text=True, capture_output=True, cwd=root)

def test_bump_precisely_updates_cli_versions(checkout):
    old = re.search(r'__version__ = "([^"]+)"', (checkout / FILES[0]).read_text()).group(1)
    new = "99.88.77" if old != "99.88.77" else "99.88.78"
    lock_before = (checkout / FILES[2]).read_text()
    result = run(checkout, new)
    assert result.returncode == 0, result.stderr
    lock_after = (checkout / FILES[2]).read_text()
    assert lock_after == lock_before.replace('name = "ipx-cli"\nversion = "' + old + '"',
                                             'name = "ipx-cli"\nversion = "' + new + '"', 1)
    assert '__version__ = "' + new + '"' in (checkout / FILES[0]).read_text()
    assert 'version = "' + new + '"' in (checkout / FILES[1]).read_text()
    assert 'IPX_VERSION=' + new in (checkout / FILES[4]).read_text()
    assert 'IPX_VERSION = "' + new + '"' in (checkout / FILES[5]).read_text()
    npm = json.loads((checkout / FILES[3]).read_text())
    assert npm["version"] == new
    assert npm["optionalDependencies"]
    assert all(v == new for v in npm["optionalDependencies"].values())
    assert run(checkout, new).returncode == 0

def test_invalid_version_does_not_write_files(checkout):
    before = {p: (checkout / p).read_bytes() for p in FILES}
    assert run(checkout, "1.1.0; echo nope").returncode != 0
    assert {p: (checkout / p).read_bytes() for p in FILES} == before

def test_mismatch_does_not_write_files(checkout):
    p = checkout / FILES[1]
    p.write_text(p.read_text().replace('name = "ipx-cli"\nversion = "',
                                       'name = "ipx-cli"\nversion = "999.', 1))
    before = {p: (checkout / p).read_bytes() for p in FILES}
    assert run(checkout, "99.88.77").returncode != 0
    assert {p: (checkout / p).read_bytes() for p in FILES} == before
