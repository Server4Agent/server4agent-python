"""Transport pieces shared by the sync and async clients.

Only the actual send/await differs between them; URL/header prep, the retry
policy, JSON decoding, error parsing, and debug logging all live here so the
two clients stay in lockstep.
"""
from __future__ import annotations

import json
import logging
import os
import random
from typing import Any, Mapping, Optional
from urllib.parse import quote

from ._version import __version__
from .errors import error_from_status

DEFAULT_BASE_URL = "https://api.server4agent.com"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2

USER_AGENT = f"server4agent-python/{__version__}"

# Statuses worth retrying at all.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Methods safe to retry on any retryable status (no risk of duplicate writes).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
# For non-idempotent methods (POST/PATCH) we only retry when the server clearly
# did not process the request, so a retry can never create a duplicate.
NON_IDEMPOTENT_RETRY_STATUS = frozenset({408, 429, 503})

_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 8.0

logger = logging.getLogger("server4agent")
# Opt-in one-line request logging via SERVER4AGENT_LOG=debug.
if os.environ.get("SERVER4AGENT_LOG", "").lower() == "debug" and not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[server4agent] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)


def default_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def quote_segment(value: Any) -> str:
    """Percent-encode a value for safe use as a single URL path segment, so an id
    containing "/", "..", "?", or "#" can't redirect the request elsewhere."""
    return quote(str(value), safe="")


def clean_params(query: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Drop None values so we never send `?path=None`."""
    if not query:
        return None
    out = {k: v for k, v in query.items() if v is not None}
    return out or None


def decode_json(raw: bytes) -> Any:
    """Parse a JSON body, tolerating empty or non-JSON responses (proxy HTML, etc.)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def should_retry_status(method: str, status: int, attempt: int, max_retries: int) -> bool:
    if attempt >= max_retries or status not in RETRYABLE_STATUS:
        return False
    if method.upper() in IDEMPOTENT_METHODS:
        return True
    return status in NON_IDEMPOTENT_RETRY_STATUS


def can_retry_exception(method: str, attempt: int, max_retries: int) -> bool:
    # A network/timeout error may have been processed server-side, so only retry
    # methods where a duplicate is harmless.
    return attempt < max_retries and method.upper() in IDEMPOTENT_METHODS


def retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    """Seconds to wait before the next attempt.

    Honors a numeric ``Retry-After`` header when present; otherwise uses
    exponential backoff with full jitter to avoid thundering herds.
    """
    if retry_after:
        try:
            return min(float(retry_after), _BACKOFF_MAX)
        except ValueError:
            pass  # HTTP-date form — fall through to backoff
    ceiling = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, ceiling)


def request_id_from(headers: Mapping[str, str]) -> Optional[str]:
    return headers.get("x-request-id") or headers.get("X-Request-Id")


def raise_for_status(status: int, body: Any, headers: Mapping[str, str]) -> None:
    """Turn a non-2xx response into the right typed error."""
    parsed = body if isinstance(body, dict) else {}
    code = parsed.get("error", "unknown_error")
    message = parsed.get("message") or f"Request failed with status {status}"
    raise error_from_status(status, code, message, request_id_from(headers), parsed)
