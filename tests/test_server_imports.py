import http.client
import json
import threading
import time
import urllib.parse

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


def test_capabilities_says_unsupported_and_session_rejected(proxy):
    status, body = call(proxy, "GET", "/__inferenceport/crypto-capabilities")
    assert status == 200 and json.loads(body)["supported"] is False
    assert call(proxy, "GET", "/api/tags", "ipxs_key", headers={"X-Inferenceport-Session": "abc"})[0] == 400


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


def test_ollama_down_gives_502(home):
    from ipx.proxy import ProxyServer

    srv = ProxyServer(host="127.0.0.1", port=0, ollama_url="http://127.0.0.1:1", users=[], api_keys=["k"], verifier=None)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    for _ in range(100):
        if srv.httpd:
            break
        time.sleep(0.02)
    srv.port = srv.httpd.server_address[1]
    assert call(srv, "GET", "/api/tags", "k")[0] == 502
    srv.shutdown()


def test_parse_users():
    from ipx.commands.server import parse_users

    assert parse_users(["A@b.co", "c@d.io:admin"]) == [{"email": "a@b.co", "role": "member"}, {"email": "c@d.io", "role": "admin"}]
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
    import sys

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
