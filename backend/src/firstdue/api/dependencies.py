"""Authorization on every endpoint.

Every route in this API except liveness and readiness requires a caller, and
every route declares the scopes that caller must hold. The declaration is on the
route, not inside the handler, so "which endpoints are protected" is answerable
by reading the router rather than by auditing function bodies.

**Roles carry scopes; scopes are checked exactly.** A viewer holds read scopes.
A captain additionally holds ``write:referral``. A chief additionally holds the
scopes that cut utilities. No role's read scopes imply any write scope, and the
check is a subset test against the exact scopes a route asked for.

Health endpoints are deliberately unauthenticated: a load balancer cannot hold a
credential, and an unauthenticated liveness probe leaks nothing an attacker
could not learn by observing that the port is open.

In fake mode a caller presents a bearer token derived from ``DEMO_SEED`` --
deterministic, in no file, and printed by ``firstdue status``. Live mode
verifies a Google-issued OIDC token and binds the verified email to a role from
``CONSOLE_ROLE_BINDINGS``; see :meth:`ConsoleAuthenticator._verify_oidc` for
what that does and does not establish. There is no mode in which a missing
credential is treated as a viewer, and no mode in which a caller chooses their
own role.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from enum import StrEnum
from typing import Final

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

# The console verifies the same kind of token as the push endpoint, against a
# different audience. One implementation, imported: two copies of a token
# verifier would mean one of them is the copy nobody exercises.
from firstdue.api.auth import OidcVerifier, verify_google_oidc
from firstdue.domain.enums import Department, Scope
from firstdue.domain.identity import WRITE_SCOPES
from firstdue.errors import ConfigurationError, NotAuthorizedError
from firstdue.observability.logging import get_logger
from firstdue.settings import Settings

logger = get_logger(__name__)

BEARER_PREFIX: Final[str] = "Bearer "


class Role(StrEnum):
    """Who is holding the tablet.

    Three roles, because the system has exactly three decisions a human makes:
    look at it, file a referral, or commit another agency's resources.
    """

    VIEWER = "viewer"
    CAPTAIN = "captain"
    CHIEF = "chief"


#: Read scopes every authenticated caller holds. Note what is absent:
#: ``read:ems-derived`` is not here, because person-level derivation is reached
#: by an agent under an incident grant, not by a console session.
VIEWER_SCOPES: Final[frozenset[Scope]] = frozenset(
    {
        Scope.READ_PROFILE,
        Scope.READ_GEOMETRY,
        Scope.READ_PUBLIC_RECORDS,
        Scope.READ_AUDIT,
    }
)

#: A captain files referrals and dispatches companies.
CAPTAIN_SCOPES: Final[frozenset[Scope]] = VIEWER_SCOPES | {
    Scope.READ_TIER_II_METADATA,
    Scope.WRITE_REFERRAL,
    Scope.WRITE_WORK_ORDER,
    Scope.WRITE_PROFILE,
}

#: A chief additionally commits other agencies.
CHIEF_SCOPES: Final[frozenset[Scope]] = CAPTAIN_SCOPES | {
    Scope.NOTIFY_AGENCY,
    Scope.REQUEST_UTILITY_SHUTOFF,
    Scope.REQUEST_ROAD_CLOSURE,
}

ROLE_SCOPES: Final[dict[Role, frozenset[Scope]]] = {
    Role.VIEWER: VIEWER_SCOPES,
    Role.CAPTAIN: CAPTAIN_SCOPES,
    Role.CHIEF: CHIEF_SCOPES,
}


class Caller(BaseModel):
    """An authenticated human or service, and what they may do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    role: Role
    department: Department = Department.FIRE
    scopes: frozenset[Scope]

    def holds(self, *scopes: Scope) -> bool:
        return set(scopes).issubset(self.scopes)

    @property
    def can_write(self) -> bool:
        return bool(self.scopes & WRITE_SCOPES)


def console_token(settings: Settings, role: Role) -> str | None:
    """The fake-mode bearer token for a role.

    Derived from ``DEMO_SEED`` so the demo authenticates without a secret
    existing in any file. Live mode returns ``None``: there the caller presents
    an OIDC token, and a shared secret beside it would be the weaker door.
    """
    if not settings.use_fake_agents:
        return None
    material = f"firstdue-console|{settings.demo_seed}|{role}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith(BEARER_PREFIX):
        raise NotAuthorizedError("this endpoint requires a bearer token")
    token = header[len(BEARER_PREFIX) :].strip()
    if not token:
        raise NotAuthorizedError("this endpoint requires a bearer token")
    return token


