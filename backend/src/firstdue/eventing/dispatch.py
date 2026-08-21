"""One delivery policy, two transports.

Delivering an event to a consumer involves five decisions, and getting any of
them wrong in one transport but not the other would make fake mode a rehearsal
of a system that does not exist. So they are made here, once:

1. **Is the envelope one we understand?** A schema version from the future is a
   poison message. Guessing at a field a newer producer considered required is
   how a consumer acts on half a message.
2. **Have we already done this?** At-least-once delivery becomes exactly-once
   effect by consulting a dedupe store keyed per subscriber -- deduping globally
   would starve the second consumer of a topic.
3. **Is the consumer's dependency alive?** A circuit breaker per subscriber, so
   one sick consumer stops being fed instead of burning the whole queue's
   retries.
4. **Is this failure worth retrying?** :mod:`firstdue.reliability.retry`
   classifies it. Transient and contended failures retry with derived jittered
   backoff; poison and permanent ones dead-letter immediately, because retrying
   a message that is wrong is how a queue stops moving.
5. **What happens when we give up?** A dead letter with the attempt count and a
   stable error code. Never a silent drop.

Nothing here reads a clock or a random number generator: time arrives from the
:class:`~firstdue.ports.clock.Clock`, and backoff jitter is derived from the
event id, so a replay reproduces the same schedule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.events import EventEnvelope, RetryState, Topic
from firstdue.domain.idempotency import (
    DEFAULT_CLAIM_TTL,
    IdempotencyRecord,
    request_hash,
)
from firstdue.errors import IdempotencyMismatchError
from firstdue.eventing.deadletter import DeadLetterRecord, InMemoryDeadLetterStore
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.repositories import IdempotencyRepository
from firstdue.reliability.breaker import CircuitBreaker
from firstdue.reliability.retry import (
    DEFAULT_POLICY,
    FailureClass,
    RetryPolicy,
    backoff_ms,
    classify,
    error_code_of,
    is_retryable,
)

logger = get_logger(__name__)

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class DeliveryStatus(StrEnum):
    """What became of one (envelope, subscriber) pair."""

    DELIVERED = "DELIVERED"
    #: The subscriber had already acted on this key. The correct no-op.
    DEDUPED = "DEDUPED"
    #: Another worker holds the claim; this delivery steps aside.
    IN_PROGRESS = "IN_PROGRESS"
    #: Retries exhausted, or the failure was not worth retrying.
    DEAD_LETTERED = "DEAD_LETTERED"
    #: The subscriber's breaker is open; the envelope was parked, not delivered.
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class DeliveryOutcome(BaseModel):
    """The record of one delivery attempt sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DeliveryStatus
    subscriber: str = Field(min_length=1, max_length=120)
    event_id: str = Field(min_length=1, max_length=120)
    attempts: int = Field(ge=0)
    failure_class: FailureClass | None = None
    error_code: str | None = Field(default=None, max_length=80)
    #: Every backoff the dispatcher imposed, in order. Asserted by tests.
    backoffs_ms: tuple[int, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in (DeliveryStatus.DELIVERED, DeliveryStatus.DEDUPED)

    @property
    def should_ack(self) -> bool:
        """Whether the transport should acknowledge the message.

        A dead letter is acked: it has been recorded, and redelivering it would
        only produce the same dead letter again. Only ``CIRCUIT_OPEN`` and
        ``IN_PROGRESS`` are worth having the broker hand back later.
        """
        return self.status in (
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DEDUPED,
            DeliveryStatus.DEAD_LETTERED,
        )


@dataclass(slots=True)
class Subscription:
    """One consumer registered against one topic."""

    topic: Topic
    subscriber: str
    handler: EventHandler
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self) -> None:
        self.breaker = CircuitBreaker(f"subscriber:{self.subscriber}")


# ------------------------------------------------------------------- dedupe


async def route(
    dispatcher: EventDispatcher,
    subscriptions: Sequence[Subscription],
    envelope: EventEnvelope,
    *,
    subscriber: str | None = None,
) -> tuple[DeliveryOutcome, ...]:
    """Deliver one envelope to the local subscribers that should receive it.

    Pub/Sub pushes one subscription at a time, so the endpoint names which
    consumer the delivery is for. ``subscriber=None`` fans out to every local
    subscriber of the topic, which is what an in-process publish and a replay
    harness both want.
    """
    selected = [
        sub
        for sub in subscriptions
        if sub.topic is envelope.topic and (subscriber is None or sub.subscriber == subscriber)
    ]
    return tuple([await dispatcher.deliver(sub, envelope) for sub in selected])


