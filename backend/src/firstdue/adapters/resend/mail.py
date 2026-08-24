"""Resend as a mail transport, behind the mail port that already exists.

A second :class:`~firstdue.ports.office.MailClient`, not a second port. The
referral email is subject to the same audit trail, the same durable idempotency
key, and the same fake/live parity as every other outward write in the fleet,
and all of that is already wired to ``MailClient``. Inventing a port here would
mean a second copy of the wiring, and a second copy is a second thing that can
disagree with the first -- the same reasoning that put Model Armor and the
Workspace clients behind ports the system already had.

No vendor SDK. Resend's send API is one ``POST`` with a bearer token, and
``httpx`` is a base dependency, so the whole integration is a request, a status
code, and an id. An SDK would add a dependency, a client lifecycle, and its own
retry policy competing with :mod:`firstdue.reliability.retry`.

Two things this file will not do. It will not remember what it sent in process
memory -- the key-to-message mapping goes in the **durable** idempotency
repository, because Cloud Run replaces instances and "we referred the same
property to the building department twice because we restarted" is exactly what
the key exists to prevent. And it will not put the API key or the message body
anywhere a log sink can see: the only things logged here are a message id, an
attempt number, a status code, and a stable error code.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Final

import httpx

# The same dedupe the Gmail client uses, imported rather than copied. Two
# implementations of "have we already sent this" are two implementations that
# can drift, and the one that drifts is the one nobody is looking at.
from firstdue.adapters.google.office import DurableArtifactDedupe
from firstdue.errors import (
    ConfigurationError,
    FirstDueError,
    SourceUnavailableError,
    UpstreamTimeoutError,
    ValidationError,
)
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.office import MailMessage
from firstdue.ports.repositories import IdempotencyRepository
from firstdue.reliability.retry import (
    DEFAULT_POLICY,
    RetryPolicy,
    backoff_ms,
    classify,
    error_code_of,
    is_retryable,
)

logger = get_logger(__name__)

#: Resend's send endpoint. One call, one message.
RESEND_ENDPOINT: Final[str] = "https://api.resend.com/emails"

#: The setting a missing credential names, so the operator is told which
#: environment variable to set rather than that "mail is misconfigured".
API_KEY_SETTING: Final[str] = "RESEND_API_KEY"
SENDER_SETTING: Final[str] = "RESEND_FROM_ADDRESS"

#: Statuses that mean the vendor is unwell rather than the request is wrong.
#: 408 and 429 are 4xx by number and transient by meaning, so they are named
#: here rather than left to the "4xx is permanent" rule.
_TRANSIENT_STATUS: Final[frozenset[int]] = frozenset({408, 429})

#: Statuses that mean the credential itself was refused. A refused key is a
#: deployment fault, not a busy dependency, and retrying re-refuses it.
_CREDENTIAL_STATUS: Final[frozenset[int]] = frozenset({401, 403})


def _failure_for(status_code: int) -> FirstDueError:
    """Map a Resend status onto the taxonomy ADR 0007 classifies.

    The mapping is the whole retry decision, expressed once. A 5xx and a
    throttle become :class:`SourceUnavailableError`, which classifies
    ``TRANSIENT`` and is retried; a refused credential becomes
    :class:`ConfigurationError`, which classifies ``PERMANENT``; every other
    4xx becomes :class:`ValidationError`, which classifies ``POISON``. Nothing
    here decides *whether* to retry -- it only says what kind of failure this
    was, and :func:`~firstdue.reliability.retry.classify` does the rest.
    """
    if status_code in _CREDENTIAL_STATUS:
        return ConfigurationError(
            "the mail transport refused the configured credential",
            details={"setting": API_KEY_SETTING, "status": str(status_code)},
        )
    if status_code in _TRANSIENT_STATUS or status_code >= 500:
        return SourceUnavailableError(
            "mail transport is unavailable", details={"status": str(status_code)}
        )
    return ValidationError(
        "the mail transport rejected the message", details={"status": str(status_code)}
    )


class ResendMailClient:
    """Resend. One send per message, deduped durably.

    Satisfies :class:`~firstdue.ports.office.MailClient` exactly as
    :class:`~firstdue.adapters.google.office.GmailClient` does, so which
    transport a deployment uses is a wiring decision and nothing above this
    line changes.
    """

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        clock: Clock,
        idempotency: IdempotencyRepository,
        policy: RetryPolicy = DEFAULT_POLICY,
        endpoint: str = RESEND_ENDPOINT,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Fail at construction when the credential is missing.

        A client built without a key would be a client that raises on the first
        referral a captain approves -- which is the worst possible moment to
        discover a deployment is misconfigured. Live mode is a startup failure
        here, never a silent downgrade to the fake.
        """
        if not api_key.strip():
            raise ConfigurationError(
                f"the Resend mail transport requires {API_KEY_SETTING}",
                details={"setting": API_KEY_SETTING},
            )
        if not sender.strip():
            raise ConfigurationError(
                f"the Resend mail transport requires {SENDER_SETTING}",
                details={"setting": SENDER_SETTING},
            )
        self._api_key = api_key
        self._sender = sender
        self._policy = policy
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._transport = transport
        self._dedupe = DurableArtifactDedupe(idempotency, clock=clock, scope="mail")

    async def send(self, message: MailMessage, *, idempotency_key: str) -> MailMessage:
        """Send once. A replayed key returns the original message unsent."""
        existing = await self._dedupe.completed_ref(idempotency_key)
        if existing is not None:
            return message.model_copy(update={"external_ref": existing})
        if not await self._dedupe.claim(idempotency_key):
            # Another instance is mid-send. Doing nothing is correct: its
            # message is the one the recipient will read, and a second one
            # would be the duplicate the key exists to prevent.
            return message

        external_ref = await self._post(message, idempotency_key=idempotency_key)
        await self._dedupe.complete(idempotency_key, external_ref)
        return message.model_copy(update={"external_ref": external_ref})

    async def sent(self) -> Sequence[MailMessage]:
        """Not served from this process.

        Resend can list messages; reading them back would mean holding a second
        copy of every body in a process that has no reason to. The audit log
        and the incident log are the record of what went out.
        """
        return []

    # ------------------------------------------------------------ internals

    async def _post(self, message: MailMessage, *, idempotency_key: str) -> str:
        """One send, retried only where retrying can help.

        The class of the failure decides, not its status code: ADR 0007's
        classification is what says a throttle is worth another attempt and a
        malformed recipient is not. Retrying a message the vendor has already
        refused on its merits is how a queue stops moving.
        """
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await self._post_once(message, idempotency_key=idempotency_key)
            except FirstDueError as exc:
                failure = classify(exc)
                logger.warning(
                    "referral_mail_send_failed",
                    extra={
                        # A message id, an attempt, and a stable code. Not the
                        # body, not the recipients, and not the credential.
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "failure_class": str(failure),
                        "error_code": error_code_of(exc),
                    },
                )
                if not is_retryable(failure) or attempt >= self._policy.max_attempts:
                    raise
                delay = backoff_ms(attempt + 1, policy=self._policy, seed=message.message_id)
                await asyncio.sleep(delay / 1000.0)
        # Unreachable while max_attempts >= 1, which the policy enforces.
        raise SourceUnavailableError(  # pragma: no cover - defensive
            "mail transport is unavailable", details={"message_id": message.message_id}
        )

    async def _post_once(self, message: MailMessage, *, idempotency_key: str) -> str:
        """The single vendor call. Everything else in this file is policy."""
        payload: dict[str, Any] = {
            "from": self._sender,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.body,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Resend honours this header too. Belt and braces: our own durable
            # record is the authority, and this makes the vendor agree even in
            # the window between the claim and the completion.
            "Idempotency-Key": idempotency_key,
        }

        try:
            # A client per send rather than a pooled one. A referral is a rare
            # event, and a connection held open to a vendor for days is a
            # resource with an owner nobody can name.
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(
                "mail transport did not answer in time",
                details={"error_type": type(exc).__name__},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                "mail transport is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        if response.status_code >= 400:
            raise _failure_for(response.status_code)
        return _message_id_of(response)


def _message_id_of(response: httpx.Response) -> str:
    """The vendor's own id for the message, or a refusal to pretend there is one.

    A 2xx without an id is a contract the vendor did not keep, and it is
    :class:`ValidationError` rather than a transient: retrying would send the
    message a second time to chase an id, which is the one outcome the whole
    file exists to avoid.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(
            "the mail transport accepted the message but did not answer with JSON",
            details={"status": str(response.status_code)},
        ) from exc
    external_ref = str(body.get("id") or "") if isinstance(body, dict) else ""
    if not external_ref:
        raise ValidationError(
            "the mail transport accepted the message without returning an id",
            details={"status": str(response.status_code)},
        )
    return external_ref[:200]
