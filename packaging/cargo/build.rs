//! Fetches the prebuilt (Nuitka-compiled) ipx binary for the build target, verifies its SHA-256 and places it in
//! OUT_DIR so `main.rs` can embed it.
//!
//! Environment:
//!   IPX_BINARY_PATH   use this local file instead of downloading (offline / CI builds)
//!   IPX_RELEASE_BASE  override the download base URL (mirrors); default is the GitHub release for this version
//!   DOCS_RS           set by docs.rs - an empty placeholder is embedded instead

use sha2::{Digest, Sha256};
use std::{env, fs, io::Read, path::PathBuf};

fn asset_for(target: &str) -> Option<&'static str> {
    let arch64 = target.starts_with("x86_64");
    let arm64 = target.starts_with("aarch64");
    if target.contains("windows") {
        if arch64 { Some("ipx-windows-x64.exe") } else if arm64 { Some("ipx-windows-arm64.exe") } else { None }
    } else if target.contains("apple") {
        if arch64 { Some("ipx-macos-x64") } else if arm64 { Some("ipx-macos-arm64") } else { None }
    } else if target.contains("linux") {
        if arch64 { Some("ipx-linux-x64") } else if arm64 { Some("ipx-linux-arm64") } else { None }
    } else {
        None
    }
}

fn download(url: &str) -> Vec<u8> {
    let resp = ureq::get(url)
        .set("User-Agent", "ipx-cli-build")
        .call()
        .unwrap_or_else(|e| panic!("could not download {url}: {e}\nSet IPX_BINARY_PATH to a local ipx binary to build offline."));
    let mut buf = Vec::new();
    resp.into_reader().read_to_end(&mut buf).expect("download interrupted");
    buf
}

fn main() {
    println!("cargo:rerun-if-env-changed=IPX_BINARY_PATH");
    println!("cargo:rerun-if-env-changed=IPX_RELEASE_BASE");
    println!("cargo:rerun-if-changed=build.rs");

    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    let dest = out.join("ipx.bin");
    let hash_file = out.join("ipx.sha256");

    if env::var_os("DOCS_RS").is_some() {
        fs::write(&dest, b"").unwrap();
        fs::write(&hash_file, "docsrs00").unwrap();
        return;
    }

    let bytes = if let Ok(local) = env::var("IPX_BINARY_PATH") {
        println!("cargo:rerun-if-changed={local}");
        fs::read(&local).unwrap_or_else(|e| panic!("cannot read IPX_BINARY_PATH={local}: {e}"))
    } else {
        let target = env::var("TARGET").unwrap();
        let asset = asset_for(&target).unwrap_or_else(|| {
            panic!("ipx has no prebuilt binary for {target}. Download one from the releases page or build from source (pip install inferenceport-cli).")
        });
        let version = env::var("CARGO_PKG_VERSION").unwrap();
        let base = env::var("IPX_RELEASE_BASE")
            .unwrap_or_else(|_| format!("https://github.com/inferenceport-ai/cli/releases/download/v{version}"));
        let data = download(&format!("{base}/{asset}"));
        let expected = String::from_utf8(download(&format!("{base}/{asset}.sha256")))
            .expect("checksum file is not UTF-8")
            .split_whitespace()
            .next()
            .unwrap_or("")
            .to_lowercase();
        let actual = format!("{:x}", Sha256::digest(&data));
        if expected != actual {
            panic!("checksum mismatch for {asset}: expected {expected}, got {actual}");
        }
        data
    };

    let digest = format!("{:x}", Sha256::digest(&bytes));
    fs::write(&dest, &bytes).unwrap();
    fs::write(&hash_file, &digest[..16]).unwrap();
}
