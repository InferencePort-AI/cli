#!/usr/bin/env node
// Builds the per-platform npm packages from the release binaries.
//
//   node make-platform-packages.mjs <version> <binaries-dir> <out-dir>
//
// <binaries-dir> holds files named ipx-<os>-<arch>[.exe] (os: windows|macos|linux, arch: x64|arm64), as produced by
// packaging/build_binary.py. Each package contains package.json (with os/cpu so npm only installs the matching
// one) and bin/ipx[.exe].
import fs from "node:fs";
import path from "node:path";

const [version, binDir, outDir] = process.argv.slice(2);
if (!version || !binDir || !outDir) {
  console.error("usage: make-platform-packages.mjs <version> <binaries-dir> <out-dir>");
  process.exit(2);
}

const targets = [
  { file: "ipx-windows-x64.exe", os: "win32", cpu: "x64" },
  { file: "ipx-windows-arm64.exe", os: "win32", cpu: "arm64" },
  { file: "ipx-macos-x64", os: "darwin", cpu: "x64" },
  { file: "ipx-macos-arm64", os: "darwin", cpu: "arm64" },
  { file: "ipx-linux-x64", os: "linux", cpu: "x64" },
  { file: "ipx-linux-arm64", os: "linux", cpu: "arm64" },
];

let made = 0;
for (const t of targets) {
  const src = path.join(binDir, t.file);
  if (!fs.existsSync(src)) {
    console.warn(`skip ${t.os}-${t.cpu}: ${src} not found`);
    continue;
  }
  const name = `@inferenceport-ai/ipx-${t.os}-${t.cpu}`;
  const dir = path.join(outDir, `ipx-${t.os}-${t.cpu}`);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, "bin"), { recursive: true });
  const exe = t.os === "win32" ? "ipx.exe" : "ipx";
  fs.copyFileSync(src, path.join(dir, "bin", exe));
  fs.chmodSync(path.join(dir, "bin", exe), 0o755);
  fs.writeFileSync(
    path.join(dir, "package.json"),
    JSON.stringify(
      {
        name,
        version,
        description: `ipx native binary for ${t.os}-${t.cpu}`,
        license: "Apache-2.0",
        repository: { type: "git", url: "git+https://github.com/inferenceport-ai/cli.git" },
        os: [t.os],
        cpu: [t.cpu],
        files: ["bin"],
        preferUnplugged: true,
      },
      null,
      2
    ) + "\n"
  );
  fs.writeFileSync(path.join(dir, "README.md"), `# ${name}\n\nPlatform binary for [\`@inferenceport-ai/ipx\`](https://www.npmjs.com/package/@inferenceport-ai/ipx). Install that package instead.\n`);
  console.log(`built ${name}@${version}`);
  made++;
}
if (made === 0) {
  console.error("no binaries found");
  process.exit(1);
}
