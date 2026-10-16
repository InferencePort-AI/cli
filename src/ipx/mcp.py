"""A minimal MCP client (Streamable HTTP, JSON-RPC 2.0) - just enough to call tools on InferencePort's MCP servers.

Replies are either plain JSON or a Server-Sent-Events stream carrying the JSON-RPC message; both are handled.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ipx import __version__
from ipx.app import App
from ipx.errors import ApiError, IpxError
from ipx.http import ORG_HEADER, raw_request

PROTOCOL_VERSION = "2025-03-26"
REGIONAL_PROD_BASE = "https://us-east-2.api.inferenceport.ai"
PUBLIC_API_BASE = "https://api.inferenceport.ai"


def council_url(base_url: str) -> str:
    """The council MCP endpoint (trailing slash matters: the server mounts it at /mcp/council/)."""
    base = base_url.rstrip("/")
    return f"{PUBLIC_API_BASE if base == REGIONAL_PROD_BASE else base}/mcp/council/"


def parse_rpc(body: str, want_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Pull the JSON-RPC response out of a JSON or SSE body."""
    text = (body or "").strip()
    if not text:
        return None
    candidates = [text] if text.startswith("{") else [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
    found: Optional[Dict[str, Any]] = None
    for c in candidates:
        try:
            msg = json.loads(c)
        except ValueError:
            continue
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            if want_id is None or msg.get("id") == want_id:
                return msg
            found = found or msg
    return found


class McpError(IpxError):
    pass


class McpClient:
    def __init__(self, app: App, url: str, *, timeout: float = 600.0) -> None:
        app.require_login()
        self.app = app
        self.url = url
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._next_id = 0
        self.server_info: Dict[str, Any] = {}

    def _headers(self) -> Dict[str, str]:
        t = self.app.api
        h = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {t.creds.access_token}",
            "X-Client-ID": t.cfg.client_id,
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        # Org API keys already belong to their org; only a signed-in session needs the header.
        if t.org and t.creds.auth_type != "api_key":
            h[ORG_HEADER] = t.org
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _post(self, payload: Dict[str, Any]):
        t = self.app.api
        t.require_auth()
        t._maybe_refresh()
        for attempt in (0, 1):
            try:
                return raw_request("POST", self.url, json=payload, headers=self._headers(), timeout=self.timeout)
            except ApiError as e:
                # Same recovery as Transport.request: a rejected token gets one refresh and retry.
                if e.status == 401 and attempt == 0 and t.refresher and t.creds.data.get("refresh_token") and t.refresher(t.cfg, t.creds):
                    continue
                if e.status == 401:
                    e.hint = "Your session expired or the token was rejected. Run `ipx auth login`."
                raise
        raise IpxError("Request failed after refreshing the session.")  # pragma: no cover

    def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        msg = parse_rpc(resp.text, rid)
        if msg is None:
            raise McpError(f"The MCP server sent an unreadable reply to {method}.")
        if msg.get("error"):
            err = msg["error"]
            raise McpError(str(err.get("message") or err) if isinstance(err, dict) else str(err))
        return msg.get("result")

    def connect(self) -> "McpClient":
        res = self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": {"name": "ipx", "version": __version__}}) or {}
        self.server_info = res.get("serverInfo") or {}
        if not self.server_info:
            raise McpError("The server replied, but not with a valid MCP handshake.")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except IpxError:
            pass  # a notification has no reply that matters
        return self

    def list_tools(self) -> List[Dict[str, Any]]:
        return (self._call("tools/list") or {}).get("tools") or []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Run a tool and return its text output. Tool-level failures raise McpError with the server's message."""
        res = self._call("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        text = "\n".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
        if res.get("isError"):
            raise McpError(text or f"The {name} tool failed.")
        return text

    def call_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        text = self.call_tool(name, arguments)
        try:
            return json.loads(text)
        except ValueError:
            return text
