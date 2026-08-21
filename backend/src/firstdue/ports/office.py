"""Calendar, mail, and object storage.

Three systems the fleet writes into that are not municipal records: a company's
calendar, a crew's inbox, and the bucket that holds pre-incident plans.

They share the two rules every external write in this system follows -- an
idempotency key, and a named compensating action -- because "we sent the crew
two conflicting survey invitations" and "we filed the referral twice" are the
same class of failure.

Everything here is a boundary. The fake implementations do real deduplication
and real compensation; the Google-backed ones are thin and import their clients
lazily, so a credential-free process never loads them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class CalendarEvent(BaseModel):
    """A scheduled company survey."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=120)
    calendar_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=8000)
    starts_at: datetime
    ends_at: datetime
    attendees: tuple[str, ...] = ()
    #: The external system's own reference, returned on creation.
    external_ref: str | None = Field(default=None, max_length=200)
    cancelled_at: datetime | None = None


class MailMessage(BaseModel):
    """A crew notification.

    Body text is composed from resolved fields. Nothing in it is a tactical
    instruction: it says a survey is scheduled and what the file already
    disagrees about.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1, max_length=120)
    to: tuple[str, ...] = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    external_ref: str | None = Field(default=None, max_length=200)


class StoredObject(BaseModel):
    """An artifact written to object storage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: str = Field(min_length=1, max_length=200)
    bucket: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(ge=0)
    #: SHA-256 of the bytes written, so a replay is provably the same artifact.
    content_hash: str = Field(min_length=8, max_length=64)
    written_at: datetime
    #: A URI an operator can resolve. Never rendered into a log or audit record.
    uri: str | None = Field(default=None, max_length=400)


@runtime_checkable
class CalendarClient(Protocol):
    async def create_event(self, event: CalendarEvent, *, idempotency_key: str) -> CalendarEvent:
        """Create an event, or return the existing one for this key."""
        ...

    async def cancel_event(self, event_id: str, *, at: datetime) -> CalendarEvent:
        """The compensating action for a created event."""
        ...

    async def list_events(self, calendar_id: str) -> Sequence[CalendarEvent]: ...


@runtime_checkable
class MailClient(Protocol):
    async def send(self, message: MailMessage, *, idempotency_key: str) -> MailMessage:
        """Send once. A replayed key returns the original message unsent."""
        ...

    async def sent(self) -> Sequence[MailMessage]: ...


@runtime_checkable
class ObjectStore(Protocol):
    async def put(
        self, *, object_id: str, content: bytes, content_type: str, idempotency_key: str
    ) -> StoredObject:
        """Write an artifact. Writing the same key twice stores it once."""
        ...

    async def get(self, object_id: str) -> StoredObject | None: ...

    async def read(self, object_id: str) -> bytes | None: ...
