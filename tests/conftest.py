"""Test harness: an in-process fake InferencePort API / Supabase / Ollama, and a CLI runner pointed at it."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple

import pytest
from click.testing import CliRunner


class Fake:
    """A tiny programmable HTTP server. Register routes with fake.route(method, path, handler)."""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Callable[..., Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                rec = {"method": self.command, "path": path, "query": self.path.partition("?")[2], "headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw}
                try:
                    rec["json"] = json.loads(raw) if raw and raw[:1] in b"{[" else None
                except ValueError:
                    rec["json"] = None
                fake.calls.append(rec)
                handler = fake.routes.get((self.command, path))
                if handler is None:
                    status, headers, body = 404, {}, {"detail": f"no route {self.command} {path}"}
                else:
                    status, headers, body = handler(rec)
                if isinstance(body, (dict, list)):
                    data = json.dumps(body).encode()
                    headers = {"Content-Type": "application/json", **headers}
                elif isinstance(body, str):
                    data = body.encode()
                else:
                    data = body
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True).start()

    def route(self, method: str, path: str, handler=None, *, status: int = 200, body: Any = None, headers: Dict[str, str] = None):
        if handler is None:
            handler = lambda rec, s=status, b=body, h=headers: (s, h or {}, b if b is not None else {})  # noqa: E731
        self.routes[(method.upper(), path)] = handler

    def last(self, path: str) -> Dict[str, Any]:
        return [c for c in self.calls if c["path"] == path][-1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def fake():
    f = Fake()
    yield f
    f.close()


@pytest.fixture()
def ollama():
    f = Fake()
    yield f
    f.close()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("IPX_HOME", str(tmp_path / "ipxhome"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NO_COLOR", "1")
    for var in ("IPX_TOKEN", "IPX_API_KEY", "IPX_ORG", "IPX_BASE_URL", "IPX_OLLAMA_URL", "IPX_JSON"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture()
def ipx(home, fake, ollama, monkeypatch):
    """Invoke the CLI against the fake API. Returns a callable ipx(*args, input=None)."""
    from ipx import ui
    from ipx.cli import _register, root

    monkeypatch.setenv("IPX_BASE_URL", fake.url)
    monkeypatch.setenv("IPX_OLLAMA_URL", ollama.url)
    _register()
    # Supabase calls go to the fake too.
    from ipx.config import Config

    cfg = Config()
    cfg.set("supabase", {"url": fake.url + "/sb", "publishableKey": "pk_test"})
    runner = CliRunner()

    def run(*args: str, input: str = None, env: Dict[str, str] = None):
        ui.STATE.update(json=False, quiet=False, yes=False)
        return runner.invoke(root, list(args), input=input, env=env, catch_exceptions=False, prog_name="ipx")

    run.fake, run.ollama, run.home = fake, ollama, home
    return run


def sign_in(ipx, token="tok_abc", email="me@example.com"):
    """Persist a fake signed-in session without going through the login flow."""
    from ipx.config import Credentials

    c = Credentials()
    c.store_session({"access_token": token, "refresh_token": "rt_1", "expires_at": 4102444800, "user": {"id": "u1", "email": email}}, email=email)
