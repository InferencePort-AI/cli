"""HTTP-boundary comparisons against the unchanged desktop TypeScript sources.

Set IPX_DESKTOP_SOURCE to inferenceport-ai/src. Node and the desktop TypeScript
package are test dependencies only; the production server never executes Node.
"""
import concurrent.futures
import copy
import http.client
import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ipx.proxy import ProxyServer, RateLimiter
from ipx.server_crypto import Session, Sessions, decode, decrypt_body, derive_key, encode
from ipx.server_hardware import get_hardware_rating

USERS = [{"email": "Admin@host.test", "role": "ADMIN"}, {"email": "member@host.test", "role": "member"},
         {"email": "other@host.test", "role": "invalid"}]
SNAPSHOT = {"cpu": {"brand": "Fixture CPU", "cores": 8, "cache": {"l3": 0}}, "flags": "avx2",
            "mem": {"total": 32 * 1024**3, "available": 20 * 1024**3}, "controllers": [], "backend": "unknown"}
ROOT = Path(os.environ.get("IPX_DESKTOP_SOURCE", Path(__file__).parents[2] / "inferenceport-ai" / "src"))
LOADER = Path(__file__).with_name("desktop_reference.cjs")
REFERENCE_NODE = os.environ.get("IPX_REFERENCE_NODE", "node")


def verification(token):
    if token == "limited": return 429, {"error": "quota", "notice": "tomorrow", "usage": {"used": 10}}
    if token == "limited-empty": return 429, {}
    if token == "outage": return 503, None
    if token in ("admin", "member", "other", "stranger", "case"):
        return 200, {"found": True, "email": {"admin": USERS[0]["email"], "member": USERS[1]["email"],
                    "other": USERS[2]["email"], "stranger": "stranger@host.test", "case": "admin@host.test"}[token]}
    return 401, {"found": False}


class Verifier:
    def verify(self, token, emails):
        return verification(token)


