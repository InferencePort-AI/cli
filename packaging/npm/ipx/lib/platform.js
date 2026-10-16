"use strict";

// Locates the native ipx binary for this machine.
//   IPX_BINARY_PATH  use this file instead (development, CI smoke tests)
//   otherwise        the optional dependency @inferenceport/ipx-<os>-<cpu> that npm installed for this platform
const fs = require("fs");
const path = require("path");

const SUPPORTED = ["win32-x64", "win32-arm64", "darwin-x64", "darwin-arm64", "linux-x64", "linux-arm64"];

function resolveBinary() {
  const override = process.env.IPX_BINARY_PATH;
  if (override) {
    if (!fs.existsSync(override)) {
      throw new Error(`IPX_BINARY_PATH points to a file that does not exist: ${override}`);
    }
    return override;
  }

  const key = `${process.platform}-${process.arch}`;
  if (!SUPPORTED.includes(key)) {
    throw new Error(`no prebuilt binary for ${key} (supported: ${SUPPORTED.join(", ")}). Install with pip or build from source.`);
  }

  const pkg = `@inferenceport/ipx-${key}`;
  const exe = process.platform === "win32" ? "ipx.exe" : "ipx";
  let pkgJson;
  try {
    pkgJson = require.resolve(`${pkg}/package.json`);
  } catch (err) {
    throw new Error(
      `the platform package ${pkg} is not installed. Reinstall without --no-optional / --omit=optional, ` +
        "or use another install method (pip, cargo, install script)."
    );
  }
  const bin = path.join(path.dirname(pkgJson), "bin", exe);
  if (!fs.existsSync(bin)) {
    throw new Error(`${pkg} is installed but ${bin} is missing; try reinstalling @inferenceport/ipx.`);
  }
  return bin;
}

module.exports = { resolveBinary, SUPPORTED };
