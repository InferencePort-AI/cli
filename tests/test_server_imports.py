import http.client
import json
import threading
import time

import pytest

from ipx.errors import IpxError

# ----------------------------------------------------------------- host proxy


@pytest.fixture()
def proxy(ollama, home):
    from ipx.proxy import ProxyServer

    class StubVerifier:
        def __init__(self):
            self.calls = []

        def verify(self, token, emails):
            self.calls.append((token, emails))
            return {"good-admin": (200, {"found": True, "email": "boss@acme.com"}),
                    "good-member": (200, {"found": True, "email": "dev@acme.com"}),
                    "stranger": (200, {"found": True, "email": "eve@evil.com"}),
                    "limited": (429, {"error": "limit"})}.get(token, (401, None))

    ollama.route("GET", "/api/tags", body={"models": [{"name": "m"}]})
    ollama.route("POST", "/api/chat", body='{"message":{"content":"hi"},"done":true}\n')
    ollama.route("POST", "/api/pull", body={"status": "success"})
    ollama.route("DELETE", "/api/delete", body={})
    ver = StubVerifier()
    srv = ProxyServer(host="127.0.0.1", port=0, ollama_url=ollama.url,
                      users=[{"email": "boss@acme.com", "role": "admin"}, {"email": "dev@acme.com", "role": "member"}],
                      api_keys=["ipxs_key"], verifier=ver)
    # bind an ephemeral port by serving in a thread
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    for _ in range(100):
        if srv.httpd:
            break
        time.sleep(0.02)
    srv.port = srv.httpd.server_address[1]
    srv.verifier_stub = ver
    yield srv
    srv.shutdown()
    t.join(2)