def request(port, method, path, token=None, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(headers or {})
    if token is not None: headers["Authorization"] = "Bearer " + token
    if isinstance(body, (dict, list)): body = json.dumps(body, separators=(",", ":"))
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    # Only the nondeterministic Date header is excluded. Framing and
    # connection headers remain part of the comparison.
    result = response.status, {k.lower(): v for k, v in response.getheaders()
                               if k.lower() not in ("date",)}, raw
    conn.close()
    return result


@pytest.fixture
def python_host(home, ollama):
    servers = []
    def start(users=USERS, port=0, http_settings=None):
        server = ProxyServer(host="127.0.0.1", port=port, ollama_url=ollama.url, users=users, api_keys=["key"], verifier=Verifier(), http_settings=http_settings)
        ready = threading.Event()
        errors = []
        def serve():
            try: server.serve(ready.set)
            except Exception as err:
                errors.append(err)
                ready.set()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert ready.wait(5)
        if errors: raise errors[0]
        servers.append((server, thread))
        return server
    yield start
    for server, thread in servers:
        server.shutdown()
        thread.join(5)


@pytest.fixture
def reference_env(home, fake, ollama, tmp_path, request):
    if not (ROOT / "node_modules/typescript").exists() and not os.environ.get("IPX_TYPESCRIPT_PATH"):
        pytest.skip("Set IPX_DESKTOP_SOURCE and install desktop dev dependencies for differential tests")
    fake.route("POST", "/verify", lambda rec: (*verification(rec["json"]["token"])[:1], {}, verification(rec["json"]["token"])[1]))
    return dict(os.environ, **getattr(request, "param", {}), PARITY_VERIFY_URL=fake.url + "/verify", PARITY_OLLAMA_URL=ollama.url,
                PARITY_LOG_DIR=str(tmp_path), PARITY_CLOCK_FILE=str(tmp_path / "clock"), PARITY_USERS=json.dumps(USERS), PARITY_HARDWARE=json.dumps(SNAPSHOT))


@pytest.fixture
def reference(reference_env):
    proc = subprocess.Popen([REFERENCE_NODE, str(LOADER), str(ROOT)], env=reference_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    def read():
        for line in proc.stdout: lines.put(line)
        lines.put("EXIT")
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    output = []
    try:
        while True:
            line = lines.get(timeout=15)
            output.append(line)
            if line.startswith("READY "):
                yield int(line.split()[1])
                break
            assert line != "EXIT", "".join(output)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        reader.join(5)
        proc.stdout.close()


CASES = [
    ("OPTIONS", "/api/chat", None, None, {"Origin": "https://inference.js.org"}),
    ("OPTIONS", "/", None, None, {"Origin": "http://localhost:52458"}),
    ("OPTIONS", "/", None, None, {"Origin": "http://localhost:1234"}),
    ("GET", "/__health", None, None, {}), ("GET", "/__health?query=1", None, None, {}),
    *[("GET", "/__inferenceport/crypto-capabilities?x=1", t, None, {}) for t in (None, "key", "admin", "stranger", "bad", "limited", "outage")],
    *[("POST", "/api/chat?x=a%2Fb", t, {"model": "fixture"}, {}) for t in (None, "key", "admin", "member", "other", "stranger", "case", "bad", "expired", "limited", "limited-empty", "outage")],
    *[(m, p, "member", None, {}) for m in ("GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "TRACE")
      for p in ("/api/tags", "/api/pull", "/api/chat?model=run")],
    ("GET", "/api/tags", None, None, {"Authorization": "bearer admin"}),
    ("GET", "/api/tags", "admin", None, {"X-Inferenceport-Session": "unknown"}),
    ("GET", "/api/tags", None, None, {"X-Inferenceport-Session": "unknown"}),
    *[("POST", "/__inferenceport/crypto-handshake", "admin", b, {}) for b in ("{", {}, [], None, {"clientPublicKey": "bad", "clientNonce": "!"})],
    *[("POST", "/sysinfo", "admin", b, {}) for b in ("{", {}, [], "null", {"modelSizeRaw": 2})],
    ("GET", "http://invalid.test/", "key", None, {}),
]


@pytest.mark.parametrize("method,path,token,body,headers", CASES)
def test_http_boundary(reference, python_host, ollama, method, path, token, body, headers):
    def echo(rec):
        return 201, {"Content-Type": "application/json", "X-Upstream": "yes"}, {"method": rec["method"], "query": rec["query"], "body": rec["raw"].decode(), "path": rec["path"]}
    for method_ in ("GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "TRACE"):
        for path_ in ("/api/chat", "/api/tags", "/api/pull"):
            ollama.route(method_, path_, echo)
    server = python_host()
    assert request(server.httpd.server_port, method, path, token, body, headers) == request(reference, method, path, token, body, headers)


def handshake(port, token="admin", nonce="! malformed nonce !"):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    status, _, raw = request(port, "POST", "/__inferenceport/crypto-handshake", token, {"clientPublicKey": encode(public), "clientNonce": nonce})
    assert status == 200, raw
    payload = json.loads(raw)
    assert len(decode(payload["sessionId"])) == 24
    assert len(decode(payload["challenge"])) == 32
    assert len(decode(payload["serverNonce"])) == 16
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(payload["serverPublicKey"]))
    key = derive_key(private.exchange(ec.ECDH(), peer), payload["challenge"], payload["serverNonce"], nonce)
    return payload["sessionId"], key


def envelope(key, data):
    iv = os.urandom(12)
    encrypted = AESGCM(key).encrypt(iv, data, None)
    return {"type": "enc", "iv": encode(iv), "tag": encode(encrypted[-16:]), "data": encode(encrypted[:-16])}


def test_encrypted_interop_and_isolation(reference, python_host, ollama):
    ollama.route("POST", "/api/chat", lambda rec: (200, {"Content-Type": "application/json"}, rec["raw"]))
    for port in (reference, python_host().httpd.server_port):
        sid, key = handshake(port)
        headers = {"X-Inferenceport-Session": sid}
        plain = b'{"model":"test","messages":[]}'
        enc = envelope(key, plain)
        for _ in range(2):  # Reference deliberately permits replay.
            assert request(port, "POST", "/api/chat", "admin", enc, headers)[2] == plain
        assert request(port, "POST", "/api/chat", "member", enc, headers)[0] == 403
        assert request(port, "POST", "/api/chat", None, enc, headers)[0] == 401
        assert request(port, "POST", "/api/chat", "key", enc, headers)[0] == 401
        sid2, key2 = handshake(port)
        wrong = envelope(key2, plain)
        assert request(port, "POST", "/api/chat", "admin", wrong, headers)[2] == json.dumps(wrong, separators=(",", ":")).encode()
        assert sid != sid2 and key != key2
        enc["tag"] = encode(bytes(16))
        assert request(port, "POST", "/api/chat", "admin", enc, headers)[2] == json.dumps(enc, separators=(",", ":")).encode()


def test_shared_fixed_window(reference, python_host, ollama):
    ollama.route("GET", "/api/tags", body={})
    for port in (reference, python_host().httpd.server_port):
        for _ in range(15):
            assert request(port, "GET", "/__health")[0] == 204
            assert request(port, "GET", "/api/tags", "key")[0] == 200
        assert request(port, "GET", "/__health")[0] == 429
        assert request(port, "GET", "/api/tags", "key")[0] == 429
        assert request(port, "GET", "/api/tags", "bad")[0] == 401
        assert handshake(port)[0]


def test_rate_and_session_exact_boundaries(monkeypatch):
    import ipx.proxy as proxy
    now = [100.0]
    monkeypatch.setattr(proxy.time, "time", lambda: now[0])
    limiter = RateLimiter(2)
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 160.0
    assert not limiter.allow("a")
    now[0] = 160.001
    assert limiter.allow("a")
    sessions = Sessions()
    sessions.records["s"] = Session(b"k" * 32, "email", "member", 160002)
    assert sessions.lookup("s")
    now[0] = 160.002
    assert sessions.lookup("s") is None
    assert not sessions.records


def run_reference(mode, env):
    output = subprocess.check_output([REFERENCE_NODE, str(LOADER), str(ROOT), mode], env=env, text=True, encoding="utf-8", timeout=40)
    return json.loads(next(line[7:] for line in output.splitlines() if line.startswith("RESULT ")))


def test_shared_desktop_crypto_vectors(reference_env):
    vector = run_reference("vectors", reference_env)
    private = ec.derive_private_key(1, ec.SECP256R1())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(vector["b"]))
    secret = private.exchange(ec.ECDH(), peer)
    assert secret.hex() == vector["secret"]
    key = derive_key(secret, "challenge", vector["nonce"], vector["nonce"])
    assert key.hex() == vector["key"]
    assert decrypt_body(key, json.dumps(vector["envelope"]).encode()) == b"desktop vector"


@pytest.mark.parametrize("backend,controllers", [("unknown", []), ("cpu", []), ("cuda", [{"vendor": "NVIDIA", "model": "RTX 4090", "vram": 24576, "memoryFree": 20480}]),
    ("rocm", [{"vendor": "AMD", "model": "RX 7900 XTX", "vram": 24576}]), ("cuda", [{"vendor": "AMD", "model": "RX 6600", "vram": 8192}]),
    ("cpu", [{"vendor": "NVIDIA", "model": "RTX 3060", "vram": 12288}])])
def test_hardware_algorithms(reference_env, backend, controllers):
    snap = copy.deepcopy(SNAPSHOT)
    snap.update(backend=backend, controllers=controllers)
    sizes = ["bad", "0", "0.5b", "500m", "3B", "8b", "14b", "34b", "70b", "91b", "e4", "7trailing"]
    env = dict(reference_env, PARITY_HARDWARE=json.dumps(snap), PARITY_SIZES=json.dumps(sizes))
    expected = run_reference("rating", env)
    assert [get_hardware_rating(size, snap) for size in sizes] == expected


def test_real_desktop_secure_client(reference_env, python_host, ollama):
    ollama.route("POST", "/api/chat", body={"message": {"content": "interoperable"}, "done": True})
    server = python_host()
    reference_env = dict(reference_env, PARITY_CLIENT_URL=f"http://127.0.0.1:{server.httpd.server_port}")
    result = run_reference("client", reference_env)
    assert result["status"] == 200 and "interoperable" in result["body"]
    assert len(server.sessions.records) == 1
    assert ollama.last("/api/chat")["json"] == {"model": "test", "messages": []}


def test_parallel_sessions(python_host, ollama):
    server = python_host()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(lambda _: handshake(server.httpd.server_port), range(16)))
    assert len({s for s, _ in pairs}) == 16


def test_api_key_member_role(python_host, ollama):
    server = python_host(users=[USERS[1]])
    assert request(server.httpd.server_port, "POST", "/api/pull", "key", {})[0] == 403


def test_ssrf_security_deviation(python_host):
    server = python_host()
    assert request(server.httpd.server_port, "GET", "//evil.test/", "key")[0] == 400
    assert request(server.httpd.server_port, "GET", "HTTP://evil.test/", "key")[0] == 400
    assert request(server.httpd.server_port, "GET", "/\\evil.test/", "key")[0] == 400

def test_forwarded_request_headers(reference, python_host, ollama):
    ollama.route("POST", "/api/chat", lambda rec: (200, {}, {"headers": rec["headers"]}))
    headers = {"Content-Type": "application/json", "Proxy-Authenticate": "secret", "Keep-Alive": "timeout=10", "X-Custom": "kept"}
    server = python_host()
    assert request(server.httpd.server_port, "POST", "/api/chat", "admin", {"hello": True}, headers) == request(reference, "POST", "/api/chat", "admin", {"hello": True}, headers)


def test_logging_rotation_and_redaction(home, monkeypatch):
    import ipx.proxy as proxy
    logger = proxy.ServerLog()
    monkeypatch.setattr(proxy, "MAX_LOG", 1)
    logger.line("INFO", "first")
    logger.line("WARN", "unsafe\r\n\t\x1b[31mred\x1b[0m")
    current = proxy.log_path().read_text()
    assert re.match(r"\[\d{4}-\d\d-\d\dT.*\.\d{3}Z\] \[WARN\] unsafe   red\n", current)
    assert len(list(proxy.log_path().parent.glob("InferencePort-Server-*.log"))) == 1
    assert proxy.hash_ip("unknown") == "unknown"
    assert len(proxy.hash_ip("127.0.0.1")) == 8
    assert proxy.mask("123456secret") == "123456******"
    proxy.ServerLog()
    assert not proxy.log_path().read_bytes()


def test_verification_service_request(home, fake):
    from ipx.config import Config, Credentials
    from ipx.proxy import Verifier as RealVerifier
    cfg, creds = Config(), Credentials()
    cfg.data["supabase"] = {"url": fake.url, "publishableKey": "must-not-leak"}
    creds.store_session({"access_token": "host-token", "expires_at": 9999999999})
    fake.route("POST", "/functions/v1/verify_token_with_email", body={"found": True, "email": "A@test"})
    verifier = RealVerifier(cfg, creds)
    assert verifier.verify("client-token", ["A@test"])[0] == 200
    rec = fake.calls[-1]
    assert rec["json"] == {"token": "client-token", "emails": ["A@test"]}
    assert rec["headers"]["authorization"] == "Bearer host-token"
    assert "apikey" not in rec["headers"]
    creds.clear()
    assert verifier.verify("client-token", [])[0] == 200
    assert "authorization" not in fake.calls[-1]["headers"]


def test_body_limit_and_chunked_request(reference, python_host, ollama):
    from ipx.proxy import MAX_BODY
    ollama.route("POST", "/api/chat", lambda rec: (200, {}, {"size": len(rec["raw"])}))
    for port in (reference, python_host().httpd.server_port):
        assert json.loads(request(port, "POST", "/api/chat", "key", b"x" * MAX_BODY)[2]) == {"size": MAX_BODY}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("POST", "/api/chat", b"x" * (MAX_BODY + 1), headers={"Authorization": "Bearer key"})
            with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
                conn.getresponse()
        except (BrokenPipeError, ConnectionResetError):
            pass  # both runtimes destroy an oversized request's socket
        finally:
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/chat", iter([b"abc", b"def"]), headers={"Authorization": "Bearer key"}, encode_chunked=True)
        response = conn.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"size": 6}
        conn.close()

