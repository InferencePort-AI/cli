"""Node-compatible HTTP/1 transport backed by llhttp (httptools).

This module owns framing, header normalization and socket deadlines. Application
routing never interprets unvalidated Content-Length/Transfer-Encoding values.
"""
from __future__ import annotations

import email.utils
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus

import httptools


@dataclass(frozen=True)
class HTTPSettings:
    max_header_size: int = 16384
    max_headers_count: int = 1000
    headers_timeout: float = 60
    request_timeout: float = 300
    connections_checking_interval: float = 30
    keep_alive_timeout: float = 5
    keep_alive_timeout_buffer: float = 1


class ProtocolError(Exception):
    def __init__(self, status=400):
        self.status = status


class BodyTooLarge(Exception):
    pass


class Headers:
    """IncomingMessage.headers duplicate-field rules, including first auth."""
    SINGLE = frozenset("age authorization content-length content-type etag expires from host if-modified-since if-unmodified-since last-modified location max-forwards proxy-authorization referer retry-after server user-agent".split())

    def __init__(self, pairs, maximum=1000):
        self.data = {}
        for key, value in pairs[:maximum] if maximum > 0 else pairs:
            key = key.lower()
            if key not in self.data:
                self.data[key] = value
            elif key not in self.SINGLE:
                self.data[key] += ("; " if key == "cookie" else ", ") + value

    def get(self, key, default=None):
        return self.data.get(key.lower(), default)

    def items(self):
        return self.data.items()


class HTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, owner, settings=None):
        self.owner, self.settings = owner, settings or HTTPSettings()
        self.live, self.live_lock, self.stopping = set(), threading.Lock(), threading.Event()
        if ":" in address[0]: self.address_family = socket.AF_INET6
        # TCPServer deliberately avoids HTTPServer.server_bind/getfqdn, whose
        # reverse-DNS lookup can block detached startup on macOS.
        super().__init__(address, Request)
        self.server_port = self.server_address[1]
        self.watchdog = threading.Thread(target=self._check_connections, daemon=True)
        self.watchdog.start()

    def _check_connections(self):
        while not self.stopping.wait(self.settings.connections_checking_interval):
            now = time.monotonic()
            with self.live_lock: live = list(self.live)
            for handler in live:
                if handler.expires and now >= handler.expires:
                    handler.abort(408)

    def server_close(self):
        self.stopping.set()
        super().server_close()