@runtime_checkable
class DedupeStore(Protocol):
    """Remembers which dedupe keys a subscriber has already acted on."""

    async def begin(self, key: str, *, now: datetime, correlation_id: str) -> bool:
        """Claim the key. False means the caller must not run the handler."""
        ...

    async def commit(self, key: str, *, now: datetime, result_ref: str | None = None) -> None:
        """Mark the effect as complete."""
        ...

    async def abandon(self, key: str) -> None:
        """Release a claim whose effect did not happen, so a retry may run it."""
        ...


class MemoryDedupeStore:
    """Process-local dedupe. Correct for the in-process bus, and nothing more."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._committed: set[str] = set()

    async def begin(self, key: str, *, now: datetime, correlation_id: str) -> bool:
        if key in self._committed or key in self._claimed:
            return False
        self._claimed.add(key)
        return True

    async def commit(self, key: str, *, now: datetime, result_ref: str | None = None) -> None:
        self._claimed.discard(key)
        self._committed.add(key)

    async def abandon(self, key: str) -> None:
        self._claimed.discard(key)

    def clear(self) -> None:
        self._claimed.clear()
        self._committed.clear()


class RepositoryDedupeStore:
    """Durable dedupe backed by :class:`IdempotencyRepository`.

    This is what makes the HTTP push endpoint safe: Pub/Sub redelivers across
    process restarts, and a dedupe set that lived in memory would forget
    everything the moment Cloud Run replaced the instance.
    """

    def __init__(self, repository: IdempotencyRepository, *, scope_prefix: str = "event") -> None:
        self._repository = repository
        self._prefix = scope_prefix

    def _scope(self, key: str) -> str:
        # The dedupe key already encodes subscriber and topic; the scope keeps
        # event dedupe from colliding with external-write dedupe.
        return f"{self._prefix}:{key}"

    async def begin(self, key: str, *, now: datetime, correlation_id: str) -> bool:
        record = IdempotencyRecord(
            key=key if len(key) >= 8 else key.ljust(8, "-"),
            scope=self._scope(key),
            request_hash=request_hash({"dedupe_key": key}),
            claimed_at=now,
            claim_expires_at=now + DEFAULT_CLAIM_TTL,
            correlation_id=correlation_id,
        )
        try:
            claim = await self._repository.claim(record)
        except IdempotencyMismatchError:
            # The same key was used for a different request. Refusing to run is
            # the only safe answer; the caller records it as a poison message.
            return False
        return claim.should_execute

    async def commit(self, key: str, *, now: datetime, result_ref: str | None = None) -> None:
        await self._repository.complete(
            self._scope(key),
            key if len(key) >= 8 else key.ljust(8, "-"),
            at=now,
            result_ref=result_ref,
        )

    async def abandon(self, key: str) -> None:
        # The claim expires on its own. Deleting it here would let a concurrent
        # worker start the same effect while this one is still unwinding.
        return None


# -------------------------------------------------------------------- sleep


@runtime_checkable
class Sleeper(Protocol):
    async def __call__(self, seconds: float) -> None: ...


class VirtualSleeper:
    """Records the delays it was asked for without waiting.

    Fake mode is deterministic, and a deterministic system does not spend four
    real seconds proving it computed four seconds. The Pub/Sub path does its
    waiting in the broker's redelivery, not in this process, so nothing on the
    live path depends on a real sleep either.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)

    @property
    def total_seconds(self) -> float:
        return sum(self.slept)


class RealSleeper:
    """Actually waits. Used where a process must hold the backoff itself."""

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --------------------------------------------------------------- dispatcher