def test_streaming_and_client_disconnect(reference, python_host, ollama):
    release = threading.Event()
    def stream():
        yield b'{"done":false}\n'
        assert release.wait(5)
        yield b'{"done":true}\n'
    ollama.route("POST", "/api/generate", lambda rec: (200, {"Content-Type": "application/x-ndjson"}, stream()))
    for port in (reference, python_host().httpd.server_port):
        release.clear()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/generate", "{}", headers={"Authorization": "Bearer key"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("transfer-encoding") == "chunked"
        assert resp.read1(100) == b'{"done":false}\n'
        release.set()
        assert resp.read() == b'{"done":true}\n'
        conn.close()
        # Disconnect during a stream; later requests still succeed.
        release.clear()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/generate", "{}", headers={"Authorization": "Bearer key"})
        resp = conn.getresponse()
        assert resp.read1(100)
        resp.close()
        conn.close()
        release.set()
        assert request(port, "GET", "/__health")[0] == 204

@pytest.mark.parametrize("body", ['{"modelSizeRaw":[]}', '{"modelSizeRaw":{}}', '{"modelSizeRaw":NaN}', '{"modelSizeRaw":Infinity}'])
def test_sysinfo_javascript_json_edges(reference, python_host, body):
    server = python_host()
    assert request(server.httpd.server_port, "POST", "/sysinfo", "admin", body) == request(reference, "POST", "/sysinfo", "admin", body)


