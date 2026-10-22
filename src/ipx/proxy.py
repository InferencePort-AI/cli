"""Standalone desktop-compatible hosting protocol; see docs/server-parity.md."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from ada_url import URL

from ipx.config import Config, Credentials, config_dir
from ipx.server_crypto import Sessions, decrypt_body
from ipx.server_http import BodyTooLarge, HTTPServer

MAX_BODY = 20 * 1024 * 1024
MAX_LOG = 10 * 1024 * 1024
RATE_LIMIT = 30
CAPABILITIES = "/__inferenceport/crypto-capabilities"
HANDSHAKE = "/__inferenceport/crypto-handshake"
WRITE_PATTERN = re.compile(r"pull|rm|delete|create|models|run", re.I)
ALLOWED_ORIGINS = ("http://localhost:52458", "https://inference.js.org")
HOP = {"host", "authorization", "connection", "content-length", "transfer-encoding", "upgrade",
       "proxy-authorization", "proxy-authenticate", "te", "trailers"}


def js_truthy(value):
    return not (value is None or value is False or isinstance(value, (str, int, float)) and not value)


def parse_json(raw):
    def invalid(value):
        raise ValueError(f"Unexpected token {value}")
    return json.loads(raw, parse_constant=invalid)


def log_path():
    return config_dir() / "InferencePort-Server.log"


def sanitize(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", re.sub(r"[\r\n\t]", " ", s)).strip()


def hash_ip(ip: str) -> str:
    return "unknown" if ip == "unknown" else hashlib.sha256(ip.encode()).hexdigest()[:8]


def mask(token: str) -> str:
    return "undefined" if not token else token[:6] + "*" * max(0, len(token) - 6) if len(token) > 6 else "*" * len(token)


class ServerLog:
    def __init__(self, echo=False):
        self.lock, self.echo = threading.Lock(), echo
        fd = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)

    def line(self, level, msg):
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        text = f"[{stamp}] [{level}] {sanitize(msg)}"
        with self.lock:
            try:
                p = log_path()
                if p.stat().st_size >= MAX_LOG:
                    p.rename(p.with_name(f"InferencePort-Server-{int(time.time() * 1000)}.log"))
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass
        if self.echo:
            print(text, flush=True)


class RateLimiter:
    def __init__(self, per_minute=RATE_LIMIT):
        self.per, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, key):
        now = int(time.time() * 1000)
        with self.lock:
            count, reset = self.hits.get(key, (0, -1))
            if now > reset:
                count, reset = 0, now + 60000
            if count >= self.per:
                return False
            self.hits[key] = count + 1, reset
            return True


class Verifier:
    def __init__(self, cfg: Config, creds: Credentials):
        from ipx.auth import supabase_config
        self.url = supabase_config(cfg)["url"] + "/functions/v1/verify_token_with_email"
        self.cfg, self.creds = cfg, creds

    def verify(self, token, emails):
        headers = {"Content-Type": "application/json"}
        try:
            from ipx.auth import refresh
            if self.creds.expiring():
                refresh(self.cfg, self.creds)
            if self.creds.auth_type == "jwt" and self.creds.access_token:
                headers["Authorization"] = f"Bearer {self.creds.access_token}"
        except Exception:
            pass  # getSession failures do not prevent the reference verification request
        url = urllib.parse.urlsplit(self.url)
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = cls(url.hostname, url.port, timeout=None)
        try:
            body = json.dumps({"token": token, "emails": emails}, separators=(",", ":"), ensure_ascii=False).encode()
            conn.putrequest("POST", url.path, skip_accept_encoding=True, skip_host=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.putheader("Host", url.netloc)
            conn.putheader("Connection", "keep-alive")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            response = conn.getresponse()
            raw = response.read()
            try:
                result = parse_json(raw)
            except ValueError:
                result = None
            return response.status, result
        except (OSError, http.client.HTTPException):
            return 500, None
        finally:
            conn.close()


class ProxyServer:
    def __init__(self, *, host, port, ollama_url, users, api_keys, verifier, echo=False, http_settings=None):
        self.host, self.port = host, port
        self.http_settings, self.max_body = http_settings, MAX_BODY
        self.ollama_url = ollama_url
        self.ollama = urllib.parse.urlsplit(ollama_url)
        self.users, self.api_keys, self.verifier = list(users), set(api_keys), verifier
        self.log, self.limiter, self.sessions = ServerLog(echo), RateLimiter(), Sessions()
        self.httpd = None
        self.connections, self.connections_lock = set(), threading.Lock()

    def serve(self, on_ready=None):
        httpd = HTTPServer((self.host, self.port), self, self.http_settings)
        self.httpd = httpd
        self.log.line("INFO", f"Proxy server running on http://localhost:{httpd.server_address[1]}")
        if on_ready:
            on_ready()
        try:
            httpd.serve_forever(poll_interval=0.1)
        finally:
            httpd.server_close()
            self.log.line("INFO", "Proxy server stopped")

    def shutdown(self):
        if self.httpd:
            with self.connections_lock:
                for conn in self.connections:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    @staticmethod
    def _send(h, status, body=None, extra=None):
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body is not None else b""
        chunked = h.request_version == "HTTP/1.1" and status != 204 and h.command != "HEAD"
        if h.request_version != "HTTP/1.1": h.close_connection = True
        h.send_response_only(status)
        h.send_header("Date", h.date_time_string())
        h.send_header("Connection", "close" if h.close_connection else "keep-alive")
        if not h.close_connection:
            h.send_header("Keep-Alive", f"timeout={int(h.settings.keep_alive_timeout)}")
        if body is not None:
            h.send_header("Content-Type", "application/json")
        if chunked:
            h.send_header("Transfer-Encoding", "chunked")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD" and status != 204:
            if chunked:
                if data:
                    h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                h.wfile.write(b"0\r\n\r\n")
            else:
                h.wfile.write(data)

    def _cors(self, h):
        origin = h.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            return {"Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept, Authorization, X-Inferenceport-Session"}
        return {}

    def _verify(self, token):
        return self.verifier.verify(token, [u["email"] for u in self.users]) if self.verifier else (500, None)

    def _identity(self, token, session=None):
        if session is None and token in self.api_keys:
            matched = next((u for u in self.users if (u.get("role") or "").lower() == "admin"),
                           self.users[0] if self.users else {"email": "__server_api_key__", "role": "admin"})
            return 200, None, matched
        self.log.line("INFO", f"Verifying {'session request token' if session else 'token'}: {mask(token)}")
        status, result = self._verify(token)
        result = result if isinstance(result, dict) else {}
        if status == 429:
            error = result.get("error")
            body = {"error": error if isinstance(error, str) else "Token verification limit reached for today."}
            if isinstance(result.get("notice"), str):
                body["notice"] = result["notice"]
            if result.get("usage") is not None:
                body["usage"] = result["usage"]
            return 429, body, None
        if status != 200 or not js_truthy(result.get("found")) or not js_truthy(result.get("email")):
            return 401, {"error": "Token verification failed"}, None
        if session and result["email"] != session.email:
            return 403, {"error": "Session token email mismatch"}, None
        matched = next((u for u in self.users if u["email"] == result["email"]), None)
        return (200, None, matched) if matched else (403, {"error": "Access denied"}, None)

    def _read(self, h):
        return h.read_body()

    def _handle(self, h):
        try:
            self._route(h)
        except (BodyTooLarge, BrokenPipeError, ConnectionResetError):
            h.close_connection = True

    def _route(self, h):
        ip, cors = h.client_address[0], self._cors(h)
        path = h.path.split("?", 1)[0] or "/"
        send = lambda status, body=None: self._send(h, status, body, cors)
        if h.command == "OPTIONS":
            return send(204 if cors else 403)
        if h.command == "GET" and h.path == "/__health":
            return send(204 if self.limiter.allow(ip) else 429)
        auth = h.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if h.command == "GET" and path == CAPABILITIES:
            supported = False
            if token and token not in self.api_keys:
                status, result = self._verify(token)
                supported = status == 200 and isinstance(result, dict) and js_truthy(result.get("found")) and js_truthy(result.get("email"))
            body = {"supported": supported, "version": 1}
            if not supported:
                body["reason"] = "api-verification-required"
            return send(200, body)
        self.log.line("INFO", f"[Connection] From={hash_ip(ip)} Path={sanitize(h.path or '/')}")
        sid = h.headers.get("X-Inferenceport-Session", "").strip()
        session = self.sessions.lookup(sid) if sid else None
        if sid and not session:
            return send(401, {"error": "Invalid or expired session"})
        if not token:
            return send(401, {"error": "Missing Authorization header"})
        status, error, matched = self._identity(token, session)
        if status != 200:
            return send(status, error)
        if h.command == "POST" and path == HANDSHAKE:
            try:
                raw = self._read(h)
                try:
                    obj = parse_json(raw.decode("utf-8", "replace") or "{}")
                except ValueError:
                    return send(400, {"error": "Invalid JSON"})
                if obj is None:
                    return send(500, {"error": "Failed to read body"})
                if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) for k in ("clientPublicKey", "clientNonce")):
                    return send(400, {"error": "Missing clientPublicKey or clientNonce"})
                try:
                    payload = self.sessions.establish(obj["clientPublicKey"], obj["clientNonce"], matched["email"], matched.get("role"))
                except ValueError as err:
                    self.log.line("ERROR", f"Crypto handshake failed: {err}")
                    return send(400, {"error": "Handshake failed"})
                return send(200, payload)
            except BodyTooLarge:
                raise
            except (OSError, ValueError):
                return send(500, {"error": "Failed to read body"})
        if (matched.get("role") or "member").lower() != "admin" and h.command != "GET" and WRITE_PATTERN.search(h.path):
            return send(403, {"error": "Insufficient permissions"})
        if not self.limiter.allow(ip):
            return send(429)
        if h.command == "POST" and path == "/sysinfo":
            try:
                raw = self._read(h)
                if session:
                    raw = decrypt_body(session.key, raw)
            except BodyTooLarge:
                raise
            except (OSError, ValueError):
                return send(500, {"error": "Sysinfo read failed"})
            try:
                obj = parse_json(raw.decode("utf-8", "replace") or "{}")
                size = obj.get("modelSizeRaw") if isinstance(obj, dict) else None
                if obj is None:
                    raise ValueError("Cannot read properties of null")
                if not js_truthy(size):
                    return send(400, {"error": "Missing modelSizeRaw"})
                from ipx.server_hardware import get_hardware_rating
                return send(200, get_hardware_rating(size))
            except Exception as err:
                self.log.line("ERROR", f"Sysinfo error: {err}")
                return send(520, {"error": "Sysinfo failure"})
        if h.path.startswith("//") or urllib.parse.urlsplit(h.path).scheme:
            return send(400, {"error": "Absolute URLs not allowed"})
        raw = self._read(h)
        self._forward(h, cors, decrypt_body(session.key, raw) if session else raw)

    def _forward(self, h, cors, body):
        target_url = URL(h.path, self.ollama_url)
        # WHATWG treats backslashes as slashes for HTTP URLs. Reject every
        # resolved cross-origin target, including mixed /\ network paths.
        if target_url.origin != URL(self.ollama_url).origin:
            return self._send(h, 400, {"error": "Absolute URLs not allowed"}, cors)
        target = urllib.parse.urlsplit(target_url.href)
        path = urllib.parse.urlunsplit(("", "", target.path or "/", target.query, ""))
        self.log.line("INFO", f"[Proxy] Forwarding -> {h.command} {target.scheme}://{target.netloc}{target.path}")
        headers = {k.lower(): v for k, v in h.headers.items() if k.lower() not in HOP}
        if body:
            headers["content-length"] = str(len(body))
        conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=None)
        try:
            conn.putrequest(h.command, path, skip_accept_encoding=True, skip_host=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.putheader("Host", target.netloc)
            conn.putheader("Connection", "keep-alive")
            if not body and h.command in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", "0")
            conn.endheaders(body if body and h.command not in ("GET", "HEAD") else None)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as err:
            conn.close()
            self.log.line("ERROR", f"[Proxy] Request error: {err}")
            return self._send(h, 500, {"error": "Proxy error"}, cors)
        try:
            h.send_response_only(resp.status)
            response_headers = {k.lower(): v for k, v in cors.items()}
            response_headers.update({k.lower(): v for k, v in resp.getheaders() if k.lower() != "set-cookie"})
            chunked = resp.getheader("transfer-encoding", "").lower() == "chunked"
            has_body = h.command != "HEAD" and resp.status not in (204, 304) and resp.status >= 200
            if not resp.getheader("content-length") and not chunked and has_body:
                chunked = True
                response_headers["transfer-encoding"] = "chunked"
            if "connection" not in response_headers:
                response_headers["connection"] = "close" if h.close_connection else "keep-alive"
                if not h.close_connection:
                    response_headers["keep-alive"] = f"timeout={int(h.settings.keep_alive_timeout)}"
            if not resp.getheader("date"):
                response_headers["date"] = h.date_time_string()
            for k, v in response_headers.items():
                h.send_header(k, v)
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    h.send_header(k, v)
            if resp.getheader("connection", "").lower() == "close":
                h.close_connection = True
            h.end_headers()
            if has_body:
                while True:
                    part = resp.read1(65536)
                    if not part:
                        break
                    h.wfile.write((f"{len(part):x}\r\n".encode() + part + b"\r\n") if chunked else part)
                    h.wfile.flush()
                if chunked:
                    h.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            h.close_connection = True
        finally:
            conn.close()
