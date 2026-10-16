# ipx-cli

The InferencePort AI command line, installed through Cargo.

```bash
cargo install ipx-cli
ipx --help
```

This crate is a small Rust launcher. Its build script downloads the native `ipx` binary for the
release matching the crate version, verifies its SHA-256 checksum, and embeds it, so **no Python or
other runtime is required**.

Docs: https://inferenceport-ai.readthedocs.io — source: https://github.com/inferenceport-ai/cli
