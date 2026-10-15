#!/usr/bin/env node
"use strict";

// Thin launcher: finds the platform-specific native binary and runs it with the same stdio and exit code.
const { spawnSync } = require("child_process");
const { resolveBinary } = require("../lib/platform.js");

let bin;
try {
  bin = resolveBinary();
} catch (err) {
  console.error(`ipx: ${err.message}`);
  process.exit(127);
}

const result = spawnSync(bin, process.argv.slice(2), { stdio: "inherit", windowsHide: false });

if (result.error) {
  console.error(`ipx: failed to start ${bin}: ${result.error.message}`);
  process.exit(126);
}
if (result.signal) {
  // Re-raise so shells see the right termination reason (e.g. Ctrl+C).
  process.kill(process.pid, result.signal);
} else {
  process.exit(result.status === null ? 1 : result.status);
}
