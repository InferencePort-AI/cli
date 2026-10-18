"""Dependency-free HTTP transport (urllib) with auth, org header, token refresh, SSE and multipart support."""

from __future__ import annotations

import http.client
import json as _json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Tuple, Union

from ipx import __version__
from ipx.config import CONSOLE_URL, Config, Credentials
from ipx.errors import ApiError, IpxError, NotSignedIn

ORG_HEADER = "X-InferencePort-Org"
USER_AGENT = f"ipx-cli/{__version__}"

_SSL: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    global _SSL
    if _SSL is None:
        try:
            import certifi

            _SSL = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # pragma: no cover - certifi missing or unreadable
            _SSL = ssl.create_default_context()
    return _SSL


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never forward credentials to a different host (urllib copies all headers by default)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url)
            new_host = urllib.parse.urlparse(newurl)
            if (old_host.scheme, old_host.netloc.lower()) != (new_host.scheme, new_host.netloc.lower()):
                for name in list(new.headers):
                    if name.lower() in ("authorization", "x-client-id", "x-inferenceport-org", "cookie"):
                        del new.headers[name]
                for name in list(new.unredirected_hdrs):
                    if name.lower() in ("authorization", "x-client-id", "x-inferenceport-org", "cookie"):
                        del new.unredirected_hdrs[name]
        return new


