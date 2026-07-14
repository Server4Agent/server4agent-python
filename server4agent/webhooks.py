from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

# Server4Agent signs webhook deliveries as
# HMAC-SHA256(secret, f"{unix_ts}.{raw_body}"), sent as the
# Server4Agent-Signature header: "t=<unix_ts>,v1=<hex digest>".


def _sign(secret: str, body: str, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_webhook_signature(
    secret: str,
    body: str,
    header: Optional[str],
    tolerance_sec: int = 300,
) -> bool:
    """Verify a webhook delivery.

    `body` must be the raw request body exactly as received — decoding and
    re-serializing JSON changes the bytes and breaks the signature. `header`
    is the `Server4Agent-Signature` header value. Deliveries older than
    `tolerance_sec` (default 300s) are rejected to limit replay windows.
    """
    if not header:
        return False

    parts = {}
    for piece in header.split(","):
        if "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        parts[k] = v

    raw_ts = parts.get("t")
    v1 = parts.get("v1")
    if not raw_ts or not v1:
        return False
    try:
        timestamp = int(raw_ts)
    except ValueError:
        return False

    if abs(time.time() - timestamp) > tolerance_sec:
        return False

    expected = _sign(secret, body, timestamp)
    expected_v1 = expected.split("v1=")[1]
    return hmac.compare_digest(expected_v1, v1)
