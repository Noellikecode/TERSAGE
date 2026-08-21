"""In-memory repositories with real semantics.

Real means: optimistic concurrency that raises 409, append-only sequences that
refuse gaps and rewrites, idempotency dedupe that returns the original receipt,
leased and fenced locks, and a mutex around every mutation so concurrent writers
behave the way Firestore transactions do.

These are not stubs standing in for the Firestore adapters -- both are held to
the same contract suite in ``tests/contract``, and a behavioural difference
between them is a bug in one of them rather than a property of the backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta

from firstdue.domain.conflicts import Conflict, ConflictResolution
from firstdue.domain.enums import ReferralStatus
from firstdue.domain.facts import StructuralFact
from firstdue.domain.idempotency import (
    IdempotencyClaim,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyStatus,
    storage_id_for,
)
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.domain.incidents import Incident
from firstdue.domain.locks import LockLease
from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.domain.profiles import BuildingProfile, ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.domain.runs import (
    AgentRunRecord,
    CompensationRecord,
    CompensationStatus,
    RunCheckpoint,
)
from firstdue.domain.work import (
    ApprovalRequest,
    ReferralRecord,
    SurveyQueueEntry,
    SurveyRecord,
    WriteAction,
    WriteReceipt,
)
from firstdue.errors import (
    AppendOnlyViolationError,
    IdempotencyMismatchError,
    NotFoundError,
    StaleVersionError,
    ValidationError,
)


class InMemoryProfileRepository:
    """Profiles with optimistic concurrency on ``profile_version``."""

    def __init__(self) -> None:
        self._profiles: dict[str, BuildingProfile] = {}
        self._lock = asyncio.Lock()

    async def get(self, address_id: str) -> BuildingProfile | None:
        return self._profiles.get(address_id)

    async def list_by_district(self, district_id: str) -> Sequence[BuildingProfile]:
        return [p for p in self._profiles.values() if p.district_id == district_id]

    async def create(self, profile: BuildingProfile) -> BuildingProfile:
        async with self._lock:
            if profile.address_id in self._profiles:
                raise ValidationError(
                    "profile already exists", details={"address_id": profile.address_id}
                )
            self._profiles[profile.address_id] = profile
            return profile

    async def save(self, profile: BuildingProfile, *, expected_version: int) -> BuildingProfile:
        async with self._lock:
            stored = self._profiles.get(profile.address_id)
            if stored is None:
                raise NotFoundError("profile not found", details={"address_id": profile.address_id})
            if stored.profile_version != expected_version:
                raise StaleVersionError(
                    expected=expected_version,
                    actual=stored.profile_version,
                    entity=f"profile {profile.address_id}",
                )
            if profile.profile_version <= stored.profile_version:
                raise StaleVersionError(
                    expected=stored.profile_version + 1,
                    actual=profile.profile_version,
                    entity=f"profile {profile.address_id}",
                )
            if len(profile.timeline) < len(stored.timeline):
                raise AppendOnlyViolationError(
                    "a profile write may not shorten the timeline",
                    details={"address_id": profile.address_id},
                )
            self._profiles[profile.address_id] = profile
            return profile

    def snapshot_state(self) -> dict[str, BuildingProfile]:
        return dict(self._profiles)


class InMemoryFactRepository:
    """Append-only fact store. No update, no delete."""

    def __init__(self) -> None:
        self._by_id: dict[str, StructuralFact] = {}
        self._by_address: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def append(self, fact: StructuralFact) -> StructuralFact:
        async with self._lock:
            if fact.fact_id in self._by_id:
                raise AppendOnlyViolationError(
                    "fact already written; corrections are new facts",
                    details={"fact_id": fact.fact_id},
                )
            self._by_id[fact.fact_id] = fact
            self._by_address.setdefault(fact.address_id, []).append(fact.fact_id)
            return fact

    async def get(self, fact_id: str) -> StructuralFact | None:
        return self._by_id.get(fact_id)

    async def list_for_address(self, address_id: str) -> Sequence[StructuralFact]:
        return [self._by_id[fid] for fid in self._by_address.get(address_id, [])]


class InMemoryConflictRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, Conflict] = {}
        self._lock = asyncio.Lock()

    async def add(self, conflict: Conflict) -> Conflict:
        async with self._lock:
            if conflict.conflict_id in self._by_id:
                raise AppendOnlyViolationError(
                    "conflict already recorded", details={"conflict_id": conflict.conflict_id}
                )
            self._by_id[conflict.conflict_id] = conflict
            return conflict

    async def get(self, conflict_id: str) -> Conflict | None:
        return self._by_id.get(conflict_id)

    async def list_for_address(self, address_id: str) -> Sequence[Conflict]:
        return [c for c in self._by_id.values() if c.address_id == address_id]

    async def list_open(self, district_id: str | None = None) -> Sequence[Conflict]:
        from firstdue.domain.conflicts import ConflictStatus

        return [c for c in self._by_id.values() if c.status is ConflictStatus.OPEN]

    async def resolve(self, conflict_id: str, resolution: ConflictResolution) -> Conflict:
        async with self._lock:
            conflict = self._by_id.get(conflict_id)
            if conflict is None:
                raise NotFoundError("conflict not found", details={"conflict_id": conflict_id})
            resolved = conflict.resolve(resolution)
            self._by_id[conflict_id] = resolved
            return resolved


class InMemoryIncidentRepository:
    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._lock = asyncio.Lock()

    async def create(self, incident: Incident) -> Incident:
        async with self._lock:
            if incident.incident_id in self._incidents:
                raise ValidationError(
                    "incident already exists", details={"incident_id": incident.incident_id}
                )
            self._incidents[incident.incident_id] = incident
            return incident

    async def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    async def save(self, incident: Incident) -> Incident:
        async with self._lock:
            if incident.incident_id not in self._incidents:
                raise NotFoundError(
                    "incident not found", details={"incident_id": incident.incident_id}
                )
            self._incidents[incident.incident_id] = incident
            return incident

    async def list_open(self) -> Sequence[Incident]:
        from firstdue.domain.incidents import IncidentStatus

        return [i for i in self._incidents.values() if i.status is IncidentStatus.OPEN]


class InMemoryIncidentLogRepository:
    """Append-only incident log, gapless and sealable."""

    def __init__(self) -> None:
        self._logs: dict[str, AppendOnlyLog] = {}
        self._lock = asyncio.Lock()

    async def append(self, entry: IncidentLogEntry) -> IncidentLogEntry:
        async with self._lock:
            log = self._logs.get(entry.incident_id) or AppendOnlyLog(incident_id=entry.incident_id)
            updated = log.append(entry)
            self._logs[entry.incident_id] = updated
            return updated.entries[-1]

    async def get_log(self, incident_id: str) -> AppendOnlyLog:
        return self._logs.get(incident_id) or AppendOnlyLog(incident_id=incident_id)

    async def next_sequence(self, incident_id: str) -> int:
        log = self._logs.get(incident_id)
        return log.next_sequence if log is not None else 0

    async def seal(self, incident_id: str, *, at: datetime) -> AppendOnlyLog:
        async with self._lock:
            log = self._logs.get(incident_id) or AppendOnlyLog(incident_id=incident_id)
            sealed = log.seal(at=at)
            self._logs[incident_id] = sealed
            return sealed

    async def mark_written_to_rms(
        self, incident_id: str, entry_id: str, *, at: datetime
    ) -> IncidentLogEntry:
        async with self._lock:
            log = self._logs.get(incident_id)
            if log is None:
                raise NotFoundError("incident log not found", details={"incident_id": incident_id})
            entries = list(log.entries)
            for index, entry in enumerate(entries):
                if entry.entry_id == entry_id:
                    entries[index] = entry.mark_written_to_rms(at=at)
                    self._logs[incident_id] = log.model_copy(update={"entries": tuple(entries)})
                    return entries[index]
            raise NotFoundError("log entry not found", details={"entry_id": entry_id})

    async def list_unflushed(self) -> Sequence[IncidentLogEntry]:
        return [entry for log in self._logs.values() for entry in log.unflushed]


class InMemoryRegistryRepository:
    """Agent catalog with version pinning."""

    def __init__(self) -> None:
        self._agents: dict[tuple[str, str], AgentDescriptor] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._lock = asyncio.Lock()

    async def publish(self, descriptor: AgentDescriptor) -> AgentDescriptor:
        async with self._lock:
            key = (descriptor.agent_id, descriptor.version)
            existing = self._agents.get(key)
            if existing is not None and existing != descriptor:
                raise AppendOnlyViolationError(
                    "a published agent version is immutable; publish a new version instead",
                    details={"agent_ref": descriptor.ref},
                )
            self._agents[key] = descriptor
            return descriptor

    async def get_agent(self, agent_id: str, version: str) -> AgentDescriptor | None:
        return self._agents.get((agent_id, version))

    async def list_agents(
        self, *, publisher_department: str | None = None
    ) -> Sequence[AgentDescriptor]:
        agents = sorted(self._agents.values(), key=lambda d: (d.agent_id, d.version))
        if publisher_department is None:
            return agents
        return [a for a in agents if a.publisher_department == publisher_department]

    async def subscribe(self, subscription: Subscription) -> Subscription:
        async with self._lock:
            target = self._agents.get((subscription.agent_id, subscription.pinned_version))
            if target is None:
                raise NotFoundError(
                    "cannot subscribe to an unpublished agent version",
                    details={"agent_ref": subscription.ref},
                )
            self._subscriptions[subscription.subscription_id] = subscription
            return subscription

    async def list_subscriptions(
        self, *, subscriber_department: str | None = None
    ) -> Sequence[Subscription]:
        subs = sorted(self._subscriptions.values(), key=lambda s: s.subscription_id)
        if subscriber_department is None:
            return subs
        return [s for s in subs if s.subscriber_department == subscriber_department]

    async def resolve_pinned(
        self, subscriber_department: str, agent_id: str
    ) -> AgentDescriptor | None:
        for sub in sorted(self._subscriptions.values(), key=lambda s: s.subscription_id):
            if sub.subscriber_department == subscriber_department and sub.agent_id == agent_id:
                return self._agents.get((sub.agent_id, sub.pinned_version))
        return None


class InMemoryGrantRepository:
    def __init__(self) -> None:
        self._incident_grants: dict[str, IncidentGrant] = {}
        self._standing_grants: dict[str, StandingGrant] = {}
        self._lock = asyncio.Lock()

    async def store_incident_grant(self, grant: IncidentGrant) -> IncidentGrant:
        async with self._lock:
            self._incident_grants[grant.grant_id] = grant
            return grant

    async def get_incident_grant(self, grant_id: str) -> IncidentGrant | None:
        return self._incident_grants.get(grant_id)

    async def revoke_incident_grant(self, grant_id: str, *, at: datetime) -> IncidentGrant:
        async with self._lock:
            grant = self._incident_grants.get(grant_id)
            if grant is None:
                raise NotFoundError("grant not found", details={"grant_id": grant_id})
            revoked = grant.revoke(at=at)
            self._incident_grants[grant_id] = revoked
            return revoked

    async def store_standing_grant(self, grant: StandingGrant) -> StandingGrant:
        async with self._lock:
            self._standing_grants[grant.agent_id] = grant
            return grant

    async def get_standing_grant(self, agent_id: str) -> StandingGrant | None:
        return self._standing_grants.get(agent_id)


class InMemoryQueueRepository:
    def __init__(self) -> None:
        self._by_district: dict[str, list[SurveyQueueEntry]] = {}
        self._by_id: dict[str, SurveyQueueEntry] = {}
        self._lock = asyncio.Lock()

    async def replace_district_queue(
        self, district_id: str, entries: Sequence[SurveyQueueEntry]
    ) -> Sequence[SurveyQueueEntry]:
        async with self._lock:
            ordered = sorted(entries, key=lambda e: e.rank)
            for previous in self._by_district.get(district_id, []):
                self._by_id.pop(previous.entry_id, None)
            self._by_district[district_id] = list(ordered)
            for entry in ordered:
                self._by_id[entry.entry_id] = entry
            return ordered

    async def list_for_district(self, district_id: str) -> Sequence[SurveyQueueEntry]:
        return list(self._by_district.get(district_id, []))

    async def get(self, entry_id: str) -> SurveyQueueEntry | None:
        return self._by_id.get(entry_id)

    async def save(self, entry: SurveyQueueEntry) -> SurveyQueueEntry:
        async with self._lock:
            if entry.entry_id not in self._by_id:
                raise NotFoundError("queue entry not found", details={"entry_id": entry.entry_id})
            self._by_id[entry.entry_id] = entry
            district = self._by_district.get(entry.district_id, [])
            self._by_district[entry.district_id] = [
                entry if e.entry_id == entry.entry_id else e for e in district
            ]
            return entry


class InMemoryReferralRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, ReferralRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, referral: ReferralRecord) -> ReferralRecord:
        async with self._lock:
            if referral.referral_id in self._by_id:
                raise ValidationError(
                    "referral already exists", details={"referral_id": referral.referral_id}
                )
            self._by_id[referral.referral_id] = referral
            return referral

    async def get(self, referral_id: str) -> ReferralRecord | None:
        return self._by_id.get(referral_id)

    async def save(self, referral: ReferralRecord) -> ReferralRecord:
        async with self._lock:
            if referral.referral_id not in self._by_id:
                raise NotFoundError(
                    "referral not found", details={"referral_id": referral.referral_id}
                )
            self._by_id[referral.referral_id] = referral
            return referral

    async def list_open(self) -> Sequence[ReferralRecord]:
        closed = {ReferralStatus.REJECTED, ReferralStatus.WITHDRAWN}
        return [r for r in self._by_id.values() if r.status not in closed]


class InMemoryApprovalRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    async def stage(self, approval: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            if approval.approval_id in self._by_id:
                raise ValidationError(
                    "approval already staged", details={"approval_id": approval.approval_id}
                )
            self._by_id[approval.approval_id] = approval
            return approval

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._by_id.get(approval_id)

    async def save(self, approval: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            if approval.approval_id not in self._by_id:
                raise NotFoundError(
                    "approval not found", details={"approval_id": approval.approval_id}
                )
            self._by_id[approval.approval_id] = approval
            return approval

    async def list_for_incident(self, incident_id: str) -> Sequence[ApprovalRequest]:
        return [a for a in self._by_id.values() if a.incident_id == incident_id]


class InMemorySurveyRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, SurveyRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, survey: SurveyRecord) -> SurveyRecord:
        async with self._lock:
            if survey.survey_id in self._by_id:
                raise AppendOnlyViolationError(
                    "survey already recorded", details={"survey_id": survey.survey_id}
                )
            self._by_id[survey.survey_id] = survey
            return survey

    async def get(self, survey_id: str) -> SurveyRecord | None:
        return self._by_id.get(survey_id)

    async def list_for_address(self, address_id: str) -> Sequence[SurveyRecord]:
        return [s for s in self._by_id.values() if s.address_id == address_id]


class InMemoryWriteActionRepository:
    def __init__(self) -> None:
        self._actions: dict[str, WriteAction] = {}
        self._receipts: dict[str, WriteReceipt] = {}
        self._lock = asyncio.Lock()

    async def record(self, action: WriteAction) -> WriteAction:
        async with self._lock:
            self._actions[action.action_id] = action
            return action

    async def get(self, action_id: str) -> WriteAction | None:
        return self._actions.get(action_id)

    async def find_by_idempotency_key(self, target: str, key: str) -> WriteAction | None:
        """The earliest action recorded under this key.

        Ordered by ``(created_at, action_id)`` rather than by insertion, so the
        answer does not depend on which instance happened to record first --
        the Firestore adapter cannot know insertion order, and the two backends
        must agree.
        """
        matches = sorted(
            (
                action
                for action in self._actions.values()
                if action.target == target and action.idempotency_key == key
            ),
            key=lambda a: (a.created_at, a.action_id),
        )
        return matches[0] if matches else None

    async def save_receipt(self, receipt: WriteReceipt) -> WriteReceipt:
        async with self._lock:
            self._receipts[receipt.action_id] = receipt
            return receipt

    async def get_receipt(self, action_id: str) -> WriteReceipt | None:
        return self._receipts.get(action_id)


class InMemorySnapshotRepository:
    """Frozen profile reads, addressed by their stable id.

    ``put`` is idempotent: a snapshot id already present is returned untouched.
    Two incidents opened against the same profile version therefore brief from
    byte-identical state instead of from two reads taken microseconds apart.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ProfileSnapshot] = {}
        self._by_address: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def put(self, snapshot: ProfileSnapshot) -> ProfileSnapshot:
        async with self._lock:
            existing = self._by_id.get(snapshot.snapshot_id)
            if existing is not None:
                return existing
            self._by_id[snapshot.snapshot_id] = snapshot
            self._by_address.setdefault(snapshot.address_id, []).append(snapshot.snapshot_id)
            return snapshot

    async def get(self, snapshot_id: str) -> ProfileSnapshot | None:
        return self._by_id.get(snapshot_id)

    async def list_for_address(self, address_id: str) -> Sequence[ProfileSnapshot]:
        ids = self._by_address.get(address_id, [])
        return sorted(
            (self._by_id[sid] for sid in ids),
            key=lambda s: (s.profile_version, s.snapshot_id),
        )