def test_differential_expiration_and_reset_boundary(reference, reference_env, python_host, monkeypatch):
    import ipx.proxy as proxy
    now = [100000]
    clock_file = Path(reference_env["PARITY_CLOCK_FILE"])
    def tick(value):
        now[0] = value
        clock_file.write_text(str(value))
    monkeypatch.setattr(proxy.time, "time", lambda: now[0] / 1000)
    tick(100000)
    ports = (reference, python_host().httpd.server_port)
    sessions = {port: handshake(port)[0] for port in ports}
    for port in ports:
        for _ in range(30): assert request(port, "GET", "/__health")[0] == 204
        assert request(port, "GET", "/__health")[0] == 429
    tick(160000)
    for port in ports: assert request(port, "GET", "/__health")[0] == 429
    tick(160001)
    for port in ports: assert request(port, "GET", "/__health")[0] == 204
    tick(3700000)
    for port in ports:
        status, _, raw = request(port, "GET", "/api/tags", "admin", headers={"X-Inferenceport-Session": sessions[port]})
        assert status == 401 and json.loads(raw) == {"error": "Invalid or expired session"}


def raw_exchange(port, data):
    import socket
    conn = socket.create_connection(("127.0.0.1", port), timeout=3)
    conn.sendall(data)
    parts = []
    try:
        while True:
            part = conn.recv(65536)
            if not part: break
            parts.append(part)
    finally:
        conn.close()
    wire = re.sub(rb"(?im)^date: [^\r\n]*\r\n", b"", b"".join(parts))
    def canonical(message):
        if not message: return message
        head, _, body = message.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = int(lines[0].split()[1])
        if 100 <= status < 200: body = canonical(body)
        # Header order/casing is semantically unordered. Preserve duplicates,
        # framing, and every body byte, including subsequent final responses.
        return lines[0], sorted((k.lower(), v.strip()) for line in lines[1:] for k, _, v in [line.partition(b":")]), body
    return canonical(wire)