class Request(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.owner, self.settings = self.server.owner, self.server.settings
        self.expires, self.request_started, self.idle = None, None, False
        self.write_lock = threading.Lock()
        self.aborted = False
        with self.server.live_lock: self.server.live.add(self)
        with self.owner.connections_lock: self.owner.connections.add(self.connection)

    def finish(self):
        try:
            super().finish()
        except OSError:
            pass
        finally:
            with self.server.live_lock: self.server.live.discard(self)
            with self.owner.connections_lock: self.owner.connections.discard(self.connection)

    def abort(self, status=None):
        with self.write_lock:
            if self.aborted: return
            self.aborted, self.close_connection = True, True
            if status:
                try:
                    self.connection.sendall(f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nConnection: close\r\n\r\n".encode())
                except OSError:
                    pass
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def on_url(self, value):
        self.url.extend(value)
        self.header_size += len(value)
        if self.header_size >= self.settings.max_header_size: raise ProtocolError(431)

    def on_header(self, key, value):
        if self.headers_complete:
            self.trailer_size += len(key) + len(value)
            self.trailer_count += 1
            if self.trailer_size >= self.settings.max_header_size or self.settings.max_headers_count > 0 and self.trailer_count > self.settings.max_headers_count:
                raise ProtocolError(431)
            return  # trailers are separate, never request headers
        if self.settings.max_headers_count > 0 and len(self.pairs) >= self.settings.max_headers_count:
            raise ProtocolError(431)
        self.header_size += len(key) + len(value)
        if self.header_size >= self.settings.max_header_size: raise ProtocolError(431)
        self.pairs.append((key.decode("latin-1"), value.decode("latin-1")))

    def on_headers_complete(self):
        self.headers_complete = True
        self.trailer_size, self.trailer_count = 0, 0
        self.command = self.parser.get_method().decode("ascii")
        self.request_version = "HTTP/" + self.parser.get_http_version()
        self.path = self.url.decode("latin-1")
        self.headers = Headers(self.pairs, self.settings.max_headers_count)
        self.close_connection = not self.parser.should_keep_alive()
        self.expires = self.request_started + self.settings.request_timeout if self.settings.request_timeout else None

    def on_body(self, body):
        self.body_size += len(body)
        if self.enforce_limit and self.body_size > self.owner.max_body:
            self.owner.log.line("WARN", "Request body too large")
            raise BodyTooLarge()
        if self.capture_body:
            self.body.append(body)

    def on_message_complete(self):
        self.complete = True
        self.expires = None

    def feed(self, data):
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserUpgrade:
            # Node without a CONNECT/upgrade listener does not expose a tunnel.
            if self.command == "CONNECT":
                self.abort()
            else:
                self.complete = True
        except httptools.HttpParserError as err:
            if isinstance(err.__context__, (ProtocolError, BodyTooLarge)):
                raise err.__context__
            # llhttp reports unsupported transfer coding immediately AFTER
            # on_headers_complete. Node dispatches synchronous routes first;
            # defer this error until a body consumer (or drain) observes it.
            if self.headers_complete and not self.response_started:
                self.parser_failure = ProtocolError()
                return
            raise ProtocolError() from err

    def read_body(self, capture=True):
        self.capture_body = self.enforce_limit = capture
        if self.parser_failure: raise self.parser_failure
        if not self.complete:
            if self.headers.get("transfer-encoding"):
                # Feed raw framing to llhttp, never trust ad-hoc chunk parsing.
                # readline stops at chunk metadata; data is bounded by its size.
                while not self.complete and not self.aborted:
                    line = self.rfile.readline(65537)
                    if not line: raise ConnectionResetError("Incomplete chunked body")
                    self.feed(line)
                    if self.complete: break
                    try: count = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError: raise ProtocolError() from None
                    if count:
                        remaining = count
                        while remaining:
                            data = self.rfile.read1(min(65536, remaining))
                            if not data: raise ConnectionResetError("Incomplete request body")
                            self.feed(data)
                            remaining -= len(data)
                        self.feed(self.rfile.read(2))
                    else:
                        while not self.complete:
                            trailer = self.rfile.readline(65537)
                            if not trailer: raise ConnectionResetError("Incomplete trailers")
                            self.feed(trailer)
            else:
                remaining = int(self.headers.get("content-length", "0")) - self.body_size
                while remaining and not self.aborted:
                    data = self.rfile.read1(min(65536, remaining))
                    if not data: raise ConnectionResetError("Incomplete request body")
                    self.feed(data)
                    remaining -= len(data)
        return b"".join(self.body)

    def handle(self):
        self.close_connection = False
        while not self.close_connection:
            self.aborted = False
            self.url, self.pairs, self.body = bytearray(), [], []
            self.header_size, self.body_size = 0, 0
            self.capture_body = self.enforce_limit = False
            self.headers_complete, self.complete = False, False
            self.parser = httptools.HttpRequestParser(self)
            self.command, self.path, self.request_version = "", "", "HTTP/1.1"
            self.response_started = False
            self.parser_failure = None
            self.request_started = time.monotonic()
            timeout = min(t for t in (self.settings.headers_timeout, self.settings.request_timeout) if t) if self.settings.headers_timeout or self.settings.request_timeout else None
            self.expires = self.request_started + timeout if timeout else None
            try:
                if self.idle:
                    self.connection.settimeout(self.settings.keep_alive_timeout + self.settings.keep_alive_timeout_buffer)
                else:
                    self.connection.settimeout(None)
                pending_line = bytearray()
                first_line = True
                while not self.headers_complete:
                    available = self.rfile.peek(1)
                    if not available: return
                    newline = available.find(b"\n")
                    size = min(1024, newline + 1 if newline >= 0 else len(available))
                    line = self.rfile.read(size)
                    pending_line.extend(line)
                    if not line: return
                    if self.idle:
                        self.idle = False
                        self.connection.settimeout(None)
                        self.request_started = time.monotonic()
                        self.expires = self.request_started + timeout if timeout else None
                    self.feed(line)
                    if not self.headers_complete and not first_line:
                        if pending_line.strip() and self.settings.max_headers_count > 0 and len(self.pairs) >= self.settings.max_headers_count:
                            raise ProtocolError(431)
                        key, sep, value = bytes(pending_line).partition(b":")
                        size = len(key) + len(value.strip(b" \t\r\n")) if sep else len(pending_line)
                        if self.header_size + size >= self.settings.max_header_size:
                            raise ProtocolError(431)
                    if line.endswith(b"\n"):
                        first_line = False
                        pending_line.clear()
                if self.aborted: return
                # Node requireHostHeader (HTTP/1.1 only), before application route.
                if self.request_version == "HTTP/1.1" and self.headers.get("host") is None:
                    self.send_response_only(400)
                    self.send_header("Connection", "close")
                    self.send_header("Date", self.date_time_string())
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                expect = self.headers.get("expect")
                if expect and expect.lower() != "100-continue":
                    self.send_response_only(417)
                    self.send_header("Connection", "close")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Date", self.date_time_string())
                    self.end_headers()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                if expect:
                    self.connection.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                self.owner._handle(self)
                if self.aborted or self.close_connection: return
                # Node consumes unread bodies after an early route response.
                if self.parser_failure:
                    self.abort()
                    return
                self.read_body(capture=False)
                self.body.clear()
                self.idle, self.expires = True, None
            except BodyTooLarge:
                self.abort()
                return
            except ProtocolError as err:
                self.abort(err.status)
                return
            except socket.timeout:
                return
            except (ConnectionError, OSError):
                return

    def send_response_only(self, status):
        self.response_started = True
        self.response_status = status
        self.response_headers = []
        phrase = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "unknown"
        self.wfile.write(f"HTTP/1.1 {status} {phrase}\r\n".encode("latin-1"))

    def send_header(self, key, value):
        self.response_headers.append((key, str(value)))
        self.wfile.write(f"{key}: {value}\r\n".encode("latin-1"))

    def end_headers(self):
        self.wfile.write(b"\r\n")

    @staticmethod
    def date_time_string():
        return email.utils.formatdate(usegmt=True)