def call(srv, method, path, token=None, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_health_is_open(proxy):
    assert call(proxy, "GET", "/__health")[0] == 204


def test_missing_token_401(proxy):
    status, body = call(proxy, "GET", "/api/tags")
    assert status == 401 and b"Missing Authorization" in body


def test_server_api_key_is_admin(proxy):
    assert call(proxy, "GET", "/api/tags", "ipxs_key")[0] == 200
    assert call(proxy, "POST", "/api/pull", "ipxs_key", {"model": "x"})[0] == 200


def test_verified_member_can_chat_but_not_manage(proxy):
    status, body = call(proxy, "POST", "/api/chat", "good-member", {"model": "m", "messages": []})
    assert status == 200 and b"hi" in body
    assert call(proxy, "POST", "/api/pull", "good-member", {"model": "x"})[0] == 403
    assert call(proxy, "DELETE", "/api/delete", "good-member", {"model": "x"})[0] == 403
    assert call(proxy, "GET", "/api/tags", "good-member")[0] == 200


def test_admin_can_manage(proxy):
    assert call(proxy, "POST", "/api/pull", "good-admin", {"model": "x"})[0] == 200


def test_unlisted_or_bad_tokens_rejected(proxy):
    assert call(proxy, "GET", "/api/tags", "stranger")[0] == 403
    assert call(proxy, "GET", "/api/tags", "garbage")[0] == 401
    assert call(proxy, "GET", "/api/tags", "limited")[0] == 429


def test_capabilities_requires_verification_and_unknown_session_rejected(proxy):
    status, body = call(proxy, "GET", "/__inferenceport/crypto-capabilities")
    assert status == 200 and json.loads(body)["supported"] is False
    assert call(proxy, "GET", "/api/tags", "ipxs_key", headers={"X-Inferenceport-Session": "abc"})[0] == 401


def test_absolute_url_rejected(proxy):
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    c.request("GET", "http://evil.example/steal", headers={"Authorization": "Bearer ipxs_key"})
    assert c.getresponse().status == 400


def test_cors_only_for_known_origins(proxy):
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    c.request("OPTIONS", "/api/tags", headers={"Origin": "https://evil.example"})
    assert c.getresponse().status == 403
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    c.request("OPTIONS", "/api/tags", headers={"Origin": "https://inference.js.org"})
    assert c.getresponse().status == 204


def test_ollama_down_gives_500(home):
    from ipx.proxy import ProxyServer

    srv = ProxyServer(host="127.0.0.1", port=0, ollama_url="http://127.0.0.1:1", users=[], api_keys=["k"], verifier=None)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    for _ in range(100):
        if srv.httpd:
            break
        time.sleep(0.02)
    srv.port = srv.httpd.server_address[1]
    assert call(srv, "GET", "/api/tags", "k")[0] == 500
    srv.shutdown()


def test_parse_users():
    from ipx.commands.server import parse_users

    assert parse_users(["A@b.co", "c@d.io:admin"]) == [{"email": "A@b.co", "role": "member"}, {"email": "c@d.io", "role": "admin"}]
    with pytest.raises(IpxError):
        parse_users(["not-an-email"])
    with pytest.raises(IpxError):
        parse_users(["a@b.co:owner"])


def test_server_start_refuses_without_ollama(ipx, monkeypatch):
    monkeypatch.setenv("IPX_OLLAMA_URL", "http://127.0.0.1:1")
    with pytest.raises(IpxError) as e:
        ipx("server", "start", "--generate-key")
    assert "Ollama is not running" in e.value.message


def test_server_start_needs_someone_to_allow(ipx):
    ipx.ollama.route("GET", "/api/version", body={"version": "0.9"})
    with pytest.raises(IpxError) as e:
        ipx("server", "start")
    assert "Nobody would be able to connect" in e.value.message


def test_server_blocked_by_org_policy(ipx):
    from conftest import sign_in

    from ipx.config import Config

    sign_in(ipx)
    Config().set_org("acme")
    ipx.ollama.route("GET", "/api/version", body={"version": "0.9"})
    ipx.fake.route("GET", "/v1/orgs/me/desktop-policy", body={"orgs": [{"slug": "acme", "name": "Acme", "managed": True, "can_host_server": False,
                                                                       "policy": {"server_hosting": "admins"}}]})
    with pytest.raises(IpxError) as e:
        ipx("server", "start", "--generate-key")
    assert "does not allow" in e.value.message


def test_server_status_when_idle(ipx):
    r = ipx("--json", "server", "status")
    assert json.loads(r.output)["running"] is False


# ----------------------------------------------------------------- imports


def desktop_dir(home):

    from ipx.config import desktop_data_dir

    return desktop_data_dir()


def test_import_website_and_list_and_rm(ipx, home, monkeypatch):
    import ipx.commands.imports as imp

    monkeypatch.setattr(imp, "fetch_title", lambda url: "Example Site")
    r = ipx("import", "website", "example.com")
    assert r.exit_code == 0, r.output
    files = list((desktop_dir(home) / "websites").glob("*.import"))
    assert len(files) == 1
    cfg = json.loads(files[0].read_text())
    assert cfg["type"] == "website" and cfg["url"] == "https://example.com" and cfg["title"] == "Example Site"
    assert set(cfg) == {"type", "title", "author", "emoji", "background", "sdk", "short_description", "url"}
    assert "Example Site" in ipx("import", "list").output
    with pytest.raises(IpxError):
        ipx("import", "website", "https://example.com")  # duplicate
    ipx("import", "rm", "https://example.com")
    assert not list((desktop_dir(home) / "websites").glob("*.import"))


def test_import_space_uses_hf_metadata(ipx, home, monkeypatch):
    import ipx.commands.imports as imp
    from ipx.http import Response

    seen = {}

    def fake_raw(method, url, **kw):
        seen["url"] = url
        body = json.dumps({"sdk": "gradio", "cardData": {"emoji": "🎨", "colorFrom": "#111111", "colorTo": "#222222", "short_description": "Draws things"}}).encode()
        return Response(200, {"content-type": "application/json"}, body)

    monkeypatch.setattr(imp, "raw_request", fake_raw)
    ipx("import", "space", "someone/cool-space")
    assert seen["url"] == "https://huggingface.co/api/spaces/someone/cool-space"
    f = desktop_dir(home) / "spaces" / "someone____Cool_Space.import"
    cfg = json.loads(f.read_text())
    assert cfg["type"] == "space" and cfg["author"] == "someone" and cfg["title"] == "Cool Space" and cfg["sdk"] == "gradio"
    assert cfg["background"] == "linear-gradient(to right, #111111, #222222)"


def test_import_file_validates(ipx, home, tmp_path):
    bad = tmp_path / "bad.import"
    bad.write_text(json.dumps({"type": "other"}))
    with pytest.raises(IpxError):
        ipx("import", "file", str(bad))
    good = tmp_path / "ok.import"
    good.write_text(json.dumps({"type": "website", "title": "T", "url": "https://t.example", "author": "", "emoji": "x", "background": "b", "sdk": "unknown", "short_description": ""}))
    ipx("import", "file", str(good))
    assert len(list((desktop_dir(home) / "websites").glob("*.import"))) == 1


# ----------------------------------------------------------------- pid file safety


def test_stale_pidfile_with_recycled_pid_is_never_acted_on(home):
    """A pid file pointing at a live but unrelated process (our own pid, no server) must be discarded, not killed."""
    import os

    from ipx.commands import server as srvcmd

    srvcmd.pid_path().write_text(json.dumps({"pid": os.getpid(), "port": 1, "host": "127.0.0.1", "started": time.time()}))
    assert srvcmd.pid_alive(os.getpid())
    assert srvcmd.running() is None
    assert not srvcmd.pid_path().exists()


def test_stop_with_stale_pidfile_kills_nothing(ipx, monkeypatch):
    import os

    from ipx.commands import server as srvcmd

    srvcmd.pid_path().write_text(json.dumps({"pid": os.getpid(), "port": 1, "host": "0.0.0.0"}))
    killed = []
    monkeypatch.setattr(srvcmd, "kill", lambda pid: killed.append(pid))
    r = ipx("server", "stop")
    assert "No server is running" in r.output and killed == []


def test_running_recognises_a_real_server(proxy, home):
    import os

    from ipx.commands import server as srvcmd

    srvcmd.pid_path().write_text(json.dumps({"pid": os.getpid(), "port": proxy.port, "host": "0.0.0.0"}))
    assert srvcmd.running()["port"] == proxy.port


def test_user_verification_requires_a_user_session(ipx):
    from ipx.config import Credentials

    Credentials().store_api_key("ipa_live_x", email="svc@x.co")
    ipx.ollama.route("GET", "/api/version", body={"version": "0.9"})
    with pytest.raises(IpxError) as e:
        ipx("server", "start", "--allow", "a@b.co")
    assert "not an API key" in e.value.message


def test_running_host_revoked_on_policy_change(monkeypatch):
    from types import SimpleNamespace

    from ipx.commands.server import monitor_host_policy
    class Stop:
        def wait(self, seconds):
            assert seconds == 300
            return False
    stopped, logged = [], []
    srv = SimpleNamespace(shutdown=lambda: stopped.append(True), log=SimpleNamespace(line=lambda *a: logged.append(a)))
    monkeypatch.setattr("ipx.commands.server.policy.host_settings", lambda app: {"managed": True, "access": "members", "users": []})
    monitor_host_policy(None, srv, [{"email": "removed@test", "role": "member"}], [], Stop())
    assert stopped == [True] and logged[0][0] == "WARN"


def test_real_detached_cli_lifecycle(home, ollama, monkeypatch):
    import os
    import socket
    import subprocess
    import sys
    monkeypatch.setenv("IPX_OLLAMA_URL", ollama.url)
    # APPDATA is isolated by home; preserve this interpreter's dependencies
    # when Python computes a different Windows user-site path in the child.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    ollama.route("GET", "/api/version", body={"version": "fixture"})
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, "-m", "ipx", "--json", "server"]
    def run(*args):
        options = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        result = subprocess.run(command + list(args), env=dict(os.environ), capture_output=True, text=True, timeout=25, **options)
        diagnostics = ""
        if result.returncode:
            from ipx.config import config_dir
            for name in ("server-process.log", "InferencePort-Server.log", "server.pid"):
                path = config_dir() / name
                if path.exists(): diagnostics += f"\n{name}:\n" + path.read_text(encoding="utf-8", errors="replace")[-4000:]
        assert result.returncode == 0, result.stderr + diagnostics
        return json.loads(result.stdout)
    try:
        assert run("start", "--host", "127.0.0.1", "--port", str(port), "--generate-key", "--detach")["running"]
        status = run("status")
        assert status["running"] and status["healthy"] and status["port"] == port
        assert run("stop")["stopped"]
        assert not run("status")["running"]
    finally:
        run("stop")
