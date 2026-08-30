"""Persisting what the deterministic engines derived.

This is the seam between :mod:`firstdue.domain.materialize`, which is pure, and
the durable stores, which are not. Four things happen here and nowhere else:

* **A lock is held.** Two instances materializing the same address would both
  read version 7, both detect the same conflict, and one would lose the write
  and retry -- doing the work twice for one result. The lock makes that the
  uncommon path instead of the normal one.
* **Optimistic concurrency still applies.** The lock is an optimisation, not a
  guarantee: a lease can expire while its holder is paused. The version check is
  the guarantee, and losing it is *not* an error -- the other writer's pass
  computed the same thing, because the engine is deterministic.
* **Conflicts are persisted exactly once.** Conflict ids are derived, so a
  second pass over unchanged facts produces ids that are already stored and
  writes nothing.
* **New conflicts are announced.** One ``conflict.detected`` envelope per new
  conflict, carrying identifiers only, with a derived idempotency key so a
  republished event cannot double-notify.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflict_engine import RuleRegistry
from firstdue.domain.conflicts import Conflict
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.locks import DEFAULT_LEASE
from firstdue.domain.materialize import materialize
from firstdue.errors import AppendOnlyViolationError, NotFoundError, StaleVersionError
from firstdue.observability.logging import get_logger
from firstdue.ports.bus import EventBus
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import (
    ConflictRepository,
    LockRepository,
    ProfileRepository,
)

logger = get_logger(__name__)

#: The agent this service acts as. Recorded on every timeline event it writes.
ACTOR = "structure-watch"


class MaterializationOutcome(BaseModel):
    """What one materialization pass did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    #: False when another instance held the lock. Not an error: it is doing this.
    ran: bool = False
    changed: bool = False
    profile_version: int = Field(default=0, ge=0)
    new_conflict_ids: tuple[str, ...] = ()
    published_event_ids: tuple[str, ...] = ()
    #: Set when the version moved under us. The other writer computed the same
    #: result, because the engine is deterministic -- so this is informational.
    contended: bool = False


#: How many times a derivation may be re-attempted after losing a version
#: check. Small: each attempt re-reads the winner's profile and re-derives from
#: it, so the work left shrinks every round, and a profile contended more than
#: this is one under sustained write pressure the slow loop is not designed for.
MAX_MATERIALIZE_ATTEMPTS: Final[int] = 4

#: How long to wait for another worker's derivation of the same address, and
#: how many times. One derivation is milliseconds of compute plus a write, so
#: this is generous; it exists so a concurrent agent queues behind a peer
#: instead of dropping its work.
LOCK_WAIT_SECONDS: Final[float] = 0.25
LOCK_WAIT_ATTEMPTS: Final[int] = 12