RAW_CASES = [
    b"GET / HTTP/1.1\r\nHost:\r\nConnection: close\r\n\r\n",
    b"GET /\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nConnection: close\r\nExpect: unsupported\r\nContent-Length: 0\r\n\r\n",
    b"POST /sysinfo HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer key\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n{}",
    b"GET /api/tags HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer key\t \r\n\r\n",
    b"GET /api/tags HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer key\r\nX-Duplicate: first\r\nX-Duplicate: second\r\nCookie: a=1\r\nCookie: b=2\r\n\r\n",
    b"BOGUS / HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET / HTTP/2.0\r\nHost: x\r\nConnection: close\r\n\r\n",
    b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: x\r\nBad Header: no\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: x\r\nBad: a\x00b\r\n\r\n",
    b"GET / HTTP/1.1\nHost: x\nConnection: close\n\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: +1\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
    b"POST /api/chat HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer key\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\n",
    b"CONNECT localhost:11434 HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /__health HTTP/1.0\r\n\r\n",
    b"GET /api/tags HTTP/1.0\r\n\r\n",
    b"GET /__health HTTP/1.0\r\nConnection: keep-alive, close\r\n\r\n",
    b"GET /__health HTTP/1.1\r\nHost: x\r\nConnection: upgrade, close\r\nUpgrade: websocket\r\n\r\n",
    b"GET /api/tags HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer key\r\nAuthorization: Bearer bad\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: x\r\nX-Large: " + b"a" * 17000 + b"\r\n\r\n",
]


