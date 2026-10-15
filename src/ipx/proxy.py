"""The `ipx server` host proxy: share your local Ollama with approved InferencePort users.

Behaviour matches the desktop app's host server (port 52458):
  * every request needs `Authorization: Bearer <token>`: a server API key, or an InferencePort access token that
    is verified (with the verify_token_with_email function) against the allowed-users list;
  * non-admin users are read-only (no pull / delete / create / run style writes);
  * `GET /__health` answers 204;
  * everything else is streamed through to Ollama.

The desktop's optional end-to-end encryption handshake is not implemented. The capabilities probe answers
`supported: false`, which makes desktop clients fall back to plain HTTP(S) exactly as they do for older servers.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple

from ipx.config import Config, Credentials, config_dir
from ipx.errors import ApiError, IpxError
from ipx.http import raw_request

MAX_BODY = 20 * 1024 * 1024
MAX_LOG = 10 * 1024 * 1024
RATE_LIMIT = 240  # requests per minute per client address
CAPABILITIES = "/__inferenceport/crypto-capabilities"
HANDSHAKE = "/__inferenceport/crypto-handshake"
WRITE_PATTERN = re.compile(r"pull|rm|delete|create|models|run", re.I)
ALLOWED_ORIGINS = ("https://inference.js.org",)
HOP = {"host", "connection", "keep-alive", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "authorization", "x-inferenceport-session", "content-length"}


def log_path():
    return config_dir() / "server.log"


class ServerLog:
    def __init__(self, echo: bool = False) -> None:
        self.lock = threading.Lock()
        self.echo = echo
        try:
            if log_path().exists() and log_path().stat().st_size > MAX_LOG:
                log_path().write_text("", encoding="utf-8")
        except OSError:
            pass

    def line(self, level: str, msg: str) -> None:
        text = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
        with self.lock:
            try:
                with open(log_path(), "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass
        if self.echo:
            print(text, flush=True)


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:10]


def mask(token: str) -> str:
    return token[:4] + "…" + token[-3:] if len(token) > 10 else "***"


def sanitize(s: str) -> str:
    return re.sub(r"[\r\n\t]", " ", s)[:200]


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per = per_minute
        self.hits: Dict[str, Deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per:
                return False
            q.append(now)
            return True


class Verifier:
    """Verifies InferencePort access tokens through the verify_token_with_email edge function."""

    def __init__(self, cfg: Config, creds: Credentials) -> None:
        from ipx.auth import supabase_config

        sb = supabase_config(cfg)
        self.url = sb["url"] + "/functions/v1/verify_token_with_email"
        self.key = sb["publishableKey"]
        self.cfg, self.creds = cfg, creds

    def verify(self, token: str, emails: List[str]) -> Tuple[int, Optional[Dict[str, Any]]]:
        from ipx.auth import refresh

        if self.creds.expiring():
            refresh(self.cfg, self.creds)
        host_token = self.creds.access_token or self.key
        try:
            r = raw_request("POST", self.url, json={"token": token, "emails": emails},
                            headers={"Authorization": f"Bearer {host_token}", "apikey": self.key}, timeout=15)
            return r.status, r.json()  # type: ignore[union-attr]
        except ApiError as e:
            return e.status, e.body if isinstance(e.body, dict) else None
        except IpxError:
            return 500, None


class ProxyServer:
    def __init__(self, *, host: str, port: int, ollama_url: str, users: List[Dict[str, str]], api_keys: List[str],
                 verifier: Optional[Verifier], echo: bool = False) -> None:
        self.host, self.port = host, port
        self.ollama = urllib.parse.urlparse(ollama_url)
        self.users = {u["email"].lower(): (u.get("role") or "member").lower() for u in users}
        self.api_keys = set(api_keys)
        self.verifier = verifier
        self.log = ServerLog(echo)
        self.limiter = RateLimiter(RATE_LIMIT)
        self.httpd: Optional[ThreadingHTTPServer] = None

    # -- lifecycle -----------------------------------------------------------
    def serve(self, on_ready=None) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ipx"

            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None: server._handle(self)       # noqa: E704,N802
            def do_POST(self) -> None: server._handle(self)      # noqa: E704,N802
            def do_PUT(self) -> None: server._handle(self)       # noqa: E704,N802
            def do_DELETE(self) -> None: server._handle(self)    # noqa: E704,N802
            def do_HEAD(self) -> None: server._handle(self)      # noqa: E704,N802
            def do_OPTIONS(self) -> None: server._handle(self)   # noqa: E704,N802

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.log.line("INFO", f"Proxy server running on http://{self.host}:{self.port}")
        if on_ready:
            on_ready()  # listening now - safe to advertise (pid file etc.)
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.httpd.server_close()
            self.log.line("INFO", "Proxy server stopped")

    def shutdown(self) -> None:
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        h.send_response(status)
        if body is not None:
            h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)

    def _cors(self, h: BaseHTTPRequestHandler) -> Dict[str, str]:
        origin = h.headers.get("Origin")
        if origin in (f"http://localhost:{self.port}", *ALLOWED_ORIGINS):
            return {"Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept, Authorization, X-Inferenceport-Session"}
        return {}

    def _authorize(self, token: str) -> Tuple[int, Optional[Dict[str, Any]], str]:
        """Returns (status, error_body, role). status 200 means allowed."""
        if token in self.api_keys:
            return 200, None, "admin"
        if not self.verifier or not self.users:
            return 401, {"error": "Token verification failed"}, "member"
        status, result = self.verifier.verify(token, list(self.users))
        if status == 429:
            msg = (result or {}).get("error") if isinstance(result, dict) else None
            return 429, {"error": msg or "Token verification limit reached for today.", "notice": (result or {}).get("notice")}, "member"
        if status != 200 or not result or not result.get("found") or not result.get("email"):
            return 401, {"error": "Token verification failed"}, "member"
        email = str(result["email"]).lower()
        if email not in self.users:
            return 403, {"error": "Access denied"}, "member"
        return 200, None, self.users[email]

    # -- request handling --------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        ip = h.client_address[0]
        cors = self._cors(h)
        path = h.path.split("?", 1)[0]
        if h.command == "OPTIONS":
            return self._send(h, 204 if cors else 403, None, cors)
        if h.command == "GET" and path == "/__health":
            return self._send(h, 204 if self.limiter.allow(ip) else 429, None, cors)

        auth = h.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        if h.command == "GET" and path == CAPABILITIES:
            return self._send(h, 200, {"supported": False, "version": 1, "reason": "ipx-server-does-not-implement-encryption"}, cors)
        if not token:
            return self._send(h, 401, {"error": "Missing Authorization header"}, cors)
        if h.headers.get("X-Inferenceport-Session") or path == HANDSHAKE:
            return self._send(h, 400, {"error": "Encrypted sessions are not supported by this server; retry without encryption"}, cors)
        if not self.limiter.allow(ip):
            return self._send(h, 429, {"error": "Too many requests"}, cors)

        self.log.line("INFO", f"[Connection] From={hash_ip(ip)} Path={sanitize(h.path)} Token={mask(token)}")
        status, err, role = self._authorize(token)
        if status != 200:
            return self._send(h, status, err, cors)
        if role != "admin" and h.command not in ("GET", "HEAD") and WRITE_PATTERN.search(h.path):
            return self._send(h, 403, {"error": "Insufficient permissions"}, cors)
        if path == "/sysinfo":
            return self._send(h, 501, {"error": "Hardware rating is not available on this server"}, cors)
        self._forward(h, cors)

    def _forward(self, h: BaseHTTPRequestHandler, cors: Dict[str, str]) -> None:
        if h.path.startswith(("http://", "https://")) or h.path.startswith("//"):
            return self._send(h, 400, {"error": "Absolute URLs not allowed"}, cors)
        length = int(h.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(h, 413, {"error": "Request body too large"}, cors)
        body = h.rfile.read(length) if length else None
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        self.log.line("INFO", f"[Proxy] Forwarding -> {sanitize(h.command)} {sanitize(h.path.split('?', 1)[0])}")
        conn = http.client.HTTPConnection(self.ollama.hostname, self.ollama.port or 11434, timeout=600)
        try:
            conn.request(h.command, h.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            self.log.line("ERROR", f"[Proxy] Request error: {e}")
            return self._send(h, 502, {"error": "Ollama is not reachable on the host"}, cors)
        try:
            h.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                h.send_header(k, v)
            for k, v in cors.items():
                h.send_header(k, v)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            while True:
                chunk = resp.read(8192) if resp.getheader("content-length") else resp.read1(8192)
                if not chunk:
                    break
                h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                h.wfile.flush()
            h.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
