"""Transport hardening: no credential forwarding on cross-host redirects; mid-stream failures are clean errors."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ipx.errors import IpxError
from ipx.http import raw_request


def _serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_authorization_not_forwarded_to_other_host():
    seen = {}

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["cid"] = self.headers.get("X-Client-ID")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    target = _serve(Target)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            # "localhost" vs "127.0.0.1" is a different host
            self.send_header("Location", f"http://localhost:{target.server_port}/x")
            self.end_headers()

    origin = _serve(Origin)
    try:
        r = raw_request("GET", f"http://127.0.0.1:{origin.server_port}/", headers={
            "Authorization": "Bearer secret", "X-Client-ID": "abc"})
        assert r.status == 200
        assert seen["auth"] is None and seen["cid"] is None
    finally:
        origin.shutdown()
        target.shutdown()


def test_connection_reset_mid_stream_is_ipx_error():
    from ipx.http import StreamResponse

    class Raw:
        calls = 0

        def read1(self, n):
            Raw.calls += 1
            if Raw.calls == 1:
                return b"data: {}\n\n"
            raise ConnectionResetError("reset by peer")

        def close(self):
            pass

    s = StreamResponse(Raw(), 200, {})
    it = s.iter_sse()
    assert next(it) == ("message", "{}")
    with pytest.raises(IpxError):
        next(it)
