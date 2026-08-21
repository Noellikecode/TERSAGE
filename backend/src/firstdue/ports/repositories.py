"""Repository protocols.

Four rules run through all of them:

* **Append-only means append-only.** There is no ``update`` or ``delete`` on the
  fact, timeline, or incident-log repositories. The protocol does not offer one.
* **Profiles use optimistic concurrency.** :meth:`ProfileRepository.save`
  requires the version the caller read, and raises
  :class:`~firstdue.errors.StaleVersionError` (HTTP 409) otherwise.
* **Duplicate work is prevented durably.** :class:`IdempotencyRepository` is the
  memory that turns an idempotency key into an exactly-once effect, and
  :class:`LockRepository` is what stops two instances polling the same district.
* **Every implementation behaves identically.** The in-memory and Firestore
  adapters are held to one shared contract suite; a difference between them is a
  bug in one of them, not a property of the backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from firstdue.domain.conflicts import Conflict, ConflictResolution
from firstdue.domain.facts import StructuralFact
from firstdue.domain.idempotency import IdempotencyClaim, IdempotencyRecord
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.domain.incidents import Incident
from firstdue.domain.locks import LockLease
from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.domain.profiles import BuildingProfile, ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.domain.runs import AgentRunRecord, CompensationRecord, RunCheckpoint
from firstdue.domain.work import (
    ApprovalRequest,
    ReferralRecord,
    SurveyQueueEntry,
    SurveyRecord,
    WriteAction,
    WriteReceipt,
)


@runtime_checkable
class ProfileRepository(Protocol):
    async def get(self, address_id: str) -> BuildingProfile | None: ...

    async def list_by_district(self, district_id: str) -> Sequence[BuildingProfile]: ...

    async def save(self, profile: BuildingProfile, *, expected_version: int) -> BuildingProfile:
        """Persist a profile.

        Raises:
            StaleVersionError: when ``expected_version`` is not the stored
                version. The API renders this as HTTP 409.
        """
        ...

    async def create(self, profile: BuildingProfile) -> BuildingProfile: ...


@runtime_checkable
class SnapshotRepository(Protocol):
    """Frozen profile reads, addressed by their stable id.

    ``put`` is idempotent by design: storing a snapshot whose id already exists
    returns the stored one untouched. Two incidents opened against the same
    profile version therefore brief from byte-identical state, and a replay
    reproduces the moment rather than re-reading it.
    """

    async def put(self, snapshot: ProfileSnapshot) -> ProfileSnapshot: ...

    async def get(self, snapshot_id: str) -> ProfileSnapshot | None: ...

    async def list_for_address(self, address_id: str) -> Sequence[ProfileSnapshot]: ...


@runtime_checkable
class FactRepository(Protocol):
    """Append-only. Facts are never edited; corrections are new facts."""

    async def append(self, fact: StructuralFact) -> StructuralFact: ...

    async def get(self, fact_id: str) -> StructuralFact | None: ...

    async def list_for_address(self, address_id: str) -> Sequence[StructuralFact]: ...


@runtime_checkable
class ConflictRepository(Protocol):
    async def add(self, conflict: Conflict) -> Conflict: ...

    async def get(self, conflict_id: str) -> Conflict | None: ...

    async def list_for_address(self, address_id: str) -> Sequence[Conflict]: ...

    async def list_open(self, district_id: str | None = None) -> Sequence[Conflict]: ...

    async def resolve(self, conflict_id: str, resolution: ConflictResolution) -> Conflict: ...


@runtime_checkable
class IncidentRepository(Protocol):
    async def create(self, incident: Incident) -> Incident: ...

    async def get(self, incident_id: str) -> Incident | None: ...

    async def save(self, incident: Incident) -> Incident: ...

    async def list_open(self) -> Sequence[Incident]: ...


@runtime_checkable
class IncidentLogRepository(Protocol):
    """Append-only, gapless, sealable. The record of what the commander saw."""

    async def append(self, entry: IncidentLogEntry) -> IncidentLogEntry: ...

    async def get_log(self, incident_id: str) -> AppendOnlyLog: ...

    async def next_sequence(self, incident_id: str) -> int: ...

    async def seal(self, incident_id: str, *, at: datetime) -> AppendOnlyLog: ...

    async def mark_written_to_rms(
        self, incident_id: str, entry_id: str, *, at: datetime
    ) -> IncidentLogEntry: ...

    async def list_unflushed(self) -> Sequence[IncidentLogEntry]: ...


@runtime_checkable
class RegistryRepository(Protocol):
    async def publish(self, descriptor: AgentDescriptor) -> AgentDescriptor: ...

    async def get_agent(self, agent_id: str, version: str) -> AgentDescriptor | None: ...

    async def list_agents(
        self, *, publisher_department: str | None = None
    ) -> Sequence[AgentDescriptor]: ...

    async def subscribe(self, subscription: Subscription) -> Subscription: ...

    async def list_subscriptions(
        self, *, subscriber_department: str | None = None
    ) -> Sequence[Subscription]: ...

    async def resolve_pinned(
        self, subscriber_department: str, agent_id: str
    ) -> AgentDescriptor | None:
        """Return the exact pinned version a department subscribes to."""
        ...


@runtime_checkable
class GrantRepository(Protocol):
    async def store_incident_grant(self, grant: IncidentGrant) -> IncidentGrant: ...

    async def get_incident_grant(self, grant_id: str) -> IncidentGrant | None: ...

    async def revoke_incident_grant(self, grant_id: str, *, at: datetime) -> IncidentGrant: ...

    async def store_standing_grant(self, grant: StandingGrant) -> StandingGrant: ...

    async def get_standing_grant(self, agent_id: str) -> StandingGrant | None: ...


@runtime_checkable
class QueueRepository(Protocol):
    async def replace_district_queue(
        self, district_id: str, entries: Sequence[SurveyQueueEntry]
    ) -> Sequence[SurveyQueueEntry]:
        """Replace a district's ranking wholesale -- ranking is recomputed, not patched."""
        ...

    async def list_for_district(self, district_id: str) -> Sequence[SurveyQueueEntry]: ...

    async def get(self, entry_id: str) -> SurveyQueueEntry | None: ...

    async def save(self, entry: SurveyQueueEntry) -> SurveyQueueEntry: ...


