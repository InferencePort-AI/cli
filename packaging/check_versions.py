#!/usr/bin/env python3
"""Fail if the pip, npm and cargo versions disagree (or differ from an expected version given as argv[1])."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    py = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "src/ipx/__init__.py").read_text(encoding="utf-8")).group(1)
    cargo = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "packaging/cargo/Cargo.toml").read_text(encoding="utf-8"), re.M).group(1)
    npm = json.loads((ROOT / "packaging/npm/ipx/package.json").read_text(encoding="utf-8"))
    versions = {"python": py, "cargo": cargo, "npm": npm["version"]}
    for name, v in npm.get("optionalDependencies", {}).items():
        versions[f"npm optional {name}"] = v
    if len(sys.argv) > 1:
        versions["expected"] = sys.argv[1]
    distinct = set(versions.values())
    for k, v in versions.items():
        print(f"{k:50} {v}")
    if len(distinct) != 1:
        print("\nVersion mismatch.", file=sys.stderr)
        return 1
    print("\nAll versions agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
