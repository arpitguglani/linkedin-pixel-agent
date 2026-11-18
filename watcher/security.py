"""HMAC verification for the Gmail Apps Script webhook."""

from __future__ import annotations

import hashlib
import hmac
import time


def sign(secret: str, timestamp: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
    window_seconds: int = 300,
) -> bool:
    """Constant-time check of signature + replay window."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > window_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature.strip())
