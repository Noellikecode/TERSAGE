"""Authentication for the internal push endpoint.

The push endpoint injects events into the fleet. An unauthenticated one would
let anyone publish a ``fact.written`` for any address and have the fleet re-read
and act on it, so this module exists before the endpoint does.

Two mechanisms, chosen by mode, and never both:

* **Fake mode** verifies a bearer token derived from ``DEMO_SEED``. It is not a
  secret in any file -- it is derived at startup and printed by ``firstdue
  status`` -- and it is compared in constant time. If no token can be resolved,
  the endpoint refuses everything rather than falling open.
* **Live mode** verifies a Google-issued OIDC identity token: the signature, the
  audience, and that the caller is one of the service accounts these endpoints
  are for. A shared secret standing next to that would be the weaker of two
  doors, so it is not offered.

There is more than one such service account because more than one Google service
calls in -- Pub/Sub pushes events, Cloud Scheduler ticks the slow loop -- and
they are separate IAM identities deliberately. The list is closed and configured;
an empty one accepts nobody rather than everybody.

Nothing here logs a token, and no failure message says which check failed --
telling a caller whether the audience or the signature was wrong is telling them
how to get closer.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import ConfigurationError, NotAuthorizedError
from firstdue.observability.logging import get_logger
from firstdue.settings import Settings

logger = get_logger(__name__)

BEARER_PREFIX = "Bearer "

#: Verifies a Google-issued OIDC token against an audience and returns its
#: claims, or raises.
#:
#: A seam rather than an abstraction. There is exactly one implementation and
#: there is not meant to be a second; what this buys is that the live
#: verification paths -- the ones that by definition only run in production,
#: where nobody is watching a test fail -- can be driven from a test against a
#: stub. An unexercised authentication path is one nobody has read closely.
OidcVerifier = Callable[[str, str], dict[str, Any]]


def verify_google_oidc(token: str, audience: str) -> dict[str, Any]:
    """Check a token against Google's published keys and the expected audience.

    Imported lazily: a fake-mode process must not need ``google-auth`` installed
    to serve these endpoints.

    Raises whatever ``google-auth`` raises on a token it will not vouch for.
    Deciding what a failed verification means belongs to the caller, because the
    two callers guard different trust boundaries.
    """
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:
        raise ConfigurationError(
            "google-auth is not installed; install the 'google' extra",
            details={"package": "google-auth"},
        ) from exc

    # google-auth ships no annotations for this call; every claim the callers
    # use is checked explicitly rather than trusted for its type.
    verify: Any = id_token.verify_oauth2_token
    claims: dict[str, Any] = verify(token, google_requests.Request(), audience=audience)
    return claims


class InternalCaller(BaseModel):
    """Who the push endpoint decided it is talking to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    method: Literal["shared-secret", "oidc"]


def _is_authorized_principal(email: str, allowed: tuple[str, ...]) -> bool:
    """Whether a verified email is one of the identities these endpoints serve.

    Every candidate is compared, and each one in constant time. Returning on the
    first match would reintroduce exactly what ``compare_digest`` is here to
    remove: a near miss and a far miss must cost the same.

    An empty ``allowed`` returns False. That is the whole fail-closed story --
    there is no wildcard, and "nobody is configured" means nobody gets in.
    """
    matched = False
    for candidate in allowed:
        if hmac.compare_digest(email, candidate):
            matched = True
    return matched


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith(BEARER_PREFIX):
        raise NotAuthorizedError("internal endpoints require a bearer token")
    token = header[len(BEARER_PREFIX) :].strip()
    if not token:
        raise NotAuthorizedError("internal endpoints require a bearer token")
    return token


class InternalPushAuthenticator:
    """Verifies callers of the internal push endpoint."""

    def __init__(self, settings: Settings, *, verifier: OidcVerifier = verify_google_oidc) -> None:
        self._settings = settings
        self._verifier = verifier

    @property
    def is_configured(self) -> bool:
        if self._settings.use_fake_agents:
            return bool(self._settings.resolved_internal_push_token)
        return bool(
            self._settings.internal_push_audience
            and self._settings.internal_caller_service_accounts
        )

    def verify(self, request: Request) -> InternalCaller:
        """Authenticate one request.

        Raises:
            ConfigurationError: when no verification is possible. Refusing is
                the only safe answer; an endpoint that cannot check identity
                must not accept an event.
            NotAuthorizedError: when the caller failed verification.
        """
        if not self.is_configured:
            raise ConfigurationError(
                "the internal push endpoint has no configured verifier and is refusing traffic",
                details={"mode": self._settings.mode_label},
            )
        token = _bearer_token(request)
        if self._settings.use_fake_agents:
            return self._verify_shared_secret(token)
        return self._verify_oidc(token)

    def _verify_shared_secret(self, token: str) -> InternalCaller:
        expected = self._settings.resolved_internal_push_token or ""
        if not hmac.compare_digest(token, expected):
            logger.warning("internal_push_rejected", extra={"method": "shared-secret"})
            raise NotAuthorizedError("internal push token is not valid")
        return InternalCaller(subject="fake-mode-push", method="shared-secret")

    def _verify_oidc(self, token: str) -> InternalCaller:
        """Verify a Google-issued OIDC token against the push audience."""
        audience = self._settings.internal_push_audience or ""
        try:
            claims = self._verifier(token, audience)
        except ConfigurationError:
            # A missing google-auth package is the operator's problem, not the
            # caller's. Rendering it as a refusal would send Pub/Sub retrying a
            # credential that was never the thing that was wrong.
            raise
        except Exception as exc:
            # Deliberately opaque: which check failed is not the caller's business.
            logger.warning("internal_push_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("internal push identity is not valid") from exc

        email = str(claims.get("email", ""))
        verified = bool(claims.get("email_verified", False))
        allowed = self._settings.internal_caller_service_accounts
        if not verified or not _is_authorized_principal(email, allowed):
            logger.warning("internal_push_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("internal push identity is not valid")
        # The subject is the identity that actually called, not the list it was
        # found in: the audit log has to be able to tell a scheduled tick from a
        # pushed event after the fact.
        return InternalCaller(subject=email, method="oidc")


def get_authenticator(request: Request) -> InternalPushAuthenticator:
    """FastAPI dependency: the authenticator built for this app's settings."""
    authenticator: InternalPushAuthenticator = request.app.state.push_auth
    return authenticator


def require_internal_caller(request: Request) -> InternalCaller:
    """FastAPI dependency: authenticate, or raise."""
    return get_authenticator(request).verify(request)
