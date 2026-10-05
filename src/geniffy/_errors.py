"""Errors the Geniffy client raises. Every one carries the sentence the API sent, written for people."""
from __future__ import annotations

from typing import Any, Dict, Optional


class GeniffyError(Exception):
    """Anything that went wrong talking to Geniffy.

    `request_id` is the id Geniffy gave the call (its X-Request-ID): log it, and paste it into Requests in
    the Geniffy app to see exactly what was sent and what came back. None when the call never arrived."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None,
                 body: Optional[Dict[str, Any]] = None, request_id: Optional[str] = None):
        super().__init__(message)
        self.message, self.status, self.code, self.body = message, status, code, body or {}
        self.request_id = request_id

    def __str__(self) -> str:
        return f"{self.message} (request {self.request_id})" if self.request_id else self.message


class APIConnectionError(GeniffyError):
    """Geniffy could not be reached (network, DNS, timeout), after the retries."""


class AuthenticationError(GeniffyError):
    """The key is missing, wrong or revoked (401), or not allowed here (403)."""


class NotFoundError(GeniffyError):
    """No such memory or source in this memory (404)."""


class BadRequestError(GeniffyError):
    """The request was refused as sent (400, 413, 422)."""


class UnreadableError(BadRequestError):
    """A file or link was saved but could not be read; `source` is the row it left, with the reason."""

    @property
    def source(self) -> Optional[Dict[str, Any]]:
        return (self.body.get("error") or {}).get("source")


class RateLimitError(GeniffyError):
    """Too many requests at once (429); retried automatically before this is raised."""


class InternalServerError(GeniffyError):
    """Geniffy or the memory behind it did not answer (5xx); retried automatically before this is raised."""


def from_response(status: int, body: Dict[str, Any], request_id: Optional[str] = None) -> GeniffyError:
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code")
    message = err.get("message") or body.get("detail") or f"Geniffy answered {status}."
    if status in (401, 403):
        cls = AuthenticationError
    elif status == 404:
        cls = NotFoundError
    elif status == 422 and code == "unreadable":
        cls = UnreadableError
    elif status in (400, 409, 413, 422):
        cls = BadRequestError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = InternalServerError
    else:
        cls = GeniffyError
    return cls(str(message), status=status, code=code, body=body, request_id=request_id)
