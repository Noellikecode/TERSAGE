"""Calendar, Gmail, and Cloud Storage boundaries.

Each class does three things: build its client lazily, issue one call, and map
the result into the domain model. Retry policy, breaker policy, and idempotency
bookkeeping live above them and are already written -- duplicating any of it
here would give the live path different behaviour from the fake one.

Idempotency is enforced on our side rather than trusted to the API: Calendar and
Gmail have no idempotency-key concept, so the key-to-artifact mapping lives in
the **durable** :class:`~firstdue.ports.repositories.IdempotencyRepository` --
the same store the Pub/Sub dedupe uses. A process-local dictionary would be a
guarantee that lasts until the next Cloud Run instance swap, and "we invited the
company twice because we restarted" is the failure the key exists to prevent.

Cloud Storage gets a precondition instead, which is stronger still: the database
enforces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from firstdue.domain.idempotency import (
    DEFAULT_CLAIM_TTL,
    IdempotencyOutcome,
    IdempotencyRecord,
    request_hash,
)
from firstdue.errors import ConfigurationError, IdempotencyMismatchError, SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.office import CalendarEvent, MailMessage, StoredObject
from firstdue.ports.repositories import IdempotencyRepository

logger = get_logger(__name__)


class DurableArtifactDedupe:
    """Key-to-artifact memory that survives an instance swap.

    Calendar and Gmail have no idempotency-key concept of their own, so the
    memory has to be ours -- and it has to be durable, because Cloud Run
    replaces instances and "we double-invited the company because we restarted"
    is precisely what the key exists to prevent.
    """

    def __init__(self, repository: IdempotencyRepository, *, clock: Clock, scope: str) -> None:
        self._repository = repository
        self._clock = clock
        self._scope = scope

    def _key(self, idempotency_key: str) -> str:
        return idempotency_key if len(idempotency_key) >= 8 else idempotency_key.ljust(8, "-")

    async def completed_ref(self, idempotency_key: str) -> str | None:
        """The external reference a previous send or insert produced, if any."""
        record = await self._repository.get(self._scope, self._key(idempotency_key))
        if record is None or record.result_ref is None:
            return None
        return record.result_ref

    async def claim(self, idempotency_key: str) -> bool:
        """Claim the key. False means somebody else is already doing this."""
        now = self._clock.now()
        record = IdempotencyRecord(
            key=self._key(idempotency_key),
            scope=self._scope,
            request_hash=request_hash({"key": idempotency_key}),
            claimed_at=now,
            claim_expires_at=now + DEFAULT_CLAIM_TTL,
        )
        try:
            claim = await self._repository.claim(record)
        except IdempotencyMismatchError:
            return False
        return claim.outcome is IdempotencyOutcome.FRESH

    async def complete(self, idempotency_key: str, external_ref: str) -> None:
        await self._repository.complete(
            self._scope,
            self._key(idempotency_key),
            at=self._clock.now(),
            result_ref=external_ref,
        )


def _require(package: str, module: str) -> Any:
    """Import a Google client, or fail loudly with what is missing."""
    try:
        import importlib

        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - live mode only
        raise ConfigurationError(
            f"{package} is not installed; install the 'google' extra "
            "or run with USE_FAKE_AGENTS=true",
            details={"package": package},
        ) from exc


class GoogleCalendarClient:
    """Google Calendar. One insert per survey, deduped durably."""

    def __init__(
        self,
        *,
        clock: Clock,
        idempotency: IdempotencyRepository,
        credentials: Any | None = None,
    ) -> None:
        self._clock = clock
        self._credentials = credentials
        self._service: Any | None = None
        self._dedupe = DurableArtifactDedupe(idempotency, clock=clock, scope="calendar")

    def _client(self) -> Any:  # pragma: no cover - live mode only
        if self._service is None:
            discovery = _require("google-api-python-client", "googleapiclient.discovery")
            self._service = discovery.build(
                "calendar", "v3", credentials=self._credentials, cache_discovery=False
            )
        return self._service

    async def create_event(
        self, event: CalendarEvent, *, idempotency_key: str
    ) -> CalendarEvent:  # pragma: no cover - live mode only
        existing = await self._dedupe.completed_ref(idempotency_key)
        if existing is not None:
            return event.model_copy(update={"external_ref": existing})
        if not await self._dedupe.claim(idempotency_key):
            # Another instance is mid-insert. Doing nothing is correct: its
            # event is the one the crew will see.
            return event
        body = {
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": event.starts_at.isoformat()},
            "end": {"dateTime": event.ends_at.isoformat()},
            "attendees": [{"email": address} for address in event.attendees],
        }
        try:
            created = (
                self._client().events().insert(calendarId=event.calendar_id, body=body).execute()
            )
        except Exception as exc:
            raise SourceUnavailableError(
                "calendar is unreachable", details={"error_type": type(exc).__name__}
            ) from exc
        stored = event.model_copy(update={"external_ref": str(created.get("id"))})
        await self._dedupe.complete(idempotency_key, stored.external_ref or "")
        return stored

    async def cancel_event(
        self, event_id: str, *, at: datetime
    ) -> CalendarEvent:  # pragma: no cover - live mode only
        """The compensating action.

        Takes the external reference from the caller's own record rather than
        from process memory -- the compensation record written when the event
        was created is what names it, and that record is durable.
        """
        try:
            self._client().events().delete(calendarId="primary", eventId=event_id).execute()
        except Exception as exc:
            raise SourceUnavailableError(
                "calendar is unreachable", details={"error_type": type(exc).__name__}
            ) from exc
        return CalendarEvent(
            event_id=event_id,
            calendar_id="primary",
            summary="cancelled",
            description="",
            starts_at=at,
            ends_at=at,
            cancelled_at=at,
        )

    async def list_events(
        self, calendar_id: str
    ) -> Sequence[CalendarEvent]:  # pragma: no cover - live mode only
        """Not served from this process.

        A live listing would have to query Calendar, and nothing in the fleet
        needs it -- the incident log is the record of what was scheduled.
        """
        return []


class GmailClient:
    """Gmail. One send per notification, deduped durably."""

    def __init__(
        self,
        *,
        sender: str,
        clock: Clock,
        idempotency: IdempotencyRepository,
        credentials: Any | None = None,
    ) -> None:
        self._sender = sender
        self._credentials = credentials
        self._service: Any | None = None
        self._dedupe = DurableArtifactDedupe(idempotency, clock=clock, scope="mail")

    def _client(self) -> Any:  # pragma: no cover - live mode only
        if self._service is None:
            discovery = _require("google-api-python-client", "googleapiclient.discovery")
            self._service = discovery.build(
                "gmail", "v1", credentials=self._credentials, cache_discovery=False
            )
        return self._service

    async def send(
        self, message: MailMessage, *, idempotency_key: str
    ) -> MailMessage:  # pragma: no cover - live mode only
        existing = await self._dedupe.completed_ref(idempotency_key)
        if existing is not None:
            return message.model_copy(update={"external_ref": existing})
        if not await self._dedupe.claim(idempotency_key):
            return message

        import base64
        from email.message import EmailMessage

        payload = EmailMessage()
        payload["To"] = ", ".join(message.to)
        payload["From"] = self._sender
        payload["Subject"] = message.subject
        payload.set_content(message.body)
        encoded = base64.urlsafe_b64encode(payload.as_bytes()).decode("ascii")

        try:
            sent = (
                self._client().users().messages().send(userId="me", body={"raw": encoded}).execute()
            )
        except Exception as exc:
            raise SourceUnavailableError(
                "mail transport is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        stored = message.model_copy(update={"external_ref": str(sent.get("id"))})
        await self._dedupe.complete(idempotency_key, stored.external_ref or "")
        return stored

    async def sent(self) -> Sequence[MailMessage]:  # pragma: no cover - live mode only
        """Not served from this process. The incident log is the record."""
        return []


class GoogleObjectStore:
    """Cloud Storage for pre-incident plan artifacts.

    Uses ``if_generation_match=0`` on the first write, so two instances writing
    the same plan cannot both create it -- the loser gets a precondition failure
    and reads the existing object instead. Stronger than an idempotency key,
    because the database enforces it.
    """

    def __init__(self, *, bucket: str, clock: Clock, client: Any | None = None) -> None:
        if not bucket:
            raise ConfigurationError("the plan store requires a bucket name")
        self._bucket_name = bucket
        self._clock = clock
        self._client = client

    @property
    def bucket(self) -> str:
        return self._bucket_name

    def _bucket_ref(self) -> Any:  # pragma: no cover - live mode only
        if self._client is None:
            storage = _require("google-cloud-storage", "google.cloud.storage")
            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    async def put(
        self, *, object_id: str, content: bytes, content_type: str, idempotency_key: str
    ) -> StoredObject:  # pragma: no cover - live mode only
        import hashlib

        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket_ref().blob(object_id)
        try:
            blob.upload_from_string(content, content_type=content_type, if_generation_match=0)
        except PreconditionFailed:
            logger.info("plan_object_already_written", extra={"object_id": object_id})
            blob.reload()
        except Exception as exc:
            raise SourceUnavailableError(
                "plan store is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        return StoredObject(
            object_id=object_id,
            bucket=self._bucket_name,
            content_type=content_type,
            size_bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            written_at=self._clock.now(),
            uri=f"gs://{self._bucket_name}/{object_id}",
        )

    async def get(self, object_id: str) -> StoredObject | None:  # pragma: no cover - live only
        import hashlib

        blob = self._bucket_ref().get_blob(object_id)
        if blob is None:
            return None
        content = blob.download_as_bytes()
        return StoredObject(
            object_id=object_id,
            bucket=self._bucket_name,
            content_type=blob.content_type or "application/octet-stream",
            size_bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            written_at=self._clock.now(),
            uri=f"gs://{self._bucket_name}/{object_id}",
        )

    async def read(self, object_id: str) -> bytes | None:  # pragma: no cover - live mode only
        blob = self._bucket_ref().get_blob(object_id)
        if blob is None:
            return None
        data: bytes = blob.download_as_bytes()
        return data
