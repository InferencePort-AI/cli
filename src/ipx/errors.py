"""Exception types. Anything derived from IpxError is shown as a friendly message, never a traceback."""

from __future__ import annotations

from typing import Optional


class IpxError(Exception):
    """A user-facing error with an optional hint on how to fix it."""

    def __init__(self, message: str, hint: Optional[str] = None, code: int = 1):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


class ApiError(IpxError):
    """The server answered with a non-2xx status."""

    def __init__(self, status: int, message: str, hint: Optional[str] = None, body=None):
        super().__init__(message, hint)
        self.status = status
        self.body = body


class NotSignedIn(IpxError):
    def __init__(self, message: str = "You are not signed in."):
        super().__init__(message, "Run `ipx auth login` to sign in.")


class NeedsOrg(IpxError):
    def __init__(self) -> None:
        super().__init__(
            "This command needs an organization context.",
            "Pick one with `ipx auth context <org>` (or pass --org <slug>). List yours with `ipx org list`.",
        )


class LocalUnavailable(IpxError):
    """The local Ollama runtime is not reachable."""