@pytest.mark.parametrize("wire", RAW_CASES)
def test_raw_llhttp_boundary(reference, python_host, ollama, wire):
    ollama.route("GET", "/api/tags", body={"models": []})
    server = python_host()
    assert raw_exchange(server.httpd.server_port, wire) == raw_exchange(reference, wire)


@pytest.mark.parametrize("path", ["/api/a/%2e%2e/tags?x=%2f", "/api/a/../tags", "/api/%2E/tags", "/api/a/%2e./tags"])
def test_whatwg_paths(reference, python_host, ollama, path):
    ollama.route("POST", "/api/tags", lambda rec: (200, {}, {"path": rec["path"], "query": rec["query"]}))
    server = python_host()
    assert request(server.httpd.server_port, "POST", path, "admin", {}) == request(reference, "POST", path, "admin", {})


def test_live_native_collectors_against_systeminformation(reference_env):
    from ipx.hardware_native import cpu_snapshot, inspect_hardware
    cpu_snapshot.cache_clear()
    expected = run_reference("native", reference_env)
    actual = inspect_hardware()
    assert actual["cpu"] == expected["cpu"]
    assert actual["flags"] == expected["flags"]
    assert actual["mem"]["total"] == expected["mem"]["total"]
    # Do not compare memory sampled concurrently with system_profiler to a
    # later idle sample: the profiler itself can consume hundreds of MiB.
    # Bracket an isolated SI memory query with native samples. This retains
    # actual observed drift bounds rather than increasing a blanket tolerance.
    from ipx.hardware_native import memory_snapshot
    before = memory_snapshot()
    reference_memory = run_reference("native-memory", reference_env)
    after = memory_snapshot()
    assert before["total"] == reference_memory["total"] == after["total"]
    jitter = 128 * 1024**2  # Node startup + command sampling between the probes
    low, high = sorted((before["available"], after["available"]))
    assert low - jitter <= reference_memory["available"] <= high + jitter, (before, reference_memory, after)
    if os.sys.platform in ("win32", "darwin"):
        assert {row["model"] for row in expected["controllers"]} <= {row["model"] for row in actual["controllers"]}
    for row in actual["controllers"]:
        assert row.get("vram") is None or row["vram"] >= 0
        assert row.get("memoryFree") is None or 0 <= row["memoryFree"] <= row["vram"]
    assert actual["gpuDetectionFailed"] is False
    assert actual["backend"] in ("cpu", "cuda", "rocm", "metal", "unknown")


