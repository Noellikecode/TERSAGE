"""Event envelopes.

**Agents never call each other, and events never carry payloads.** Every handoff
is an envelope of identifiers; each consumer re-reads state from the store, so a
replayed event produces the same result as the original delivery.

That rule is enforced rather than documented: an envelope's ``ids`` may only
contain identifier-shaped tokens. A validator rejects anything containing
whitespace, which is what a prose payload would look like if someone tried to
smuggle one through.

The envelope is **versioned**. ``schema_version`` is written by the producer and
checked by the consumer: an envelope from a future schema is a poison message,
not something to guess at. Retry state travels with the envelope in
:class:`RetryState` so a redelivery knows which attempt it is on without the
broker having to be asked.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Final, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.work import IdempotencyKey
from firstdue.errors import ValidationError

#: Identifier tokens only: no whitespace, no punctuation that implies prose.
_ID_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]{0,199}$")

MAX_ID_KEYS = 20
MAX_ID_TOKENS = 200

#: The envelope schema this build produces. Consumers refuse anything higher.
CURRENT_SCHEMA_VERSION: Final[int] = 1
#: The oldest schema this build still understands.
MIN_SUPPORTED_SCHEMA_VERSION: Final[int] = 1

EventIdValue: TypeAlias = str | tuple[str, ...]


class Topic(StrEnum):
    """Pub/Sub topics. One per handoff in the fleet."""

    SOURCE_POLL = "source.poll"
    FACT_WRITTEN = "fact.written"
    GEOMETRY_STALE = "geometry.stale"
    CONFLICT_DETECTED = "conflict.detected"
    QUEUE_RANKED = "queue.ranked"
    REFERRAL_STAGED = "referral.staged"
    INCIDENT_OPENED = "incident.opened"
    BRIEF_EMITTED = "brief.emitted"
    NOTIFICATION_SENT = "notification.sent"
    APPROVAL_STAGED = "approval.staged"
    THERMAL_FRAME_RECEIVED = "thermal.frame.received"
    FACT_OBSERVED = "fact.observed"
    RECORD_WRITTEN = "record.written"
    INCIDENT_CLOSED = "incident.closed"
    PROFILE_MATERIALIZED = "profile.materialized"
    AGENT_PUBLISHED = "agent.published"
    #: One routed agent, woken because the incident head's plan named it.
    #:
    #: Every other topic here is an *announcement* -- something happened, and
    #: whoever subscribes reacts. This one is a routing decision crossing a
    #: process boundary, and it exists because those are not the same thing.
    #:
    #: `plan_handoffs` decides who runs on an incident by matching a rule's
    #: required capability and scopes against what each agent's descriptor
    #: declares, and withholds a wake the incident grant cannot cover. In a
    #: single process that decision was enforced, because the interceptor
    #: called the runner directly. Across eleven Cloud Run services it was not:
    #: an agent subscribed to `incident.opened` was started by Pub/Sub whatever
    #: the plan said, so a handoff the plan *withheld for a missing scope* ran
    #: anyway. The safeguard was computed, recorded, and bypassed by the
    #: transport -- the same shape as an approval gate nothing reaches.
    #:
    #: So a routed agent listens here instead, and the plan is the only thing
    #: that starts it in either topology.
    AGENT_WAKE = "agent.wake"


class RetryState(BaseModel):
    """Delivery bookkeeping carried by the envelope itself.

    Attempt counting lives on the envelope rather than in the broker so that the
    in-memory bus, the Pub/Sub push endpoint, and a hand-replayed envelope all
    agree on which attempt they are executing -- and so a dead letter records
    exactly how many times the fleet tried before giving up.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 1 on first delivery. Never zero: there is no such thing as attempt zero.
    attempt: int = Field(default=1, ge=1, le=100)
    max_attempts: int = Field(default=5, ge=1, le=100)
    #: When the first delivery was attempted, so total time-in-retry is visible.
    first_attempted_at: datetime | None = None
    #: Stable error code from the previous attempt. Never a message.
    last_error_code: str | None = Field(default=None, max_length=80)
    #: The backoff that was applied before this attempt, in milliseconds.
    backoff_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_attempts(self) -> Self:
        if self.attempt > self.max_attempts:
            raise ValidationError(
                "retry attempt exceeds max_attempts",
                details={"attempt": self.attempt, "max_attempts": self.max_attempts},
            )
        return self

    @property
    def is_first(self) -> bool:
        return self.attempt == 1

    @property
    def is_final(self) -> bool:
        """True when this attempt is the last one that will ever be made."""
        return self.attempt >= self.max_attempts

    @property
    def remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt)

    def next_attempt(
        self, *, error_code: str, backoff_ms: int, first_attempted_at: datetime
    ) -> RetryState:
        """Advance to the next attempt.

        Raises:
            ValidationError: when the retries are already exhausted. Exhaustion
                is a dead letter, not another attempt.
        """
        if self.is_final:
            raise ValidationError(
                "retries are exhausted; the envelope must be dead-lettered",
                details={"attempt": self.attempt, "max_attempts": self.max_attempts},
            )
        return RetryState(
            attempt=self.attempt + 1,
            max_attempts=self.max_attempts,
            first_attempted_at=self.first_attempted_at or first_attempted_at,
            last_error_code=error_code,
            backoff_ms=backoff_ms,
        )


