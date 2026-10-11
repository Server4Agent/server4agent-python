from __future__ import annotations

from typing import Any, Dict, Optional


class Server4AgentError(Exception):
    """Base class for every error raised by this SDK."""


class APIConnectionError(Server4AgentError):
    """The request never reached the API (DNS, TCP, TLS, or a dropped connection)."""

    def __init__(self, message: str = "Could not reach the Server4Agent API.") -> None:
        super().__init__(message)
        self.message = message


class APITimeoutError(APIConnectionError):
    """The request timed out before the API responded."""

    def __init__(self, message: str = "Request to the Server4Agent API timed out.") -> None:
        super().__init__(message)


class APIStatusError(Server4AgentError):
    """The API returned a 4xx/5xx response.

    Prefer catching a specific subclass (``NotFoundError``, ``RateLimitError``,
    ...). ``code`` is the machine-readable string from the response body and
    ``request_id`` (when present) is worth quoting to support. ``body`` is the
    whole JSON error response, for the details some errors carry beyond the
    message (a failed app start's ``log_tail``, a refused write's ``reason``).
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.body: Dict[str, Any] = body if body is not None else {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class BadRequestError(APIStatusError):
    """400 — the request was malformed."""


class AuthenticationError(APIStatusError):
    """401 — the API key is missing, invalid, or revoked."""


class PermissionDeniedError(APIStatusError):
    """403 — the key is valid but not scoped to this resource."""


class NotFoundError(APIStatusError):
    """404 — no such resource."""


class ConflictError(APIStatusError):
    """409 — the request conflicts with the current state (e.g. a cap was hit)."""


class UnprocessableEntityError(APIStatusError):
    """422 — the request was well-formed but semantically invalid."""


class RateLimitError(APIStatusError):
    """429 — too many requests. The SDK retries these automatically; you only see it once retries are exhausted."""


class InternalServerError(APIStatusError):
    """5xx — something went wrong on the API side."""


_STATUS_TO_ERROR = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def error_from_status(
    status: int,
    code: str,
    message: str,
    request_id: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
) -> APIStatusError:
    """Map an HTTP status onto the most specific ``APIStatusError`` subclass."""
    cls = _STATUS_TO_ERROR.get(status)
    if cls is None:
        cls = InternalServerError if status >= 500 else APIStatusError
    return cls(status, code, message, request_id, body)
