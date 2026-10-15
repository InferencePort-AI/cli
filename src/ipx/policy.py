"""Client-side enforcement of an organization's desktop policy (GET /v1/orgs/me/desktop-policy).

Mirrors the desktop app: when an organization context is active, model pulls, imports, server hosting and cloud
use follow that organization's controls. This stops accidents and casual changes; the server-side model/modality
policy and budgets (X-InferencePort-Org) remain the hard guard for anything billed to the organization.

Fail closed: if the policy of the active org cannot be verified (and there is no fresh cache), restricted actions
are refused rather than allowed.
"""

from __future__ import annotations

import fnmatch
import json
import time
from typing import Any, Dict, List, Optional

from ipx.app import App
from ipx.config import config_dir
from ipx.errors import IpxError

TTL = 5 * 60
MAX_STALE = 14 * 24 * 3600

DEFAULT = {
    "lightning": "allow", "lightning_billing": "personal", "pull_mode": "any", "pull_models": None,
    "allow_model_import": True, "allow_custom_tools": True, "server_hosting": "any", "server_access": "manual",
    "server_team_ids": [], "unlimited_verifications": False,
}


def _cache_path():
    return config_dir() / "policy-cache.json"


def clear_cache() -> None:
    try:
        _cache_path().unlink()
    except OSError:
        pass


def _load_cache() -> Dict[str, Any]:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fetch(app: App) -> Optional[Dict[str, Any]]:
    cache = _load_cache()
    uid = app.creds.data.get("user_id") or app.creds.email
    if cache.get("user") == uid and time.time() - float(cache.get("at", 0)) < TTL:
        return cache.get("orgs")
    try:
        d = app.api.get("/v1/orgs/me/desktop-policy", org=False) or {}
        orgs = {o["slug"]: o for o in d.get("orgs") or [] if isinstance(o, dict) and o.get("slug")}
        _cache_path().write_text(json.dumps({"user": uid, "at": time.time(), "orgs": orgs}), encoding="utf-8")
        return orgs
    except IpxError:
        if cache.get("user") == uid and time.time() - float(cache.get("at", 0)) < MAX_STALE:
            return cache.get("orgs")
        return None


def active(app: App) -> Optional[Dict[str, Any]]:
    """The active org's policy entry, or None when no organization controls apply.

    Returns {"__unverified__": True} when controls may apply but could not be checked.
    """
    slug = app.cfg.org if not app.personal else None
    if app._org_override:
        slug = app._org_override
    if not slug or not app.creds.signed_in:
        return None
    if app.creds.auth_type == "api_key":
        return None  # the desktop-policy route is JWT only; API-key use is governed server-side
    orgs = _fetch(app)
    if orgs is None:
        return {"__unverified__": True, "slug": slug}
    entry = orgs.get(slug)
    if entry is None:
        raise IpxError(f"You are not a member of organization “{slug}”.", "Pick another with `ipx auth context`.")
    if not entry.get("managed"):
        return None
    if entry.get("suspended"):
        raise IpxError(f"Your membership of {slug} is suspended.", "Contact an organization admin or run `ipx auth context --clear`.")
    return entry


def _blocked(entry: Dict[str, Any], what: str) -> IpxError:
    if entry.get("__unverified__"):
        return IpxError(f"Could not verify {entry['slug']}'s organization policy, so {what} is blocked.",
                        "Check your connection and try again, or run `ipx auth context --clear` to leave the organization context.")
    return IpxError(f"{entry.get('name', entry.get('slug'))} does not allow {what}.",
                    "Ask an organization admin, or run `ipx auth context --clear` to use your personal account.")


def assert_cloud(app: App) -> None:
    e = active(app)
    if e and (e.get("__unverified__") or (e.get("policy") or {}).get("lightning") == "local_only"):
        raise _blocked(e, "cloud models (local models only)")


def assert_pull(app: App, model: str) -> None:
    e = active(app)
    if not e:
        return
    if e.get("__unverified__"):
        raise _blocked(e, "pulling models")
    pol = {**DEFAULT, **(e.get("policy") or {})}
    mode = pol["pull_mode"]
    if mode == "blocked":
        raise _blocked(e, "pulling models")
    if mode == "allowlist":
        base = model.split(":")[0]
        allowed: List[str] = pol.get("pull_models") or []
        if not any(fnmatch.fnmatch(model, a) or fnmatch.fnmatch(base, a) or a == model or a.split(":")[0] == base for a in allowed):
            raise IpxError(f"{model} is not on {e.get('name')}'s approved model list.",
                           "Approved: " + (", ".join(allowed) if allowed else "none"))


def assert_import(app: App) -> None:
    e = active(app)
    if e and (e.get("__unverified__") or not {**DEFAULT, **(e.get("policy") or {})}["allow_model_import"]):
        raise _blocked(e, "importing models")


def host_settings(app: App) -> Dict[str, Any]:
    """{"managed": bool, "users": [...], "access": str}; raises if hosting is not allowed."""
    e = active(app)
    if not e:
        return {"managed": False, "users": [], "access": "manual"}
    if e.get("__unverified__"):
        raise _blocked(e, "hosting a server")
    pol = {**DEFAULT, **(e.get("policy") or {})}
    if pol["server_hosting"] == "disabled" or not e.get("can_host_server"):
        raise _blocked(e, "you to host a server" if pol["server_hosting"] != "disabled" else "server hosting")
    return {"managed": True, "users": e.get("server_users") or [], "access": pol["server_access"], "org": e.get("name")}
