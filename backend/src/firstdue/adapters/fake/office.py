"""Fake calendar, mail, and object store -- with real semantics.

What is simulated is the receiving, not the behaviour. Each of these dedupes on
the idempotency key, returns the original artifact on a replay, and supports the
compensating action that undoes it. A crew cannot be double-invited and a
pre-plan cannot be written twice, in fake mode or live.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from firstdue.errors import IdempotencyMismatchError, NotFoundError
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.office import CalendarEvent, MailMessage, StoredObject


class FakeCalendar:
    """An in-memory calendar that refuses to double-book one survey."""

    def __init__(self, *, clock: Clock, ids: IdGenerator) -> None:
        self._clock = clock
        self._ids = ids
        self._by_key: dict[str, CalendarEvent] = {}
        self._by_id: dict[str, CalendarEvent] = {}
        self._sequence = 0

    async def create_event(self, event: CalendarEvent, *, idempotency_key: str) -> CalendarEvent:
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        self._sequence += 1
        created = event.model_copy(update={"external_ref": f"CAL-{self._sequence:05d}"})
        self._by_key[idempotency_key] = created
        self._by_id[created.event_id] = created
        return created

    async def cancel_event(self, event_id: str, *, at: datetime) -> CalendarEvent:
        event = self._by_id.get(event_id)
        if event is None:
            raise NotFoundError("calendar event not found", details={"event_id": event_id})
        if event.cancelled_at is not None:
            return event
        cancelled = event.model_copy(update={"cancelled_at": at})
        self._by_id[event_id] = cancelled
        for key, stored in self._by_key.items():
            if stored.event_id == event_id:
                self._by_key[key] = cancelled
        return cancelled

    async def list_events(self, calendar_id: str) -> Sequence[CalendarEvent]:
        return sorted(
            (e for e in self._by_id.values() if e.calendar_id == calendar_id),
            key=lambda e: (e.starts_at, e.event_id),
        )


class FakeMailer:
    """An in-memory mailbox. A replayed key is not a second message."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._by_key: dict[str, MailMessage] = {}
        self._sent: list[MailMessage] = []
        self._sequence = 0

    async def send(self, message: MailMessage, *, idempotency_key: str) -> MailMessage:
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        self._sequence += 1
        sent = message.model_copy(update={"external_ref": f"MSG-{self._sequence:05d}"})
        self._by_key[idempotency_key] = sent
        self._sent.append(sent)
        return sent

    async def sent(self) -> Sequence[MailMessage]:
        return list(self._sent)


class FakeObjectStore:
    """An in-memory bucket that hashes what it stores.

    The hash is what makes a replay provable: the same pre-plan written twice
    has the same content hash, so "we already wrote this" is a fact rather than
    an assumption.
    """

    def __init__(self, *, bucket: str, clock: Clock) -> None:
        self._bucket = bucket
        self._clock = clock
        self._objects: dict[str, StoredObject] = {}
        self._content: dict[str, bytes] = {}
        self._by_key: dict[str, str] = {}

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(
        self, *, object_id: str, content: bytes, content_type: str, idempotency_key: str
    ) -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        existing_id = self._by_key.get(idempotency_key)
        if existing_id is not None:
            existing = self._objects[existing_id]
            if existing.content_hash != digest:
                raise IdempotencyMismatchError(
                    "this key was already used for different content",
                    details={"object_id": existing_id},
                )
            return existing

        stored = StoredObject(
            object_id=object_id,
            bucket=self._bucket,
            content_type=content_type,
            size_bytes=len(content),
            content_hash=digest,
            written_at=self._clock.now(),
            uri=f"gs://{self._bucket}/{object_id}",
        )
        self._objects[object_id] = stored
        self._content[object_id] = content
        self._by_key[idempotency_key] = object_id
        return stored

    async def get(self, object_id: str) -> StoredObject | None:
        return self._objects.get(object_id)

    async def read(self, object_id: str) -> bytes | None:
        return self._content.get(object_id)

    @property
    def object_count(self) -> int:
        return len(self._objects)
