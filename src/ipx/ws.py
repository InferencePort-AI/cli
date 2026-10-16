"""Minimal dependency-free WebSocket client (RFC 6455) for the realtime commands.

Only what `ipx` needs: TLS WebSockets (plain ones are limited to loopback hosts, so a bearer token is never sent in
the clear), request headers, one subprotocol, text and binary frames, fragmentation, ping/pong and the close handshake.
``recv`` accepts a timeout that only applies while waiting for the *start* of a frame, so a timeout can never leave a
half-read frame behind.
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
import time
import urllib.parse
from typing import Dict, Optional, Tuple

from ipx.errors import IpxError
from ipx.http import USER_AGENT, api_error, ssl_context

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME = 64 * 1024 * 1024


class WebSocketClosed(IpxError):
    """The server closed the connection (or it dropped). ``code`` is the close code, 1006 when abnormal."""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(reason or f"The connection was closed (code {code}).")
        self.code = code
        self.reason = reason


def _xor(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class WebSocket:
    def __init__(self, sock: socket.socket, subprotocol: Optional[str] = None, io_timeout: float = 60.0):
        self.sock = sock
        self.subprotocol = subprotocol
        self.io_timeout = io_timeout
        self.close_code: Optional[int] = None
        self.close_reason = ""
        self._buf = b""
        self._closed = False
        sock.settimeout(io_timeout)

    # -- sending -------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        try:
            self.sock.sendall(bytes(header) + mask + _xor(payload, mask))
        except (OSError, ssl.SSLError) as e:
            raise WebSocketClosed(self.close_code or 1006, f"The connection dropped while sending ({e}).")

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    # -- receiving -----------------------------------------------------------
    def _readable(self, timeout: Optional[float]) -> bool:
        if self._buf:
            return True
        if isinstance(self.sock, ssl.SSLSocket) and self.sock.pending():
            return True
        ready, _, _ = select.select([self.sock], [], [], timeout)
        return bool(ready)

    def _fill(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                raise WebSocketClosed(1006, "The connection timed out.")
            except OSError as e:
                raise WebSocketClosed(1006, f"The connection dropped ({e}).")
            if not chunk:
                raise WebSocketClosed(self.close_code or 1006, self.close_reason or "The connection was closed.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> Tuple[bool, int, bytes]:
        b1, b2 = self._fill(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        masked, n = bool(b2 & 0x80), b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._fill(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._fill(8))[0]
        if n > MAX_FRAME:
            raise IpxError("The server sent an oversized WebSocket frame.")
        mask = self._fill(4) if masked else b""
        payload = self._fill(n) if n else b""
        return fin, opcode, (_xor(payload, mask) if masked else payload)

    def recv(self, timeout: Optional[float] = None) -> Optional[Tuple[int, bytes]]:
        """Next data message as ``(opcode, payload)``; ``None`` if nothing arrived within ``timeout`` seconds.

        Raises :class:`WebSocketClosed` when the server closes the connection.
        """
        message = bytearray()
        message_op = OP_TEXT
        in_message = False
        # One absolute deadline for the whole call: control frames (pings) must not restart the wait.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            # The timeout only covers the wait for a frame to start; once a message is in flight we read it through.
            if not in_message:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if not self._readable(remaining):
                    return None
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace") if len(payload) > 2 else ""
                self.close_code, self.close_reason = code, reason
                if not self._closed:
                    self._closed = True
                    try:
                        self._send_frame(OP_CLOSE, payload[:2])
                    except OSError:
                        pass
                raise WebSocketClosed(code, reason)
            if opcode in (OP_TEXT, OP_BINARY):
                message_op = opcode
                message = bytearray(payload)
            elif opcode == OP_CONT:
                message += payload
            if fin:
                return message_op, bytes(message)
            in_message = True

    def recv_text(self, timeout: Optional[float] = None) -> Optional[str]:
        got = self.recv(timeout)
        if got is None:
            return None
        return got[1].decode("utf-8", "replace")

    # -- closing -------------------------------------------------------------
    def close(self, code: int = 1000) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect(url: str, *, headers: Optional[Dict[str, str]] = None, subprotocol: Optional[str] = None,
            timeout: float = 30.0) -> WebSocket:
    """Open a WebSocket. A non-101 answer is raised as the matching :class:`ipx.errors.ApiError`."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise IpxError(f"Not a WebSocket URL: {url}")
    secure = parts.scheme == "wss"
    host = parts.hostname or ""
    if not secure and host not in ("localhost", "127.0.0.1", "::1"):
        raise IpxError(f"Refusing to open an unencrypted WebSocket to {host}.",
                       "Use an https:// base URL; plain ws:// is only allowed for localhost.")
    port = parts.port or (443 if secure else 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        sock: socket.socket = ssl_context().wrap_socket(raw, server_hostname=host) if secure else raw
    except (OSError, ssl.SSLError) as e:
        raise IpxError(f"Could not connect to {host}:{port}.", str(e))

    key = base64.b64encode(os.urandom(16)).decode()
    req = [f"GET {path} HTTP/1.1", f"Host: {parts.netloc}", "Upgrade: websocket", "Connection: Upgrade",
           f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"User-Agent: {USER_AGENT}"]
    if subprotocol:
        req.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    for k, v in (headers or {}).items():
        if "\r" in k + v or "\n" in k + v:
            raise IpxError("Invalid header value.")
        req.append(f"{k}: {v}")
    try:
        sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode("utf-8"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break
    except OSError as e:
        sock.close()
        raise IpxError("The WebSocket handshake failed.", str(e))

    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        sock.close()
        raise IpxError("The server did not answer the WebSocket handshake.")
    resp_headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}

    if status != 101:
        body = rest
        length = int(resp_headers.get("content-length") or 0)
        try:
            while len(body) < min(length, 65536):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
        except OSError:
            pass
        sock.close()
        raise api_error(status, body, resp_headers)

    expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if resp_headers.get("sec-websocket-accept") != expected:
        sock.close()
        raise IpxError("The server's WebSocket handshake was invalid.")

    ws = WebSocket(sock, resp_headers.get("sec-websocket-protocol"))
    ws._buf = rest  # frames that arrived together with the handshake
    return ws
