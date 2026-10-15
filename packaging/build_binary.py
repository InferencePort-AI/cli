#!/usr/bin/env python3
"""Compile ipx into a single native executable with Nuitka.

    python packaging/build_binary.py [--out dist] [--name ipx] [--jobs N]

Output: dist/ipx-<os>-<arch>[.exe]  (os: windows|macos|linux, arch: x64|arm64)  and a matching .sha256 file.
The result is fully self-contained: no Python, pip, npm or Rust is needed to run it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def target() -> str:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--name", default="ipx")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--no-lto", action="store_true", help="Faster compile, slightly larger/slower binary.")
    args = ap.parse_args()

    from ipx import __version__

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    plat = target()
    exe_name = f"{args.name}-{plat}" + (".exe" if plat.startswith("windows") else "")
    build_dir = ROOT / "build" / "nuitka"
    shutil.rmtree(build_dir, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        f"--jobs={args.jobs}",
        f"--output-dir={build_dir}",
        f"--output-filename={exe_name}",
        # the app and the libraries that load code/data dynamically
        "--include-package=ipx",
        "--include-package=rich._unicode_data",
        "--include-package-data=certifi",
        "--include-package-data=rich",
        # no GUI/debug extras we never use
        "--nofollow-import-to=tkinter,unittest,pydoc,pytest,setuptools,pip,distutils",
        "--python-flag=no_docstrings",
        # first launch unpacks once to a per-version cache; later launches start instantly
        "--onefile-tempdir-spec={CACHE_DIR}/ipx/" + __version__,
        "--product-name=ipx",
        "--file-description=InferencePort AI command line",
        "--company-name=InferencePort AI",
        f"--product-version={__version__}",
        f"--file-version={__version__}",
        "--copyright=Copyright 2026 InferencePort LLC",
        "--quiet",
    ]
    if plat.startswith("windows"):
        icon = ROOT / "packaging" / "icon.ico"
        if icon.exists():
            cmd.append(f"--windows-icon-from-ico={icon}")
        cmd.append("--windows-console-mode=force")
    if not args.no_lto:
        cmd.append("--lto=auto")
    cmd.append(str(ROOT / "packaging" / "entry.py"))

    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)

    built = build_dir / exe_name
    if not built.exists():
        candidates = list(build_dir.glob(f"{args.name}*"))
        raise SystemExit(f"Nuitka finished but {built} is missing; found {candidates}")
    dest = out / exe_name
    shutil.copy2(built, dest)
    dest.chmod(0o755)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    (out / (exe_name + ".sha256")).write_text(f"{digest}  {exe_name}\n", encoding="utf-8")
    print(f"\nBuilt {dest} ({dest.stat().st_size / 1e6:.1f} MB)\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