@runtime_checkable
class ReferralRepository(Protocol):
    async def add(self, referral: ReferralRecord) -> ReferralRecord: ...

    async def get(self, referral_id: str) -> ReferralRecord | None: ...

    async def save(self, referral: ReferralRecord) -> ReferralRecord: ...

    async def list_open(self) -> Sequence[ReferralRecord]: ...


@runtime_checkable
class ApprovalRepository(Protocol):
    async def stage(self, approval: ApprovalRequest) -> ApprovalRequest: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def save(self, approval: ApprovalRequest) -> ApprovalRequest: ...

    async def list_for_incident(self, incident_id: str) -> Sequence[ApprovalRequest]: ...


@runtime_checkable
class SurveyRepository(Protocol):
    async def add(self, survey: SurveyRecord) -> SurveyRecord: ...

    async def get(self, survey_id: str) -> SurveyRecord | None: ...

    async def list_for_address(self, address_id: str) -> Sequence[SurveyRecord]: ...


@runtime_checkable
class WriteActionRepository(Protocol):
    """Records every external write and its receipt, for audit and rollback."""

    async def record(self, action: WriteAction) -> WriteAction: ...

    async def get(self, action_id: str) -> WriteAction | None: ...

    async def find_by_idempotency_key(self, target: str, key: str) -> WriteAction | None: ...

    async def save_receipt(self, receipt: WriteReceipt) -> WriteReceipt: ...

    async def get_receipt(self, action_id: str) -> WriteReceipt | None: ...


@runtime_checkable
class LockRepository(Protocol):
    """Distributed processing locks with expiry and fencing.

    Acquisition returns ``None`` rather than raising when the lock is held: one
    instance losing a race is ordinary operation, not an error worth an
    exception and a stack trace.
    """

    async def acquire(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None: ...

    async def renew(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None:
        """Extend a lease this owner still holds. ``None`` if it was lost."""
        ...

    async def release(self, lock_id: str, *, owner: str) -> bool:
        """Release only if still the holder. Returns whether it was released."""
        ...

    async def get(self, lock_id: str) -> LockLease | None: ...


@runtime_checkable
class IdempotencyRepository(Protocol):
    """Durable memory of which keys have already been acted on."""

    async def claim(self, record: IdempotencyRecord) -> IdempotencyClaim:
        """Claim a key within a scope.

        Returns ``FRESH`` when the caller should execute, ``REPLAY`` when the
        same request already completed, and ``IN_PROGRESS`` when another worker
        holds an unexpired claim.

        Raises:
            IdempotencyMismatchError: when the key was used for a different
                request body. Guessing which body was meant is not an option.
        """
        ...

    async def complete(
        self, scope: str, key: str, *, at: datetime, result_ref: str | None = None
    ) -> IdempotencyRecord: ...

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None: ...


@runtime_checkable
class AgentRunRepository(Protocol):
    """Agent runs, their checkpoints, and their terminal states."""

    async def start(self, run: AgentRunRecord) -> AgentRunRecord: ...

    async def get(self, run_id: str) -> AgentRunRecord | None: ...

    async def save(self, run: AgentRunRecord) -> AgentRunRecord:
        """Persist a transition.

        Raises:
            ValidationError: when the stored run is already terminal. A terminal
                run that could be overwritten would let a late retry erase the
                record of why the first attempt failed.
        """
        ...

    async def checkpoint(self, run_id: str, checkpoint: RunCheckpoint) -> AgentRunRecord:
        """Append one checkpoint. Checkpoints are never rewritten."""
        ...

    async def list_resumable(self, *, agent_id: str | None = None) -> Sequence[AgentRunRecord]:
        """Runs that stopped short and left a position worth resuming from."""
        ...

    async def find_by_idempotency_key(self, key: str) -> AgentRunRecord | None: ...


@runtime_checkable
class CompensationRepository(Protocol):
    """Recorded obligations to undo executed writes."""

    async def record(self, compensation: CompensationRecord) -> CompensationRecord: ...

    async def get(self, compensation_id: str) -> CompensationRecord | None: ...

    async def save(self, compensation: CompensationRecord) -> CompensationRecord: ...

    async def list_outstanding(self) -> Sequence[CompensationRecord]:
        """Everything recorded but not yet executed. Never silently forgotten."""
        ...

    async def list_for_action(self, action_id: str) -> Sequence[CompensationRecord]: ...
