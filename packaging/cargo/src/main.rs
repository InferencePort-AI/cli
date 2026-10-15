//! ipx launcher: embeds the native binary fetched by build.rs, unpacks it once into a per-version cache folder and
//! runs it with the same arguments, stdio and exit code. No runtime other than this executable is needed.

use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{exit, Command},
};

static BINARY: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/ipx.bin"));
const HASH: &str = include_str!(concat!(env!("OUT_DIR"), "/ipx.sha256"));
const VERSION: &str = env!("CARGO_PKG_VERSION");

fn cache_root() -> PathBuf {
    if let Some(dir) = env::var_os("IPX_CACHE_DIR") {
        return PathBuf::from(dir);
    }
    #[cfg(windows)]
    {
        if let Some(d) = env::var_os("LOCALAPPDATA") {
            return PathBuf::from(d).join("ipx").join("bin");
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(h) = env::var_os("HOME") {
            return PathBuf::from(h).join("Library").join("Caches").join("ipx").join("bin");
        }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Some(d) = env::var_os("XDG_CACHE_HOME") {
            return PathBuf::from(d).join("ipx").join("bin");
        }
        if let Some(h) = env::var_os("HOME") {
            return PathBuf::from(h).join(".cache").join("ipx").join("bin");
        }
    }
    env::temp_dir().join("ipx-bin")
}

fn unpack() -> std::io::Result<PathBuf> {
    let name = if cfg!(windows) { "ipx.exe" } else { "ipx" };
    let dir = cache_root().join(format!("{VERSION}-{}", HASH.trim()));
    let target = dir.join(name);
    if target.exists() && fs::metadata(&target)?.len() == BINARY.len() as u64 {
        return Ok(target);
    }
    fs::create_dir_all(&dir)?;
    // Write to a temp file then rename so concurrent first runs never see a half-written binary.
    let tmp = dir.join(format!("{name}.{}.tmp", std::process::id()));
    fs::write(&tmp, BINARY)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o755))?;
    }
    if let Err(e) = fs::rename(&tmp, &target) {
        // Another process won the race; that copy is just as good.
        let _ = fs::remove_file(&tmp);
        if !Path::new(&target).exists() {
            return Err(e);
        }
    }
    Ok(target)
}

fn main() {
    if BINARY.is_empty() {
        eprintln!("ipx: this build embeds no binary (docs.rs placeholder).");
        exit(1);
    }
    let bin = match unpack() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("ipx: could not unpack the native binary: {e}\nSet IPX_CACHE_DIR to a writable folder.");
            exit(1);
        }
    };
    let mut cmd = Command::new(&bin);
    cmd.args(env::args_os().skip(1));

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let err = cmd.exec(); // only returns on failure
        eprintln!("ipx: failed to start {}: {err}", bin.display());
        exit(126);
    }
    #[cfg(not(unix))]
    {
        match cmd.status() {
            Ok(s) => exit(s.code().unwrap_or(1)),
            Err(e) => {
                eprintln!("ipx: failed to start {}: {e}", bin.display());
                exit(126);
            }
        }
    }
}