class InMemoryLockRepository:
    """Leased, fenced locks.

    The fence counter survives release, so a fence is never reused. A process
    that slept through its lease and wakes up holding fence 4 is detectable
    against a current holder on fence 5 -- which expiry alone cannot do.
    """

    def __init__(self) -> None:
        self._leases: dict[str, LockLease] = {}
        self._fences: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None:
        if lease <= timedelta(0):
            raise ValidationError("a lock lease must be positive", details={"lock_id": lock_id})
        async with self._lock:
            held = self._leases.get(lock_id)
            if held is not None and not held.is_expired(now) and held.owner != owner:
                return None
            fence = self._fences.get(lock_id, 0) + 1
            self._fences[lock_id] = fence
            granted = LockLease(
                lock_id=lock_id,
                owner=owner,
                acquired_at=now,
                expires_at=now + lease,
                fence=fence,
            )
            self._leases[lock_id] = granted
            return granted

    async def renew(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None:
        async with self._lock:
            held = self._leases.get(lock_id)
            if held is None or not held.is_held_by(owner, now=now):
                return None
            renewed = held.renewed(now=now, lease=lease)
            self._leases[lock_id] = renewed
            return renewed

    async def release(self, lock_id: str, *, owner: str) -> bool:
        async with self._lock:
            held = self._leases.get(lock_id)
            if held is None or held.owner != owner:
                return False
            del self._leases[lock_id]
            return True

    async def get(self, lock_id: str) -> LockLease | None:
        return self._leases.get(lock_id)


class InMemoryIdempotencyRepository:
    """Durable memory of which keys have already been acted on."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def claim(self, record: IdempotencyRecord) -> IdempotencyClaim:
        async with self._lock:
            stored = self._records.get(record.storage_id)
            if stored is None:
                self._records[record.storage_id] = record
                return IdempotencyClaim(outcome=IdempotencyOutcome.FRESH, record=record)

            if stored.request_hash != record.request_hash:
                raise IdempotencyMismatchError(
                    "this idempotency key was already used for a different request",
                    details={"scope": record.scope, "key": record.key},
                )
            if stored.status is IdempotencyStatus.COMPLETED:
                return IdempotencyClaim(outcome=IdempotencyOutcome.REPLAY, record=stored)
            if stored.is_claimable(record.claimed_at):
                # The previous worker died holding the claim. Take it over.
                self._records[record.storage_id] = record
                return IdempotencyClaim(outcome=IdempotencyOutcome.FRESH, record=record)
            return IdempotencyClaim(outcome=IdempotencyOutcome.IN_PROGRESS, record=stored)

    async def complete(
        self, scope: str, key: str, *, at: datetime, result_ref: str | None = None
    ) -> IdempotencyRecord:
        async with self._lock:
            storage_id = storage_id_for(scope, key)
            stored = self._records.get(storage_id)
            if stored is None:
                raise NotFoundError(
                    "no idempotency claim to complete", details={"scope": scope, "key": key}
                )
            if stored.status is IdempotencyStatus.COMPLETED:
                # Completing twice keeps the first completion. The effect
                # happened once; the record says when.
                return stored
            completed = stored.completed(at=at, result_ref=result_ref)
            self._records[storage_id] = completed
            return completed

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        return self._records.get(storage_id_for(scope, key))


class InMemoryAgentRunRepository:
    """Agent runs, their checkpoints, and their terminal states."""

    def __init__(self) -> None:
        self._runs: dict[str, AgentRunRecord] = {}
        self._by_key: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self, run: AgentRunRecord) -> AgentRunRecord:
        async with self._lock:
            if run.run_id in self._runs:
                raise ValidationError("agent run already exists", details={"run_id": run.run_id})
            self._runs[run.run_id] = run
            self._by_key.setdefault(run.idempotency_key, run.run_id)
            return run

    async def get(self, run_id: str) -> AgentRunRecord | None:
        return self._runs.get(run_id)

    async def save(self, run: AgentRunRecord) -> AgentRunRecord:
        async with self._lock:
            stored = self._runs.get(run.run_id)
            if stored is None:
                raise NotFoundError("agent run not found", details={"run_id": run.run_id})
            if stored.is_terminal:
                raise ValidationError(
                    "an agent run in a terminal state cannot be overwritten",
                    details={"run_id": run.run_id, "status": str(stored.status)},
                )
            self._runs[run.run_id] = run
            return run

    async def checkpoint(self, run_id: str, checkpoint: RunCheckpoint) -> AgentRunRecord:
        async with self._lock:
            stored = self._runs.get(run_id)
            if stored is None:
                raise NotFoundError("agent run not found", details={"run_id": run_id})
            updated = stored.checkpointed(checkpoint)
            self._runs[run_id] = updated
            return updated

    async def list_resumable(self, *, agent_id: str | None = None) -> Sequence[AgentRunRecord]:
        return sorted(
            (
                run
                for run in self._runs.values()
                if run.is_resumable and (agent_id is None or run.agent_id == agent_id)
            ),
            key=lambda r: r.run_id,
        )

    async def find_by_idempotency_key(self, key: str) -> AgentRunRecord | None:
        run_id = self._by_key.get(key)
        return self._runs.get(run_id) if run_id else None


class InMemoryCompensationRepository:
    """Recorded obligations to undo executed writes."""

    def __init__(self) -> None:
        self._by_id: dict[str, CompensationRecord] = {}
        self._lock = asyncio.Lock()

    async def record(self, compensation: CompensationRecord) -> CompensationRecord:
        async with self._lock:
            existing = self._by_id.get(compensation.compensation_id)
            if existing is not None:
                # Recording the same obligation twice is one obligation.
                return existing
            self._by_id[compensation.compensation_id] = compensation
            return compensation

    async def get(self, compensation_id: str) -> CompensationRecord | None:
        return self._by_id.get(compensation_id)

    async def save(self, compensation: CompensationRecord) -> CompensationRecord:
        async with self._lock:
            if compensation.compensation_id not in self._by_id:
                raise NotFoundError(
                    "compensation record not found",
                    details={"compensation_id": compensation.compensation_id},
                )
            self._by_id[compensation.compensation_id] = compensation
            return compensation

    async def list_outstanding(self) -> Sequence[CompensationRecord]:
        return sorted(
            (c for c in self._by_id.values() if c.status is CompensationStatus.RECORDED),
            key=lambda c: c.compensation_id,
        )

    async def list_for_action(self, action_id: str) -> Sequence[CompensationRecord]:
        return sorted(
            (c for c in self._by_id.values() if c.action_id == action_id),
            key=lambda c: c.compensation_id,
        )
