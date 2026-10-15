"""Sign-in flows against Supabase (email/password, OAuth via browser, SSO) plus API-key login and token refresh."""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from ipx.config import FALLBACK_SUPABASE, Config, Credentials
from ipx.errors import ApiError, IpxError
from ipx.http import raw_request

CALLBACK_PAGE = "https://inference.js.org/authcallback.html"
OAUTH_PROVIDERS = {"github": "GitHub", "google": "Google", "microsoft": "Microsoft", "huggingface": "Hugging Face"}
# Supabase's provider id for Microsoft is "azure".
PROVIDER_IDS = {"github": "github", "google": "google", "microsoft": "azure", "huggingface": "custom:huggingface"}


# --------------------------------------------------------------------------- supabase config


def supabase_config(cfg: Config) -> Dict[str, str]:
    """Supabase URL + publishable key. Fetched from /v1/config (public) and cached, with a baked-in fallback."""
    cached = cfg.get("supabase")
    if isinstance(cached, dict) and cached.get("url") and cached.get("publishableKey"):
        return cached
    try:
        r = raw_request("GET", cfg.base_url + "/v1/config", timeout=15)
        sb = r.json().get("supabase") or {}  # type: ignore[union-attr]
        if sb.get("url") and sb.get("publishableKey"):
            found = {"url": sb["url"].rstrip("/"), "publishableKey": sb["publishableKey"]}
            cfg.set("supabase", found)
            return found
    except IpxError:
        pass
    return dict(FALLBACK_SUPABASE)


def _auth_call(cfg: Config, method: str, path: str, body: Any = None, token: Optional[str] = None) -> Dict[str, Any]:
    sb = supabase_config(cfg)
    headers = {"apikey": sb["publishableKey"], "Authorization": f"Bearer {token or sb['publishableKey']}"}
    try:
        r = raw_request(method, sb["url"] + path, json=body, headers=headers, timeout=30)
    except ApiError as e:
        raise IpxError(_friendly_auth_error(e), "Check your credentials, or sign in with `ipx auth login --provider github`.")
    return r.json() or {}  # type: ignore[union-attr]


def _friendly_auth_error(e: ApiError) -> str:
    msg = e.message
    low = msg.lower()
    if "invalid login" in low or "invalid_grant" in low or "invalid credentials" in low:
        return "Incorrect email or password."
    if "email not confirmed" in low:
        return "Your email address is not verified yet. Check your inbox for the confirmation link."
    return msg


# --------------------------------------------------------------------------- flows


def password_login(cfg: Config, creds: Credentials, email: str, password: str) -> Dict[str, Any]:
    session = _auth_call(cfg, "POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
    creds.store_session(session, email=email)
    return session


def refresh(cfg: Config, creds: Credentials) -> bool:
    """Exchange the stored refresh token for a new session. Returns True on success."""
    rt = creds.refresh_token
    if not rt or creds.data.get("auth_type") != "jwt":
        return False
    try:
        session = _auth_call(cfg, "POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": rt})
    except IpxError:
        return False
    if not session.get("access_token"):
        return False
    creds.store_session(session)
    return True


def fetch_user(cfg: Config, access_token: str) -> Dict[str, Any]:
    return _auth_call(cfg, "GET", "/auth/v1/user", token=access_token)


def oauth_url(cfg: Config, provider: str, redirect_to: str) -> str:
    sb = supabase_config(cfg)
    params = {"provider": PROVIDER_IDS[provider], "redirect_to": redirect_to}
    if provider == "microsoft":
        params["scope"] = "openid profile email"
    q = urllib.parse.urlencode(params)
    return f"{sb['url']}/auth/v1/authorize?{q}"


def sso_url(cfg: Config, email: str, redirect_to: str) -> str:
    """Discover the org SSO connection for an email domain and ask Supabase for the IdP login URL."""
    disc = raw_request("POST", cfg.base_url + "/v1/orgs/sso/discover", json={"email": email}, timeout=15).json()  # type: ignore[union-attr]
    if not disc or not disc.get("sso"):
        raise IpxError("No single sign-on is configured for this email domain.",
                       "Use `ipx auth login` (email/password) or `--provider`.")
    target: Dict[str, Any] = {"provider_id": disc["provider_id"]} if disc.get("provider_id") else {"domain": disc.get("domain")}
    if not target.get("provider_id") and not target.get("domain"):
        raise IpxError("Single sign-on is configured but incomplete. Contact your administrator.")
    body = dict(target, redirect_to=redirect_to, skip_http_redirect=True)
    res = _auth_call(cfg, "POST", "/auth/v1/sso", body)
    if not res.get("url"):
        raise IpxError("Could not start single sign-on.")
    return str(res["url"])


