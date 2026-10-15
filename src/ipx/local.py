"""Client for the local Ollama runtime (the same engine the desktop app drives)."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

from ipx.errors import ApiError, IpxError, LocalUnavailable
from ipx.http import StreamResponse, raw_request

INSTALL_HINT = "Install Ollama from https://ollama.com, then run `ollama serve` (the desktop app starts it for you)."


def ollama_version(url: str) -> Optional[str]:
    try:
        r = raw_request("GET", url.rstrip("/") + "/api/version", timeout=2)
        return (r.json() or {}).get("version")  # type: ignore[union-attr]
    except IpxError:
        return None


class Ollama:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _call(self, method: str, path: str, body: Any = None, *, stream: bool = False, timeout: float = 600.0):
        try:
            return raw_request(method, self.url + path, json=body, timeout=timeout, stream=stream)
        except ApiError as e:
            if e.status == 404 and "not found" in e.message.lower():
                raise IpxError(e.message, "Pull it first with `ipx model pull <name>`.")
            raise IpxError(f"Ollama: {e.message}")
        except IpxError as e:
            if "Could not reach" in e.message or "failed" in e.message.lower():
                raise LocalUnavailable("Local Ollama is not running.", INSTALL_HINT)
            raise

    # -- management ------------------------------------------------------
    def version(self) -> Optional[str]:
        return ollama_version(self.url)

    def list(self) -> List[Dict[str, Any]]:
        return (self._call("GET", "/api/tags", timeout=15).json() or {}).get("models", [])  # type: ignore[union-attr]

    def running(self) -> List[Dict[str, Any]]:
        return (self._call("GET", "/api/ps", timeout=15).json() or {}).get("models", [])  # type: ignore[union-attr]

    def show(self, name: str) -> Dict[str, Any]:
        return self._call("POST", "/api/show", {"model": name}, timeout=30).json() or {}  # type: ignore[union-attr]

    def delete(self, name: str) -> None:
        self._call("DELETE", "/api/delete", {"model": name}, timeout=60)

    def copy(self, src: str, dst: str) -> None:
        self._call("POST", "/api/copy", {"source": src, "destination": dst}, timeout=60)

    def _ndjson(self, method: str, path: str, body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        s = self._call(method, path, body, stream=True)
        assert isinstance(s, StreamResponse)
        try:
            for line in s.iter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("error"):
                    raise IpxError(str(obj["error"]))
                yield obj
        finally:
            s.close()

    def pull(self, name: str) -> Iterator[Dict[str, Any]]:
        return self._ndjson("POST", "/api/pull", {"model": name, "stream": True})

    def create(self, name: str, **body: Any) -> Iterator[Dict[str, Any]]:
        return self._ndjson("POST", "/api/create", dict(body, model=name, stream=True))

    def chat(self, model: str, messages: List[Dict[str, Any]], **options: Any) -> Iterator[Dict[str, Any]]:
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options:
            body["options"] = options
        return self._ndjson("POST", "/api/chat", body)

    def generate(self, model: str, prompt: str, system: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
        if system:
            body["system"] = system
        return self._ndjson("POST", "/api/generate", body)
