"""Signed callbacks.

The receiving systems -- referral intake, the records system, an aid partner's
notification endpoint -- call back with case numbers and acknowledgements. Those
callbacks change what a profile says, so they are authenticated by signature
rather than by the caller asserting who they are.

HMAC-SHA256 over the canonical request, with the timestamp inside the signed
material and a freshness window on top. The timestamp being *signed* is the
point: without it, a captured callback can be replayed forever.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Final

from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

SIGNATURE_HEADER: Final[str] = "X-FirstDue-Signature"
TIMESTAMP_HEADER: Final[str] = "X-FirstDue-Timestamp"
#: How stale a signed callback may be. Long enough for a retry, short enough
#: that a captured request stops working within the same incident.
DEFAULT_TOLERANCE: Final[timedelta] = timedelta(minutes=5)


class SignatureError(Exception):
    """Raised when a callback signature does not verify.

    Not a :class:`~firstdue.errors.FirstDueError` subclass on purpose: the API
    layer turns this into a bare 401 with no detail. Telling a caller *why*
    their forgery failed is telling them how to fix it.
    """


def canonical_material(*, method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """The exact bytes that get signed.

    Method, path, timestamp, and a hash of the body. Signing the body hash
    rather than the body keeps the material small and fixed-length while still
    binding the signature to the content.
    """
    digest = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{digest}".encode()


def sign_payload(*, secret: str, method: str, path: str, timestamp: datetime, body: bytes) -> str:
    """Sign a callback. Used by the fake receiving systems and by tests."""
    material = canonical_material(
        method=method, path=path, timestamp=timestamp.isoformat(), body=body
    )
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    method: str,
    path: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    now: datetime,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> None:
    """Verify a signed callback, or raise.

    Raises:
        SignatureError: for a missing, stale, malformed, or wrong signature.
            One exception type for all of them, deliberately: a caller learns
            that it failed and nothing about which check caught them.
    """
    if not signature or not timestamp:
        raise SignatureError("callback is not signed")

    try:
        sent_at = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise SignatureError("callback timestamp is not valid") from exc
    if sent_at.tzinfo is None:
        raise SignatureError("callback timestamp is not timezone-aware")

    if abs((now - sent_at).total_seconds()) > tolerance.total_seconds():
        # A signature that never goes stale is a replay waiting to happen.
        logger.warning("callback_signature_stale")
        raise SignatureError("callback is outside the freshness window")

    expected = sign_payload(secret=secret, method=method, path=path, timestamp=sent_at, body=body)
    if not hmac.compare_digest(expected, signature):
        logger.warning("callback_signature_invalid")
        raise SignatureError("callback signature does not verify")