def api_key_login(cfg: Config, creds: Credentials, key: str) -> Dict[str, Any]:
    """Validate a Lightning API key (ipa_live_...) against /v1/me and store it."""
    key = key.strip()
    try:
        me = raw_request("GET", cfg.base_url + "/v1/me",
                         headers={"Authorization": f"Bearer {key}", "X-Client-ID": cfg.client_id}, timeout=20).json()  # type: ignore[union-attr]
    except ApiError as e:
        if e.status in (401, 403):
            raise IpxError("That API key was rejected.", "Create one at https://console.inferenceport.ai or with `ipx p2g keys create`.")
        raise
    email = ((me or {}).get("identity") or {}).get("email")
    creds.store_api_key(key, email=email)
    return me or {}


def parse_callback(text: str) -> Dict[str, Any]:
    """Parse tokens from a pasted callback URL or a bare `access_token=...&refresh_token=...` string."""
    text = text.strip()
    frag = text.split("#", 1)[1] if "#" in text else (text.split("?", 1)[1] if "?" in text and "access_token" in text else text)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(frag).items()}
    if params.get("error"):
        raise IpxError(params.get("error_description") or params["error"])
    if not params.get("access_token"):
        raise IpxError("No access token found in what you pasted.", "Paste the full address from the browser's address bar.")
    session: Dict[str, Any] = {
        "access_token": params["access_token"],
        "refresh_token": params.get("refresh_token"),
    }
    if params.get("expires_at"):
        session["expires_at"] = float(params["expires_at"])
    elif params.get("expires_in"):
        session["expires_at"] = time.time() + float(params["expires_in"])
    return session


_LANDING = """<!doctype html><meta charset=utf-8><title>ipx</title>
<style>body{margin:0;display:grid;place-items:center;height:100vh;background:#0b1020;color:#e2e8f0;font:16px system-ui}
.c{text-align:center;padding:40px 48px;border:1px solid #1e293b;border-radius:16px;background:#0f172a}
h1{margin:0 0 8px;font-size:22px;background:linear-gradient(90deg,#4f7cff,#a78bfa);-webkit-background-clip:text;color:transparent}
p{margin:0;color:#94a3b8}</style>
<div class=c><h1 id=t>Signing you in…</h1><p id=m>One moment.</p></div>
<script>
const h=location.hash.slice(1);
fetch('/token?n=__NONCE__',{method:'POST',body:h}).then(r=>{
 document.getElementById('t').textContent=r.ok?'You are signed in to ipx':'Sign-in failed';
 document.getElementById('m').textContent=r.ok?'You can close this tab and return to your terminal.':'Return to your terminal for details.';
}).catch(()=>{document.getElementById('t').textContent='Sign-in failed';});
</script>"""


def browser_login(cfg: Config, build_url, *, timeout: float = 300.0, open_browser: bool = True,
                  on_url=None) -> Dict[str, Any]:
    """Run the OAuth/SSO dance with a loopback listener.

    The hosted callback page (inference.js.org/authcallback.html) forwards the token fragment to the `rd`
    address we give it; a tiny page served from 127.0.0.1 reads the fragment and posts it back to us.
    """
    nonce = secrets.token_urlsafe(16)
    result: Dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # silence
            pass

        def do_GET(self) -> None:  # noqa: N802
            if urllib.parse.urlparse(self.path).path == "/callback":
                body = _LANDING.replace("__NONCE__", nonce).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = urllib.parse.urlparse(self.path).path == "/token" and q.get("n", [""])[0] == nonce
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            self.send_response(200 if ok else 403)
            self.end_headers()
            if ok:
                try:
                    result["session"] = parse_callback(raw)
                except IpxError as e:
                    result["error"] = e
                done.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    rd = f"http://127.0.0.1:{port}/callback"
    redirect_to = f"{CALLBACK_PAGE}?rd={urllib.parse.quote(rd, safe='')}"
    url = build_url(redirect_to)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_url:
            on_url(url)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # pragma: no cover
                pass
        if not done.wait(timeout):
            raise IpxError("Timed out waiting for the browser sign-in.",
                           "Try again, or use `ipx auth login --provider <name> --paste` to paste the redirect URL.")
    finally:
        server.shutdown()
        server.server_close()
    if "error" in result:
        raise result["error"]
    return result["session"]


def complete_session(cfg: Config, creds: Credentials, session: Dict[str, Any]) -> Dict[str, Any]:
    """Look up the user for a freshly obtained session and persist it."""
    user = fetch_user(cfg, session["access_token"])
    session = dict(session, user=user)
    creds.store_session(session, email=user.get("email"))
    return user


def sign_out(cfg: Config, creds: Credentials) -> None:
    token = creds.access_token if creds.data.get("auth_type") == "jwt" else None
    if token:
        try:
            _auth_call(cfg, "POST", "/auth/v1/logout", {}, token=token)
        except IpxError:
            pass  # revoking server-side is best effort; local credentials are always removed
    creds.clear()


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
