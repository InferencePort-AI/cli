#!/bin/sh
# Install the latest ipx release binary.   curl -fsSL https://raw.githubusercontent.com/inferenceport-ai/cli/main/install.sh | sh
# Options (environment): IPX_VERSION=1.0.0  IPX_INSTALL_DIR=$HOME/.local/bin
set -eu

REPO="inferenceport-ai/cli"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$os" in
  linux) os=linux ;;
  darwin) os=macos ;;
  *) echo "Unsupported OS: $os (on Windows use install.ps1)" >&2; exit 1 ;;
esac
arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch=x64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) echo "Unsupported CPU: $arch" >&2; exit 1 ;;
esac

asset="ipx-$os-$arch"
if [ -n "${IPX_VERSION:-}" ]; then
  base="https://github.com/$REPO/releases/download/v${IPX_VERSION#v}"
else
  base="https://github.com/$REPO/releases/latest/download"
fi
dest_dir="${IPX_INSTALL_DIR:-$HOME/.local/bin}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading $asset ..."
curl -fsSL "$base/$asset" -o "$tmp/ipx"
curl -fsSL "$base/$asset.sha256" -o "$tmp/ipx.sha256"

expected="$(cut -d' ' -f1 "$tmp/ipx.sha256")"
if command -v sha256sum >/dev/null 2>&1; then actual="$(sha256sum "$tmp/ipx" | cut -d' ' -f1)"
else actual="$(shasum -a 256 "$tmp/ipx" | cut -d' ' -f1)"; fi
if [ "$expected" != "$actual" ]; then
  echo "Checksum mismatch - refusing to install." >&2
  exit 1
fi

mkdir -p "$dest_dir"
chmod +x "$tmp/ipx"
mv "$tmp/ipx" "$dest_dir/ipx"
echo "Installed ipx to $dest_dir/ipx"
case ":$PATH:" in *":$dest_dir:"*) ;; *) echo "Add $dest_dir to your PATH to use it." ;; esac
"$dest_dir/ipx" --version