class ConsoleAuthenticator:
    """Resolves a request to a caller, or refuses it."""

    def __init__(self, settings: Settings, *, verifier: OidcVerifier = verify_google_oidc) -> None:
        self._settings = settings
        self._verifier = verifier
        #: Read once, at startup, so a request never depends on re-parsing it.
        self._role_bindings = settings.console_roles
        if not settings.use_fake_agents and not self._role_bindings:
            # Not fatal: a deployment may have nobody enrolled yet, and refusing
            # to start would take the read-only console down with it. But it is
            # worth saying out loud, because until one principal is bound every
            # caller is a viewer -- which means no referral and no shutoff can be
            # approved by anyone.
            logger.warning("console_role_bindings_empty")

    @property
    def is_configured(self) -> bool:
        if self._settings.use_fake_agents:
            return console_token(self._settings, Role.VIEWER) is not None
        return bool(self._settings.console_audience)

    def authenticate(self, request: Request) -> Caller:
        """Identify the caller.

        Raises:
            ConfigurationError: when no verifier is configured. Refusing is the
                only safe answer; an endpoint that cannot identify a caller must
                not act for one.
            NotAuthorizedError: when the credential does not verify.
        """
        if not self.is_configured:
            raise ConfigurationError(
                "no console verifier is configured; the API is refusing traffic",
                details={"mode": self._settings.mode_label},
            )
        token = _bearer(request)
        if self._settings.use_fake_agents:
            return self._match_role(token)
        return self._verify_oidc(token)

    def _match_role(self, token: str) -> Caller:
        for role in Role:
            expected = console_token(self._settings, role)
            if expected and hmac.compare_digest(token, expected):
                return Caller(
                    subject=f"demo-{role}", role=role, scopes=frozenset(ROLE_SCOPES[role])
                )
        logger.warning("console_auth_rejected", extra={"method": "shared-secret"})
        raise NotAuthorizedError("credential is not valid")

    def _verify_oidc(self, token: str) -> Caller:
        """Verify a Google-issued OIDC token and bind its principal to a role.

        **This is principal-level binding, not per-user single sign-on.** What
        is established is that Google issued this token for this audience and
        that the verified email on it is one ``CONSOLE_ROLE_BINDINGS`` names.
        There is no session, no group membership, and no directory lookup, and
        the binding list is configuration an operator maintains by hand. A
        console that needs real per-user authentication needs an identity-aware
        proxy in front of it -- that is a deployment decision this process
        cannot make for itself, and pretending otherwise here would be claiming
        an authentication story the code does not have.

        The role is not read from a token claim. A Google-issued ID token, for a
        service account or a user, carries no custom claims at all, so a
        ``firstdue_role`` claim could never be present: defaulting off it meant
        every caller in live mode was a viewer and no approval gate in the system
        was reachable. A gate nobody can pass is not a gate.

        An unbound principal is a viewer -- the least authority the system has,
        and the right answer for a caller nobody has vouched for.
        """
        audience = self._settings.console_audience or ""
        try:
            claims = self._verifier(token, audience)
        except ConfigurationError:
            # A missing google-auth package is the operator's problem, not the
            # caller's; rendering it as a refusal would send an officer hunting
            # for a credential that is fine.
            raise
        except Exception as exc:
            # Deliberately opaque: which check failed is not the caller's business.
            logger.warning("console_auth_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("credential is not valid") from exc

        email = str(claims.get("email", "")).strip().lower()
        if not claims.get("email_verified") or not email:
            logger.warning("console_auth_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("credential is not valid")
        bound = self._role_bindings.get(email, Role.VIEWER.value)
        try:
            role = Role(bound)
        except ValueError as exc:
            # Settings already refused an unknown role name at startup, so this
            # is reachable only if the two vocabularies drift. It is here because
            # the alternative is a ValueError escaping as a 500, and an
            # authorization failure rendered as an internal error is one nobody
            # reads as an authorization failure.
            logger.warning("console_auth_rejected", extra={"method": "oidc"})
            raise NotAuthorizedError("credential is not valid") from exc
        return Caller(subject=email, role=role, scopes=frozenset(ROLE_SCOPES[role]))


def get_authenticator(request: Request) -> ConsoleAuthenticator:
    authenticator: ConsoleAuthenticator = request.app.state.console_auth
    return authenticator


def require_scopes(*required: Scope) -> Callable[[Request], Caller]:
    """Dependency factory: authenticate, then require these exact scopes.

    Declared on the route so the authorization matrix is readable from the
    router. Every endpoint that is not a health probe carries one of these.
    """

    def dependency(request: Request) -> Caller:
        caller = get_authenticator(request).authenticate(request)
        if not caller.holds(*required):
            missing = sorted(str(scope) for scope in set(required) - caller.scopes)
            logger.warning(
                "console_scope_denied",
                extra={"path": request.url.path, "role": str(caller.role)},
            )
            raise NotAuthorizedError(
                "this action requires scopes the caller does not hold",
                details={"missing_scopes": ", ".join(missing)},
            )
        return caller

    return dependency


#: Pre-built dependencies for the scopes routes actually use.
require_read = require_scopes(Scope.READ_PROFILE)
require_geometry_read = require_scopes(Scope.READ_GEOMETRY)
require_audit_read = require_scopes(Scope.READ_AUDIT)
require_profile_write = require_scopes(Scope.WRITE_PROFILE)
require_work_order = require_scopes(Scope.WRITE_WORK_ORDER)
require_referral_write = require_scopes(Scope.WRITE_REFERRAL)