class EventEnvelope(BaseModel):
    """An identifier-only message between agents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=120)
    topic: Topic
    occurred_at: datetime

    producer: str = Field(min_length=1, max_length=120)
    producer_version: str = Field(min_length=1, max_length=40)
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION, ge=1, le=1000)

    #: Threads one causal chain through the audit log.
    correlation_id: str = Field(min_length=1, max_length=120)
    #: The event that caused this one, if any.
    causation_id: str | None = Field(default=None, max_length=120)

    #: Identifiers only. Consumers re-read state; they never trust this content.
    ids: dict[str, EventIdValue] = Field(default_factory=dict)

    #: Delivery-level dedupe, so redelivery cannot double-execute a consumer.
    idempotency_key: IdempotencyKey

    #: Attempt bookkeeping. Defaults to the first attempt of a fresh publish.
    retry: RetryState = Field(default_factory=RetryState)

    @model_validator(mode="after")
    def _check_ids_are_ids(self) -> Self:
        if len(self.ids) > MAX_ID_KEYS:
            raise ValidationError(
                "event envelope carries too many id keys",
                details={"count": len(self.ids), "max": MAX_ID_KEYS},
            )
        total = 0
        for key, value in self.ids.items():
            if not _ID_TOKEN.match(key):
                raise ValidationError("event id key is not identifier-shaped", details={"key": key})
            tokens: tuple[str, ...] = (value,) if isinstance(value, str) else value
            total += len(tokens)
            for token in tokens:
                if not _ID_TOKEN.match(token):
                    raise ValidationError(
                        "events carry identifiers, not payloads; "
                        "consumers re-read state from the store",
                        details={"key": key},
                    )
        if total > MAX_ID_TOKENS:
            raise ValidationError(
                "event envelope carries too many identifiers",
                details={"count": total, "max": MAX_ID_TOKENS},
            )
        return self

    @property
    def is_supported_schema(self) -> bool:
        """Whether this build understands the envelope at all.

        A newer schema is refused rather than best-effort parsed: silently
        ignoring a field a future producer considered required is how a consumer
        acts on half a message.
        """
        return MIN_SUPPORTED_SCHEMA_VERSION <= self.schema_version <= CURRENT_SCHEMA_VERSION

    def dedupe_key(self, subscriber: str) -> str:
        """The per-consumer key that makes at-least-once delivery exactly-once.

        Scoped by subscriber, because two consumers of the same topic must each
        see the event once -- deduping globally would starve the second one.
        """
        return f"{subscriber}:{self.topic}:{self.idempotency_key}"

    def with_retry(self, retry: RetryState) -> EventEnvelope:
        """Return the same envelope carrying updated delivery bookkeeping."""
        return self.model_copy(update={"retry": retry})

    def caused(
        self,
        *,
        event_id: str,
        topic: Topic,
        occurred_at: datetime,
        producer: str,
        producer_version: str,
        ids: dict[str, EventIdValue],
        idempotency_key: str,
    ) -> EventEnvelope:
        """Build a downstream envelope that inherits this one's causal chain."""
        return EventEnvelope(
            event_id=event_id,
            topic=topic,
            occurred_at=occurred_at,
            producer=producer,
            producer_version=producer_version,
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            ids=ids,
            idempotency_key=idempotency_key,
        )
