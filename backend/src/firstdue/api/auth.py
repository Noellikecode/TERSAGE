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
  audience, and the service account that Pub/Sub pushes as. A shared secret
  standing next to that would be the weaker of two doors, so it is not offered.

Nothing here logs a token, and no failure message says which check failed --
telling a caller whether the audience or the signature was wrong is telling them
how to get closer.
"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import ConfigurationError, NotAuthorizedError
from firstdue.observability.logging import get_logger
from firstdue.settings import Settings

logger = get_logger(__name__)

BEARER_PREFIX = "Bearer "


class InternalCaller(BaseModel):
    """Who the push endpoint decided it is talking to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    method: Literal["shared-secret", "oidc"]


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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        if self._settings.use_fake_agents:
            return bool(self._settings.resolved_internal_push_token)
        return bool(
            self._settings.internal_push_audience and self._settings.internal_push_service_account
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
        """Verify a Google-issued OIDC token.

        Imported lazily: a fake-mode process must not need ``google-auth``
        installed to serve this endpoint.
        """
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token
        except ImportError as exc:  # pragma: no cover - live mode only
            raise ConfigurationError(
                "google-auth is not installed; install the 'google' extra",
                details={"package": "google-auth"},
            ) from exc

        # google-auth ships no annotations for this call; the result is checked
        # field by field below rather than trusted for its type.
        verify: Any = id_token.verify_oauth2_token
        try:
            claims: dict[str, Any] = verify(
                token,
                google_requests.Request(),
                audience=self._settings.internal_push_audience,
            )
        except Exception as exc:  # pragma: no cover - live mode only
            # Deliberately opaque: which check failed is not the caller's business.
            logger.warning("internal_push_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("internal push identity is not valid") from exc

        email = str(claims.get("email", ""))
        verified = bool(claims.get("email_verified", False))
        expected = self._settings.internal_push_service_account or ""
        if not verified or not hmac.compare_digest(email, expected):
            logger.warning("internal_push_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("internal push identity is not valid")
        return InternalCaller(subject=email, method="oidc")


def get_authenticator(request: Request) -> InternalPushAuthenticator:
    """FastAPI dependency: the authenticator built for this app's settings."""
    authenticator: InternalPushAuthenticator = request.app.state.push_auth
    return authenticator


def require_internal_caller(request: Request) -> InternalCaller:
    """FastAPI dependency: authenticate, or raise."""
    return get_authenticator(request).verify(request)
