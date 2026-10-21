#!/usr/bin/env python3
"""Bump first-party CLI version declarations while preserving dependencies."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS = {
    "src/ipx/__init__.py": r'(?m)^__version__ = "(?P<version>[^"]+)"',
    "packaging/cargo/Cargo.toml": r'(?m)^\[package\]\nname = "ipx-cli"\nversion = "(?P<version>[^"]+)"',
    "packaging/cargo/Cargo.lock": r'(?m)^\[\[package\]\]\nname = "ipx-cli"\nversion = "(?P<version>[^"]+)"',
    "install.sh": r'(?m)^# Options \(environment\): IPX_VERSION=(?P<version>[^\s]+)',
    "install.ps1": r'(?m)^# Options: \$env:IPX_VERSION = "(?P<version>[^"]+)"',
    "packaging/npm/ipx/package.json": r'(?m)^  "version": "(?P<version>[^"]+)"',
}

def substitute(content, regex, old, new, path):
    matches = list(re.finditer(regex, content))
    if len(matches) != 1 or matches[0].group("version") != old:
        raise ValueError(f"{path}: missing, duplicate or inconsistent version (expected {old})")
    hit = matches[0]
    return content[:hit.start("version")] + new + content[hit.end("version"):]

def bump(new):
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', new):
        raise ValueError("Expected stable X.Y.Z version, e.g. 1.1.0 (no v prefix)")
    original = {p: (ROOT / p).read_text(encoding="utf-8") for p in PATHS}
    m = re.search(PATHS["src/ipx/__init__.py"], original["src/ipx/__init__.py"])
    if m is None:
        raise ValueError("Python version declaration missing")
    old = m.group("version")
    result = {p: substitute(data, PATHS[p], old, new, p) for p, data in original.items()}
    path = "packaging/npm/ipx/package.json"
    npm = json.loads(original[path])
    if npm["name"] != "@inferenceport-ai/ipx":
        raise ValueError("Unexpected npm package")
    platform_packages = [p for p in npm["optionalDependencies"] if p.startswith("@inferenceport-ai/ipx-")]
    if not platform_packages:
        raise ValueError("No platform packages")
    for name in platform_packages:
        pattern = rf'(?m)^\s*"{re.escape(name)}": "(?P<version>[^"]+)"'
        result[path] = substitute(result[path], pattern, old, new, f"{path}: {name}")
    json.loads(result[path])
    for path, value in result.items():
        if value != original[path]:
            (ROOT / path).write_text(value, encoding="utf-8")
            print("Updated", path)
    print(f"Version {old} -> {new}")

if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: python packaging/bump_version.py X.Y.Z")
        bump(sys.argv[1])
    except (ValueError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
