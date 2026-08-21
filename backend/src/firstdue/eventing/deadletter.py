"""Dead letters -- surfaced, never dropped.

A message the fleet could not process is an operational fact about the fleet. It
goes somewhere an operator can see it, with the number of attempts and a stable
error code, and it stays there. The one thing a dead letter never carries is the
handler's exception message: envelopes are identifiers, and so are their
failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.events import EventEnvelope, Topic
from firstdue.reliability.retry import FailureClass


class DeadLetterRecord(BaseModel):
    """One envelope that will not be delivered, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope: EventEnvelope
    subscriber: str = Field(min_length=1, max_length=120)
    attempts: int = Field(ge=1)
    #: Stable error code or classification name. Never a handler message.
    reason: str = Field(min_length=1, max_length=120)
    failure_class: FailureClass
    dead_lettered_at: datetime

    @property
    def topic(self) -> Topic:
        return self.envelope.topic

    @property
    def is_poison(self) -> bool:
        """True when redelivering could not possibly help."""
        return self.failure_class in (FailureClass.POISON, FailureClass.PERMANENT)


class InMemoryDeadLetterStore:
    """The dead-letter queue for fake mode and for tests."""

    def __init__(self) -> None:
        self._records: list[DeadLetterRecord] = []
        self._lock = asyncio.Lock()

    async def add(self, record: DeadLetterRecord) -> DeadLetterRecord:
        async with self._lock:
            self._records.append(record)
            return record

    async def list_all(
        self, *, subscriber: str | None = None, topic: Topic | None = None
    ) -> Sequence[DeadLetterRecord]:
        return [
            record
            for record in self._records
            if (subscriber is None or record.subscriber == subscriber)
            and (topic is None or record.envelope.topic is topic)
        ]

    @property
    def records(self) -> Sequence[DeadLetterRecord]:
        """Every dead letter, in the order they were recorded."""
        return list(self._records)

    @property
    def count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()