@pytest.mark.parametrize("reference_env", [{"PARITY_HTTP_SETTINGS": json.dumps({"headersTimeout": 100, "requestTimeout": 250, "connectionsCheckingInterval": 10})}], indirect=True)
@pytest.mark.parametrize("wire", [b"GET /__health HTTP/1.1\r\nHost:", b"POST /sysinfo HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer key\r\nContent-Length: 100\r\n\r\n{"])
def test_node_request_and_header_deadlines(reference, reference_env, python_host, wire):
    from ipx.server_http import HTTPSettings
    settings = HTTPSettings(headers_timeout=.1, request_timeout=.25, connections_checking_interval=.01)
    server = python_host(http_settings=settings)
    expected = raw_exchange(reference, wire)
    assert expected[0] == b"HTTP/1.1 408 Request Timeout"
    assert raw_exchange(server.httpd.server_port, wire) == expected


@pytest.mark.parametrize("reference_env", [{"PARITY_HTTP_SETTINGS": json.dumps({"headersTimeout": 100, "requestTimeout": 250, "connectionsCheckingInterval": 10})}], indirect=True)
def test_completed_request_long_inference_has_no_deadline(reference, reference_env, python_host, ollama):
    import time

    from ipx.server_http import HTTPSettings
    def infer(rec):
        time.sleep(.4)
        return 200, {}, {"done": True}
    ollama.route("POST", "/api/chat", infer)
    server = python_host(http_settings=HTTPSettings(headers_timeout=.1, request_timeout=.25, connections_checking_interval=.01))
    assert request(server.httpd.server_port, "POST", "/api/chat", "key", {}) == request(reference, "POST", "/api/chat", "key", {})


@pytest.mark.parametrize("size", [16350, 16360, 16370, 16380, 16384])
def test_header_overflow_boundary(reference, python_host, size):
    wire = b"GET /__health HTTP/1.1\r\nHost: x\r\nConnection: close\r\nX: " + b"x" * size + b"\r\n\r\n"
    server = python_host()
    assert raw_exchange(server.httpd.server_port, wire) == raw_exchange(reference, wire)


def test_oversized_header_before_line_termination(reference, python_host):
    wire = b"GET / HTTP/1.1\r\nHost: x\r\nX-Large: " + b"a" * 18000
    server = python_host()
    expected = raw_exchange(reference, wire)
    assert expected[0] == b"HTTP/1.1 431 Request Header Fields Too Large"
    assert raw_exchange(server.httpd.server_port, wire) == expected


@pytest.mark.parametrize("extra", [995, 996, 997, 998, 2000])
def test_header_count_limit(reference, python_host, ollama, extra):
    ollama.route("GET", "/api/tags", lambda rec: (200, {}, {"x_end": rec["headers"].get("x-end"), "x": rec["headers"].get("x")}))
    wire = b"GET /api/tags HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer key\r\nConnection: close\r\n" + b"X:\r\n" * extra + b"X-End: excluded\r\n\r\n"
    server = python_host()
    assert raw_exchange(server.httpd.server_port, wire) == raw_exchange(reference, wire)


@pytest.mark.parametrize("reference_env", [{"PARITY_HTTP_SETTINGS": json.dumps({"keepAliveTimeout": 50, "keepAliveTimeoutBuffer": 50})}], indirect=True)
def test_keepalive_idle_buffer(reference, reference_env, python_host):
    from ipx.server_http import HTTPSettings
    server = python_host(http_settings=HTTPSettings(keep_alive_timeout=.05, keep_alive_timeout_buffer=.05))
    for port in (reference, server.httpd.server_port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/__health")
        response = conn.getresponse()
        assert response.status == 204 and response.getheader("keep-alive") == "timeout=0"
        response.read()
        assert conn.sock.recv(1) == b""
        conn.close()


def test_excess_header_name_rejected_before_colon(reference, python_host):
    # Host + Authorization + Connection + 997 X fields exhaust 1000 slots.
    wire = b"GET /api/tags HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer key\r\nConnection: close\r\n" + b"X:\r\n" * 997 + b"Q"
    server = python_host()
    expected = raw_exchange(reference, wire)
    assert expected[0] == b"HTTP/1.1 431 Request Header Fields Too Large"
    assert raw_exchange(server.httpd.server_port, wire) == expected