def _opener(ctx: Optional[ssl.SSLContext]) -> urllib.request.OpenerDirector:
    handlers: List[Any] = [_StripAuthOnCrossHostRedirect()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return _json.loads(self.body)
        except ValueError:
            raise IpxError("The server returned a response that is not JSON.", self.text[:300])

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()


class StreamResponse:
    """An open response whose body is consumed incrementally."""

    def __init__(self, raw: Any, status: int, headers: Dict[str, str]):
        self._raw = raw
        self.status = status
        self.headers = headers

    def _read(self, size: int) -> bytes:
        """Read a chunk, turning network failures mid-stream into a clean IpxError instead of a traceback."""
        try:
            if hasattr(self._raw, "read1"):
                return self._raw.read1(size)
            return self._raw.read(size)
        except (OSError, http.client.HTTPException) as e:
            raise IpxError(f"The connection was lost while receiving the response: {e}",
                           "Check your connection and try again.")

    def iter_lines(self) -> Iterator[str]:
        buf = b""
        while True:
            chunk = self._read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", "replace")
        if buf:
            yield buf.decode("utf-8", "replace")

    def iter_sse(self) -> Iterator[Tuple[str, str]]:
        """Yield (event, data) pairs from a text/event-stream body."""
        event, data = "message", []
        for line in self.iter_lines():
            if line == "":
                if data:
                    yield event, "\n".join(data)
                event, data = "message", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            yield event, "\n".join(data)

    def iter_bytes(self, size: int = 65536) -> Iterator[bytes]:
        while True:
            try:
                chunk = self._raw.read(size)
            except (OSError, http.client.HTTPException) as e:
                raise IpxError(f"The connection was lost while receiving the response: {e}",
                               "Check your connection and try again.")
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "StreamResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


FileSpec = Tuple[str, Union[bytes, BinaryIO], str]  # (filename, content, content_type)


def encode_multipart(fields: Dict[str, Any], files: Dict[str, FileSpec]) -> Tuple[bytes, str]:
    boundary = "----ipx" + uuid.uuid4().hex
    out: List[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, (filename, content, ctype) in files.items():
        data = content if isinstance(content, (bytes, bytearray)) else content.read()
        out.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode()
        )
        out.append(bytes(data))
        out.append(b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def guess_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def extract_detail(body: bytes) -> Tuple[str, Any]:
    """Pull a human message out of an error body. Returns (message, parsed_json_or_None)."""
    text = body.decode("utf-8", "replace").strip()
    try:
        parsed = _json.loads(text)
    except ValueError:
        return text[:500] or "", None
    if isinstance(parsed, dict):
        for key in ("detail", "error", "message", "msg", "error_description"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return val, parsed
            if isinstance(val, dict):
                inner = val.get("message") or val.get("error") or val.get("detail")
                if isinstance(inner, str):
                    return inner, parsed
            if isinstance(val, list) and val:
                return "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in val), parsed
    return text[:500], parsed


def api_error(status: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> ApiError:
    headers = headers or {}
    msg, parsed = extract_detail(body)
    low = msg.lower()
    hint: Optional[str] = None
    if status == 401:
        msg = msg or "Authentication failed."
        hint = "Your session may have expired. Run `ipx auth login`."
    elif status == 402:
        msg = msg or "Insufficient credits."
        hint = f"Check your balance with `ipx p2g balance` and top up at {CONSOLE_URL}."
    elif status == 403:
        if "signed-in session" in low or "not an api key" in low or "requires a signed-in" in low:
            hint = "This route needs a signed-in session. Use `ipx auth login` with email or OAuth instead of an API key."
        elif "organization" in low or "policy" in low:
            hint = "Your organization's policy blocks this. Ask an org admin, or use `ipx auth context --clear`."
        msg = msg or "Forbidden."
    elif status == 404:
        msg = msg or "Not found."
    elif status == 429:
        retry = headers.get("retry-after")
        msg = msg or "Rate limit or quota reached."
        hint = f"Try again in {retry}s." if retry else "Check your quota with `ipx usage get`."
    elif status >= 500:
        msg = msg or f"The service had an internal error ({status})."
        hint = "Check `ipx status`; retry in a moment."
    return ApiError(status, msg or f"Request failed with HTTP {status}.", hint, body=parsed)


def raw_request(
    method: str,
    url: str,
    *,
    json: Any = None,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
    stream: bool = False,
) -> Union[Response, StreamResponse]:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    body = data
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    ctx = ssl_context() if url.lower().startswith("https") else None
    try:
        resp = _opener(ctx).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        payload = e.read() if e.fp else b""
        raise api_error(e.code, payload, {k.lower(): v for k, v in e.headers.items()})
    except urllib.error.URLError as e:
        raise IpxError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e.reason}",
                       "Check your connection, or set a different endpoint with `ipx config set base_url <url>`.")
    except (TimeoutError, OSError) as e:
        raise IpxError(f"Request to {urllib.parse.urlparse(url).netloc} failed: {e}")
    h = {k.lower(): v for k, v in resp.headers.items()}
    if stream:
        return StreamResponse(resp, resp.status, h)
    try:
        with resp:
            return Response(resp.status, h, resp.read())
    except (OSError, http.client.HTTPException) as e:
        raise IpxError(f"Request to {urllib.parse.urlparse(url).netloc} failed while reading the response: {e}")


class Transport:
    """Authenticated client for the InferencePort API.

    Adds the bearer token, X-Client-ID and (unless `personal`) the X-InferencePort-Org header; refreshes an
    expiring Supabase session once before a request and once more after a 401.
    """

    def __init__(
        self,
        cfg: Config,
        creds: Credentials,
        *,
        org: Optional[str] = None,
        personal: bool = False,
        base_url: Optional[str] = None,
        refresher: Optional[Callable[[Config, Credentials], bool]] = None,
    ) -> None:
        self.cfg = cfg
        self.creds = creds
        self.org = None if personal else (org or cfg.org)
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.refresher = refresher

    # -- helpers -----------------------------------------------------------
    def url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        u = path if path.startswith("http") else self.base_url + (path if path.startswith("/") else "/" + path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                u += ("&" if "?" in u else "?") + urllib.parse.urlencode(clean, doseq=True)
        return u

    def _headers(self, auth: bool, org: bool, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        h = {"X-Client-ID": self.cfg.client_id}
        if auth:
            token = self.creds.access_token
            if token:
                h["Authorization"] = f"Bearer {token}"
        if org and self.org:
            h[ORG_HEADER] = self.org
        if extra:
            h.update(extra)
        return h

    def _maybe_refresh(self) -> None:
        if self.refresher and self.creds.expiring():
            self.refresher(self.cfg, self.creds)

    def require_auth(self) -> None:
        if not self.creds.signed_in:
            raise NotSignedIn()

    # -- API -----------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, FileSpec]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = True,
        org: bool = True,
        timeout: float = 120.0,
        stream: bool = False,
    ) -> Union[Response, StreamResponse]:
        if auth:
            self.require_auth()
            self._maybe_refresh()
        data: Optional[bytes] = None
        hdrs = dict(headers or {})
        if files is not None or form is not None:
            data, ctype = encode_multipart(form or {}, files or {})
            hdrs["Content-Type"] = ctype
        if stream:
            hdrs.setdefault("Accept", "text/event-stream")
        url = self.url(path, params)
        for attempt in (0, 1):
            try:
                return raw_request(method, url, json=json, data=data, headers=self._headers(auth, org, hdrs),
                                   timeout=timeout, stream=stream)
            except ApiError as e:
                if e.status == 401 and attempt == 0 and auth and self.refresher and self.creds.data.get("refresh_token"):
                    if self.refresher(self.cfg, self.creds):
                        continue
                if e.status == 401 and auth:
                    e.hint = "Your session expired or the token was rejected. Run `ipx auth login`."
                raise
        raise IpxError("Request failed after refreshing the session.")  # pragma: no cover

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=body if body is not None else {}, **kw).json()

    def put(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, json=body if body is not None else {}, **kw).json()

    def patch(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", path, json=body if body is not None else {}, **kw).json()

    def delete(self, path: str, **kw: Any) -> Any:
        r = self.request("DELETE", path, **kw)
        return r.json() if r.body else {}

    def download(self, url: str, dest: str, *, label: str = "Downloading", auth: bool = True) -> int:
        """Stream a (possibly absolute) URL to disk and return bytes written."""
        from ipx import ui

        s = self.request("GET", url, stream=True, auth=auth, headers={"Accept": "*/*"}, timeout=600)
        total = int(s.headers.get("content-length") or 0) or None
        written = 0
        with ui.download_progress() as prog:
            task = prog.add_task(label, total=total)
            with open(dest, "wb") as fh:
                for chunk in s.iter_bytes():
                    fh.write(chunk)
                    written += len(chunk)
                    prog.update(task, advance=len(chunk))
        s.close()
        return written


def poll(fn: Callable[[], Any], *, done: Callable[[Any], bool], interval: float = 4.0, timeout: float = 900.0,
         on_tick: Optional[Callable[[Any], None]] = None) -> Any:
    """Call fn() until done(result) is true. Raises IpxError on timeout."""
    start = time.time()
    while True:
        result = fn()
        if on_tick:
            on_tick(result)
        if done(result):
            return result
        if time.time() - start > timeout:
            raise IpxError("Timed out waiting for the job to finish.", "Re-check later with the job id shown above.")
        time.sleep(interval)


def ensure_dir_for(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