class EventDispatcher:
    """Delivers one envelope to one subscriber under the delivery policy."""

    def __init__(
        self,
        *,
        clock: Clock,
        policy: RetryPolicy = DEFAULT_POLICY,
        dead_letters: InMemoryDeadLetterStore | None = None,
        dedupe: DedupeStore | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._clock = clock
        self._policy = policy
        self._dead_letters = dead_letters or InMemoryDeadLetterStore()
        self._dedupe = dedupe or MemoryDedupeStore()
        self._sleeper: Sleeper = sleeper or VirtualSleeper()

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    @property
    def dead_letters(self) -> InMemoryDeadLetterStore:
        return self._dead_letters

    @property
    def sleeper(self) -> Sleeper:
        return self._sleeper

    async def deliver(self, subscription: Subscription, envelope: EventEnvelope) -> DeliveryOutcome:
        """Run the full delivery policy for one (envelope, subscriber) pair."""
        dedupe_key = envelope.dedupe_key(subscription.subscriber)

        if not envelope.is_supported_schema:
            return await self._dead_letter(
                subscription,
                envelope,
                attempts=0,
                failure=FailureClass.POISON,
                reason="UNSUPPORTED_SCHEMA_VERSION",
                backoffs=(),
            )

        now = self._clock.now()
        if not subscription.breaker.allow(now):
            # Parked rather than delivered. The record exists so an operator can
            # see what was not processed while the consumer was shut off.
            logger.warning(
                "delivery_circuit_open",
                extra={"subscriber": subscription.subscriber, "topic": str(envelope.topic)},
            )
            return DeliveryOutcome(
                status=DeliveryStatus.CIRCUIT_OPEN,
                subscriber=subscription.subscriber,
                event_id=envelope.event_id,
                attempts=0,
                error_code="CIRCUIT_OPEN",
            )

        if not await self._dedupe.begin(
            dedupe_key, now=now, correlation_id=envelope.correlation_id
        ):
            return DeliveryOutcome(
                status=DeliveryStatus.DEDUPED,
                subscriber=subscription.subscriber,
                event_id=envelope.event_id,
                attempts=0,
            )

        return await self._attempt_loop(subscription, envelope, dedupe_key)

    async def _attempt_loop(
        self, subscription: Subscription, envelope: EventEnvelope, dedupe_key: str
    ) -> DeliveryOutcome:
        current = self._normalized(envelope)
        backoffs: list[int] = []
        first_attempted_at = self._clock.now()

        while True:
            attempt = current.retry.attempt
            try:
                await subscription.handler(current)
            except Exception as exc:  # classification, not suppression: see classify()
                failure = classify(exc)
                code = error_code_of(exc)
                logger.warning(
                    "delivery_failed",
                    extra={
                        "subscriber": subscription.subscriber,
                        "topic": str(current.topic),
                        "attempt": attempt,
                        "failure_class": str(failure),
                        "error_code": code,
                    },
                )
                if failure is FailureClass.TRANSIENT:
                    # Only genuine dependency failures move the breaker.
                    # Contention is other writers working; poison is the
                    # message's fault; a refusal is the consumer working.
                    subscription.breaker.record_failure(self._clock.now(), error_code=code)

                if not is_retryable(failure) or current.retry.is_final:
                    await self._dedupe.abandon(dedupe_key)
                    return await self._dead_letter(
                        subscription,
                        current,
                        attempts=attempt,
                        failure=failure,
                        reason=code,
                        backoffs=tuple(backoffs),
                    )

                delay_ms = backoff_ms(attempt + 1, policy=self._policy, seed=current.event_id)
                backoffs.append(delay_ms)
                await self._sleeper(delay_ms / 1000.0)
                current = current.with_retry(
                    current.retry.next_attempt(
                        error_code=code,
                        backoff_ms=delay_ms,
                        first_attempted_at=first_attempted_at,
                    )
                )
                continue

            subscription.breaker.record_success(self._clock.now())
            await self._dedupe.commit(dedupe_key, now=self._clock.now())
            return DeliveryOutcome(
                status=DeliveryStatus.DELIVERED,
                subscriber=subscription.subscriber,
                event_id=current.event_id,
                attempts=attempt,
                backoffs_ms=tuple(backoffs),
            )

    def _normalized(self, envelope: EventEnvelope) -> EventEnvelope:
        """Align a first delivery with this consumer's retry budget.

        A redelivered envelope keeps the counter it arrived with -- the broker
        and this process must not disagree about which attempt this is.
        """
        if not envelope.retry.is_first:
            return envelope
        if envelope.retry.max_attempts == self._policy.max_attempts:
            return envelope
        return envelope.with_retry(RetryState(attempt=1, max_attempts=self._policy.max_attempts))

    async def _dead_letter(
        self,
        subscription: Subscription,
        envelope: EventEnvelope,
        *,
        attempts: int,
        failure: FailureClass,
        reason: str,
        backoffs: tuple[int, ...],
    ) -> DeliveryOutcome:
        record = DeadLetterRecord(
            envelope=envelope,
            subscriber=subscription.subscriber,
            attempts=max(1, attempts),
            reason=reason,
            failure_class=failure,
            dead_lettered_at=self._clock.now(),
        )
        await self._dead_letters.add(record)
        logger.error(
            "dead_lettered",
            extra={
                "subscriber": subscription.subscriber,
                "topic": str(envelope.topic),
                "attempts": record.attempts,
                "failure_class": str(failure),
                "error_code": reason,
            },
        )
        return DeliveryOutcome(
            status=DeliveryStatus.DEAD_LETTERED,
            subscriber=subscription.subscriber,
            event_id=envelope.event_id,
            attempts=record.attempts,
            failure_class=failure,
            error_code=reason,
            backoffs_ms=backoffs,
        )
