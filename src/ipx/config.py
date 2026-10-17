"""Paths, persisted configuration and stored credentials.

Layout (per user):
  <config dir>/config.json       preferences: base URL, org context, client id, Supabase config cache
  <config dir>/credentials.json  tokens (mode 0600 on POSIX)
  <config dir>/server.pid        PID file for `ipx server start --detach`
  <config dir>/server.log        proxy server log
  <config dir>/policy-cache.json cached organization desktop policy

Environment overrides: IPX_HOME, IPX_BASE_URL, IPX_TOKEN, IPX_API_KEY, IPX_ORG, IPX_OLLAMA_URL.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = "https://api.inferenceport.ai"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_SERVER_PORT = 52458
CONSOLE_URL = "https://console.inferenceport.ai"

# Public, publishable Supabase client key (same one the desktop app ships). Used only if /v1/config is unreachable.
FALLBACK_SUPABASE = {
    "url": "https://dpixehhdbtzsbckfektd.supabase.co",
    "publishableKey": "sb_publishable_hbqJ9rAlgaeiYRzdVAqY4A_5pwWLSR9",
}

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")


def config_dir() -> Path:
    override = os.environ.get("IPX_HOME")
    if override:
        p = Path(override).expanduser()
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "ipx"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "ipx"
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ipx"
    p.mkdir(parents=True, exist_ok=True)
    return p


def desktop_data_dirs() -> list:
    """Candidate Electron userData folders for the InferencePort AI desktop app."""
    names = ["InferencePortAI", "InferencePort AI"]
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [base / n for n in names]


def desktop_data_dir() -> Path:
    """The folder the desktop app reads imports from (existing one wins; defaults to InferencePortAI)."""
    dirs = desktop_data_dirs()
    for d in dirs:
        if d.exists():
            return d
    return dirs[0]


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def replace_file(src: str, dst: str, attempts: int = 100) -> None:
    """``os.replace`` that tolerates Windows' transient ``PermissionError`` when another writer is replacing ``dst``.

    On POSIX the first attempt always succeeds. On Windows a replace can fail while a concurrent one is in flight, so
    retry briefly before giving up.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == attempts - 1:
                raise
            time.sleep(0.01 + 0.005 * min(attempt, 20))


def _write_json(path: Path, data: Dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary file per writer: two `ipx` processes saving at once (for example refreshing the same login)
    # must never share a path, or one could replace it while the other is still writing.
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if not private:
            os.chmod(tmp, 0o644)
        replace_file(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if private and sys.platform != "win32":
        os.chmod(str(path), 0o600)


class Config:
    """Preferences persisted to config.json, with environment overrides."""

    def __init__(self) -> None:
        self.path = config_dir() / "config.json"
        self.data = _read_json(self.path)

    def save(self) -> None:
        _write_json(self.path, self.data)

    # -- values ------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return (os.environ.get("IPX_BASE_URL") or self.data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    @property
    def ollama_url(self) -> str:
        return (os.environ.get("IPX_OLLAMA_URL") or self.data.get("ollama_url") or DEFAULT_OLLAMA_URL).rstrip("/")

    @property
    def org(self) -> Optional[str]:
        v = os.environ.get("IPX_ORG") or self.data.get("org")
        return str(v).strip().lower() if v else None

    def set_org(self, slug: Optional[str]) -> None:
        if slug:
            self.data["org"] = slug
        else:
            self.data.pop("org", None)
        self.save()

    @property
    def client_id(self) -> str:
        cid = self.data.get("client_id")
        if isinstance(cid, str) and CLIENT_ID_RE.match(cid):
            return cid
        cid = "ipx-" + secrets.token_hex(16)
        self.data["client_id"] = cid
        self.save()
        return cid

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value
        self.save()


class Credentials:
    """Stored login: either a Supabase session (jwt) or a Lightning API key."""

    def __init__(self) -> None:
        self.path = config_dir() / "credentials.json"
        self.data = _read_json(self.path)

    def save(self) -> None:
        _write_json(self.path, self.data, private=True)

    def clear(self) -> None:
        self.data = {}
        try:
            self.path.unlink()
        except OSError:
            pass

    @property
    def auth_type(self) -> Optional[str]:
        if os.environ.get("IPX_API_KEY") or os.environ.get("IPX_TOKEN"):
            return "api_key" if os.environ.get("IPX_API_KEY") else "jwt"
        return self.data.get("auth_type")

    @property
    def email(self) -> Optional[str]:
        return self.data.get("email")

    @property
    def access_token(self) -> Optional[str]:
        return os.environ.get("IPX_API_KEY") or os.environ.get("IPX_TOKEN") or (
            self.data.get("api_key") if self.data.get("auth_type") == "api_key" else self.data.get("access_token")
        )

    @property
    def refresh_token(self) -> Optional[str]:
        return self.data.get("refresh_token")

    @property
    def expires_at(self) -> float:
        try:
            return float(self.data.get("expires_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)

    def expiring(self, skew: float = 60.0) -> bool:
        return self.data.get("auth_type") == "jwt" and bool(self.refresh_token) and self.expires_at - skew <= time.time()

    def store_session(self, session: Dict[str, Any], email: Optional[str] = None) -> None:
        expires_at = session.get("expires_at")
        if not expires_at and session.get("expires_in"):
            expires_at = time.time() + float(session["expires_in"])
        user = session.get("user") or {}
        self.data = {
            "auth_type": "jwt",
            "access_token": session.get("access_token"),
            "refresh_token": session.get("refresh_token"),
            "expires_at": expires_at,
            "email": email or user.get("email") or self.data.get("email"),
            "user_id": user.get("id") or self.data.get("user_id"),
            "provider": (user.get("app_metadata") or {}).get("provider") or self.data.get("provider"),
        }
        self.save()

    def store_api_key(self, key: str, email: Optional[str] = None) -> None:
        self.data = {"auth_type": "api_key", "api_key": key, "email": email}
        self.save()