class ProfileMaterializer:
    """Runs the deterministic engines over one profile and stores the result."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        conflicts: ConflictRepository,
        locks: LockRepository,
        clock: Clock,
        ids: IdGenerator,
        bus: EventBus | None = None,
        registry: RuleRegistry | None = None,
        agent_version: str = "1.0.0",
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._profiles = profiles
        self._conflicts = conflicts
        self._locks = locks
        self._clock = clock
        self._ids = ids
        self._bus = bus
        self._registry = registry
        self._agent_version = agent_version
        self._lease = lease

    def lock_id(self, address_id: str) -> str:
        return f"materialize:{address_id}"

    async def run(
        self, address_id: str, *, owner: str, correlation_id: str, causation_id: str | None = None
    ) -> MaterializationOutcome:
        """Materialize one address.

        Args:
            address_id: the profile to work on.
            owner: this worker's identity, for the lock.
            correlation_id: threads the causal chain through the audit log.
            causation_id: the event that triggered this, if any.

        Raises:
            NotFoundError: when no profile exists for the address. A materializer
                does not create profiles; something has to have written a fact.
        """
        now = self._clock.now()
        lease = await self._locks.acquire(
            self.lock_id(address_id), owner=owner, now=now, lease=self._lease
        )
        if lease is None:
            # Waited for, not skipped.
            #
            # "Its pass will produce the same result ours would have" holds for
            # a duplicate pass and fails for a concurrent *different* agent:
            # `records-watcher` and `hazard-watcher` derive from disjoint
            # canonical keys on the same profile, so skipping threw away
            # whichever one arrived second. That was safe only while the fleet
            # ran strictly serially and nothing ever contended.
            #
            # The lock is held for one derivation, so the wait is short and
            # bounded. A lock still held after that is a worker that died
            # holding it, and its lease will expire; giving up here is right
            # then, and the caller records an unfinished pass rather than a
            # silent nothing.
            for _ in range(LOCK_WAIT_ATTEMPTS):
                await asyncio.sleep(LOCK_WAIT_SECONDS)
                lease = await self._locks.acquire(
                    self.lock_id(address_id),
                    owner=owner,
                    now=self._clock.now(),
                    lease=self._lease,
                )
                if lease is not None:
                    break
        if lease is None:
            logger.info("materialize_skipped_locked", extra={"address_id": address_id})
            return MaterializationOutcome(address_id=address_id, ran=False)

        try:
            return await self._materialize(
                address_id, correlation_id=correlation_id, causation_id=causation_id
            )
        finally:
            await self._locks.release(self.lock_id(address_id), owner=owner)

    async def _materialize(
        self, address_id: str, *, correlation_id: str, causation_id: str | None
    ) -> MaterializationOutcome:
        """Derive and persist, re-deriving if somebody else got there first.

        The retry is what makes concurrent writers safe. Losing the version
        check used to end the attempt: the outcome came back ``contended`` and
        the derivation was dropped, on the stated reasoning that "another
        writer got there first with the same deterministic result". That is
        true of two *duplicate* passes and false of two different agents --
        `records-watcher` and `hazard-watcher` write disjoint canonical keys to
        the same profile, so whoever lost the race had their facts silently
        thrown away rather than merged.

        Re-reading and re-deriving fixes it, and cannot loop for long:
        `materialize` is a pure function of the profile it is handed, so the
        second attempt starts from the winner's version and derives what is
        still missing. A profile nobody else is touching takes the first
        attempt and pays nothing.
        """
        for attempt in range(MAX_MATERIALIZE_ATTEMPTS):
            outcome = await self._materialize_once(
                address_id, correlation_id=correlation_id, causation_id=causation_id
            )
            if not outcome.contended:
                return outcome
            logger.info(
                "materialize_retry",
                extra={"address_id": address_id, "attempt": attempt + 1},
            )
        logger.warning("materialize_gave_up", extra={"address_id": address_id})
        return outcome

    async def _materialize_once(
        self, address_id: str, *, correlation_id: str, causation_id: str | None
    ) -> MaterializationOutcome:
        profile = await self._profiles.get(address_id)
        if profile is None:
            raise NotFoundError("profile not found", details={"address_id": address_id})

        result = materialize(
            profile,
            now=self._clock.now(),
            actor=ACTOR,
            actor_version=self._agent_version,
            registry=self._registry,
        )

        if not result.changed:
            # The expected outcome of a redelivered event.
            return MaterializationOutcome(
                address_id=address_id,
                ran=True,
                changed=False,
                profile_version=profile.profile_version,
            )

        for conflict in result.new_conflicts:
            await self._persist_conflict(conflict)

        try:
            stored = await self._profiles.save(
                result.profile, expected_version=profile.profile_version
            )
        except StaleVersionError:
            # Another writer got there first with the same deterministic result.
            logger.info("materialize_contended", extra={"address_id": address_id})
            current = await self._profiles.get(address_id)
            return MaterializationOutcome(
                address_id=address_id,
                ran=True,
                changed=False,
                contended=True,
                profile_version=current.profile_version if current else 0,
                new_conflict_ids=tuple(c.conflict_id for c in result.new_conflicts),
            )

        published = await self._announce(
            result.new_conflicts, correlation_id=correlation_id, causation_id=causation_id
        )

        logger.info(
            "materialized",
            extra={
                "address_id": address_id,
                "profile_version": stored.profile_version,
                "new_conflicts": len(result.new_conflicts),
            },
        )
        return MaterializationOutcome(
            address_id=address_id,
            ran=True,
            changed=True,
            profile_version=stored.profile_version,
            new_conflict_ids=tuple(c.conflict_id for c in result.new_conflicts),
            published_event_ids=published,
        )

    async def _persist_conflict(self, conflict: Conflict) -> None:
        """Store one conflict. Already stored is success, not a failure.

        Conflict ids are derived, so "already stored" means the same rule fired
        on the same facts -- which is the same finding, not a second one.
        """
        try:
            await self._conflicts.add(conflict)
        except AppendOnlyViolationError:
            logger.debug("conflict_already_recorded", extra={"conflict_id": conflict.conflict_id})

    async def _announce(
        self,
        conflicts: tuple[Conflict, ...],
        *,
        correlation_id: str,
        causation_id: str | None,
    ) -> tuple[str, ...]:
        """Publish one identifier-only envelope per new conflict."""
        if self._bus is None or not conflicts:
            return ()

        published: list[str] = []
        for conflict in conflicts:
            envelope = EventEnvelope(
                event_id=self._ids.new_id("evt"),
                topic=Topic.CONFLICT_DETECTED,
                occurred_at=self._clock.now(),
                producer=ACTOR,
                producer_version=self._agent_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
                ids={
                    "address_id": conflict.address_id,
                    "conflict_id": conflict.conflict_id,
                    "rule_id": conflict.rule_id,
                    "fact_ids": conflict.fact_ids,
                },
                # Derived from the conflict, so a republish cannot double-notify.
                idempotency_key=self._ids.idempotency_key(
                    "conflict.detected", conflict.conflict_id
                ),
            )
            await self._bus.publish(envelope)
            published.append(envelope.event_id)
        return tuple(published)
