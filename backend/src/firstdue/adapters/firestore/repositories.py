"""Firestore repositories.

The invariants are the same ones the in-memory adapters enforce -- the
difference is *where* they are enforced. In memory an ``asyncio.Lock`` is enough
because there is one process. Here there are many, so every read-modify-write
runs inside a Firestore transaction and every append-only create uses
``create()`` rather than ``set()``: a duplicate id then fails at the database,
not at a Python guard a second instance could race past.

Both adapters are held to one contract suite (``tests/contract``). A behavioural
difference between them is a bug in one of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel

from firstdue.adapters.firestore.client import DocumentStore, FirestoreConfig, document_store
from firstdue.adapters.firestore.codec import decode, decode_all, encode
from firstdue.domain.conflicts import Conflict, ConflictResolution, ConflictStatus
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
from firstdue.domain.incidents import Incident, IncidentStatus
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
    WriteContentionError,
)
from firstdue.observability.logging import get_logger
from firstdue.reliability.retry import RetryPolicy, backoff_ms

M = TypeVar("M", bound=BaseModel)

#: Log-entry document ids sort lexicographically in sequence order.
_SEQUENCE_WIDTH = 9


#: How many times the client retries a transaction aborted by a concurrent
#: write before giving up. Its default; named here because the exhaustion path
#: is translated below and the number appears in the error.
logger = get_logger(__name__)

TRANSACTION_ATTEMPTS = 5

#: How many times a contender re-attempts a lock whose transaction could not
#: commit and whose document nobody holds. This is a tie-break between
#: instances, not a retry policy for an outage -- but it has to be deep enough
#: that a realistic fleet resolves. Measured when this was written: two, four,
#: and eight simultaneous contenders always settle on exactly one holder.
LOCK_ACQUIRE_ATTEMPTS = 7

#: Short delays, widening fast. The work behind the lock takes milliseconds, so
#: a contender that waited a second would be standing down in all but name;
#: but the spread has to grow or contenders keep colliding on the same retry.
LOCK_RETRY_POLICY: RetryPolicy = RetryPolicy(
    max_attempts=LOCK_ACQUIRE_ATTEMPTS, base_delay_ms=15, max_delay_ms=500, multiplier=2.5
)


def _transactional(func: Any) -> Any:
    """Wrap a coroutine so Firestore retries it on contention.

    Imported lazily for the same reason the client is: a fake-mode process must
    not need the library present.
    """
    from google.cloud.firestore_v1.async_transaction import async_transactional

    wrapped: Any = async_transactional(func)
    return wrapped


async def _commit(func: Any, *, store: DocumentStore, entity: str) -> Any:
    """Run a transaction, translating contention into a domain error.

    When several writers race for one document, the client retries the aborted
    transaction and -- if it never wins -- raises a bare ``ValueError`` saying it
    exhausted its attempts. Left alone that surfaces as a 500 for what is really
    "somebody else got there first", so it becomes a
    :class:`~firstdue.errors.WriteContentionError` (a ``StaleVersionError``,
    HTTP 409) that callers already know how to handle.

    Found by running the concurrency contract tests, not by reading the
    client's source.
    """
    from google.api_core.exceptions import Aborted

    try:
        return await _transactional(func)(store.transaction())
    except Aborted as exc:
        raise WriteContentionError(entity=entity, attempts=TRANSACTION_ATTEMPTS) from exc
    except ValueError as exc:
        if "Failed to commit transaction" in str(exc):
            raise WriteContentionError(entity=entity, attempts=TRANSACTION_ATTEMPTS) from exc
        raise


class _Repository:
    """Shared construction: a client, a config, and the collections it uses."""

    def __init__(self, client: Any, config: FirestoreConfig) -> None:
        self._client = client
        self._config = config

    def _store(self, collection: str) -> DocumentStore:
        return document_store(self._client, self._config, collection)


# ------------------------------------------------------------------ profiles


class FirestoreProfileRepository(_Repository):
    """Profiles with optimistic concurrency on ``profile_version``.

    The version check and the write happen in one transaction, so two instances
    that both read version 7 cannot both write version 8: the loser sees the
    committed 8 and gets a 409.
    """

    async def get(self, address_id: str) -> BuildingProfile | None:
        document = await self._store("profiles").get(address_id)
        return decode(BuildingProfile, document) if document else None

    async def list_by_district(self, district_id: str) -> Sequence[BuildingProfile]:
        documents = await self._store("profiles").list([("district_id", "==", district_id)])
        profiles = decode_all(BuildingProfile, documents)
        return sorted(profiles, key=lambda p: p.address_id)

    async def create(self, profile: BuildingProfile) -> BuildingProfile:
        created = await self._store("profiles").create(profile.address_id, self._document(profile))
        if not created:
            raise ValidationError(
                "profile already exists", details={"address_id": profile.address_id}
            )
        return profile

    async def save(self, profile: BuildingProfile, *, expected_version: int) -> BuildingProfile:
        store = self._store("profiles")
        ref = store.ref(profile.address_id)
        document = self._document(profile)

        async def _save(transaction: Any) -> BuildingProfile:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError("profile not found", details={"address_id": profile.address_id})
            stored = decode(BuildingProfile, snapshot.to_dict() or {})
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
            transaction.set(ref, document)
            return profile

        result: BuildingProfile = await _commit(
            _save, store=store, entity=f"profile {profile.address_id}"
        )
        return result

    @staticmethod
    def _document(profile: BuildingProfile) -> dict[str, Any]:
        return encode(
            profile,
            address_id=profile.address_id,
            district_id=profile.district_id,
            profile_version=profile.profile_version,
        )


class FirestoreSnapshotRepository(_Repository):
    """Frozen profile reads, addressed by their stable id.

    ``put`` uses ``create`` and treats an existing id as success, which is what
    makes re-snapshotting a profile version return the original read rather than
    a second one taken microseconds later.
    """

    async def put(self, snapshot: ProfileSnapshot) -> ProfileSnapshot:
        store = self._store("snapshots")
        created = await store.create(
            snapshot.snapshot_id,
            encode(
                snapshot,
                snapshot_id=snapshot.snapshot_id,
                address_id=snapshot.address_id,
                profile_version=snapshot.profile_version,
            ),
        )
        if created:
            return snapshot
        existing = await store.get(snapshot.snapshot_id)
        if existing is None:  # pragma: no cover - create said it existed
            raise NotFoundError(
                "snapshot vanished between create and read",
                details={"snapshot_id": snapshot.snapshot_id},
            )
        return decode(ProfileSnapshot, existing)

    async def get(self, snapshot_id: str) -> ProfileSnapshot | None:
        document = await self._store("snapshots").get(snapshot_id)
        return decode(ProfileSnapshot, document) if document else None

    async def list_for_address(self, address_id: str) -> Sequence[ProfileSnapshot]:
        documents = await self._store("snapshots").list([("address_id", "==", address_id)])
        return sorted(
            decode_all(ProfileSnapshot, documents),
            key=lambda s: (s.profile_version, s.snapshot_id),
        )


# --------------------------------------------------------------------- facts


class FirestoreFactRepository(_Repository):
    """Append-only fact store. ``create`` is the enforcement, not a guard."""

    async def append_many(self, facts: Sequence[StructuralFact]) -> int:
        """See :meth:`FactRepository.append_many`. Seed path only."""
        return await self._store("facts").create_many(
            [
                (
                    fact.fact_id,
                    encode(
                        fact,
                        fact_id=fact.fact_id,
                        address_id=fact.address_id,
                        canonical_key=fact.canonical_key,
                    ),
                )
                for fact in facts
            ]
        )

    async def append(self, fact: StructuralFact) -> StructuralFact:
        created = await self._store("facts").create(
            fact.fact_id,
            encode(
                fact,
                fact_id=fact.fact_id,
                address_id=fact.address_id,
                canonical_key=fact.canonical_key,
            ),
        )
        if not created:
            raise AppendOnlyViolationError(
                "fact already written; corrections are new facts",
                details={"fact_id": fact.fact_id},
            )
        return fact

    async def get(self, fact_id: str) -> StructuralFact | None:
        document = await self._store("facts").get(fact_id)
        return decode(StructuralFact, document) if document else None

    async def list_for_address(self, address_id: str) -> Sequence[StructuralFact]:
        documents = await self._store("facts").list([("address_id", "==", address_id)])
        return sorted(
            decode_all(StructuralFact, documents),
            key=lambda f: (f.ingested_at, f.fact_id),
        )


class FirestoreConflictRepository(_Repository):
    async def add(self, conflict: Conflict) -> Conflict:
        created = await self._store("conflicts").create(
            conflict.conflict_id, self._document(conflict)
        )
        if not created:
            raise AppendOnlyViolationError(
                "conflict already recorded", details={"conflict_id": conflict.conflict_id}
            )
        return conflict

    async def get(self, conflict_id: str) -> Conflict | None:
        document = await self._store("conflicts").get(conflict_id)
        return decode(Conflict, document) if document else None

    async def list_for_address(self, address_id: str) -> Sequence[Conflict]:
        documents = await self._store("conflicts").list([("address_id", "==", address_id)])
        return sorted(decode_all(Conflict, documents), key=lambda c: c.conflict_id)

    async def list_open(self, district_id: str | None = None) -> Sequence[Conflict]:
        documents = await self._store("conflicts").list(
            [("status", "==", str(ConflictStatus.OPEN))]
        )
        return sorted(decode_all(Conflict, documents), key=lambda c: c.conflict_id)

    async def resolve(self, conflict_id: str, resolution: ConflictResolution) -> Conflict:
        store = self._store("conflicts")
        ref = store.ref(conflict_id)

        async def _resolve(transaction: Any) -> Conflict:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError("conflict not found", details={"conflict_id": conflict_id})
            stored = decode(Conflict, snapshot.to_dict() or {})
            resolved = stored.resolve(resolution)
            transaction.set(ref, self._document(resolved))
            return resolved

        result: Conflict = await _commit(_resolve, store=store, entity=f"conflict {conflict_id}")
        return result

    @staticmethod
    def _document(conflict: Conflict) -> dict[str, Any]:
        return encode(
            conflict,
            conflict_id=conflict.conflict_id,
            address_id=conflict.address_id,
            status=str(conflict.status),
            rule_id=conflict.rule_id,
        )


# ----------------------------------------------------------------- incidents


class FirestoreIncidentRepository(_Repository):
    async def create(self, incident: Incident) -> Incident:
        created = await self._store("incidents").create(
            incident.incident_id, self._document(incident)
        )
        if not created:
            raise ValidationError(
                "incident already exists", details={"incident_id": incident.incident_id}
            )
        return incident

    async def get(self, incident_id: str) -> Incident | None:
        document = await self._store("incidents").get(incident_id)
        return decode(Incident, document) if document else None

    async def save(self, incident: Incident) -> Incident:
        store = self._store("incidents")
        existing = await store.get(incident.incident_id)
        if existing is None:
            raise NotFoundError("incident not found", details={"incident_id": incident.incident_id})
        await store.put(incident.incident_id, self._document(incident))
        return incident

    async def list_open(self) -> Sequence[Incident]:
        documents = await self._store("incidents").list(
            [("status", "==", str(IncidentStatus.OPEN))]
        )
        return sorted(decode_all(Incident, documents), key=lambda i: i.incident_id)

    @staticmethod
    def _document(incident: Incident) -> dict[str, Any]:
        return encode(
            incident,
            incident_id=incident.incident_id,
            address_id=incident.address_id,
            status=str(incident.status),
        )


class FirestoreIncidentLogRepository(_Repository):
    """Append-only, gapless, sealable -- enforced by one transaction per append.

    The sequence counter and the entry are written together. Two instances
    logging simultaneously cannot both claim sequence 12: one transaction
    commits, the other retries against the committed counter and takes 13.
    """

    @staticmethod
    def _entry_id(incident_id: str, sequence: int) -> str:
        return f"{incident_id}:{sequence:0{_SEQUENCE_WIDTH}d}"

    async def append(self, entry: IncidentLogEntry) -> IncidentLogEntry:
        logs = self._store("incident_logs")
        entries = self._store("incident_log_entries")
        log_ref = logs.ref(entry.incident_id)
        stored_entry = entry if entry.content_hash else entry.sealed()
        entry_ref = entries.ref(self._entry_id(entry.incident_id, stored_entry.sequence))

        async def _append(transaction: Any) -> IncidentLogEntry:
            snapshot = await log_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            if data.get("sealed_at"):
                raise AppendOnlyViolationError(
                    "the incident log is sealed",
                    details={"incident_id": entry.incident_id},
                )
            next_sequence = int(data.get("next_sequence", 0))
            if stored_entry.sequence != next_sequence:
                raise AppendOnlyViolationError(
                    "log entries must be appended in sequence",
                    details={"expected": next_sequence, "found": stored_entry.sequence},
                )
            transaction.create(
                entry_ref,
                encode(
                    stored_entry,
                    incident_id=stored_entry.incident_id,
                    sequence=stored_entry.sequence,
                    flushed=stored_entry.written_to_rms_at is not None,
                ),
            )
            transaction.set(
                log_ref,
                {
                    "incident_id": entry.incident_id,
                    "next_sequence": next_sequence + 1,
                    "sealed_at": data.get("sealed_at"),
                },
            )
            return stored_entry

        result: IncidentLogEntry = await _commit(
            _append, store=logs, entity=f"incident log {entry.incident_id}"
        )
        return result

    async def get_log(self, incident_id: str) -> AppendOnlyLog:
        header = await self._store("incident_logs").get(incident_id)
        documents = await self._store("incident_log_entries").list(
            [("incident_id", "==", incident_id)]
        )
        entries = sorted(decode_all(IncidentLogEntry, documents), key=lambda e: e.sequence)
        sealed_at = header.get("sealed_at") if header else None
        return AppendOnlyLog(
            incident_id=incident_id,
            entries=tuple(entries),
            sealed_at=sealed_at,
        )

    async def next_sequence(self, incident_id: str) -> int:
        header = await self._store("incident_logs").get(incident_id)
        return int(header.get("next_sequence", 0)) if header else 0

    async def seal(self, incident_id: str, *, at: datetime) -> AppendOnlyLog:
        logs = self._store("incident_logs")
        ref = logs.ref(incident_id)

        async def _seal(transaction: Any) -> None:
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            if data.get("sealed_at"):
                # Sealing twice keeps the first seal time.
                return
            transaction.set(
                ref,
                {
                    "incident_id": incident_id,
                    "next_sequence": int(data.get("next_sequence", 0)),
                    "sealed_at": at.isoformat(),
                },
            )

        await _commit(_seal, store=logs, entity=f"incident log {incident_id}")
        return await self.get_log(incident_id)

    async def mark_written_to_rms(
        self, incident_id: str, entry_id: str, *, at: datetime
    ) -> IncidentLogEntry:
        documents = await self._store("incident_log_entries").list(
            [("incident_id", "==", incident_id)]
        )
        entries = decode_all(IncidentLogEntry, documents)
        for entry in entries:
            if entry.entry_id != entry_id:
                continue
            flushed = entry.mark_written_to_rms(at=at)
            await self._store("incident_log_entries").put(
                self._entry_id(incident_id, entry.sequence),
                encode(
                    flushed,
                    incident_id=flushed.incident_id,
                    sequence=flushed.sequence,
                    flushed=True,
                ),
            )
            return flushed
        raise NotFoundError("log entry not found", details={"entry_id": entry_id})

    async def list_unflushed(self) -> Sequence[IncidentLogEntry]:
        documents = await self._store("incident_log_entries").list([("flushed", "==", False)])
        return sorted(
            decode_all(IncidentLogEntry, documents),
            key=lambda e: (e.incident_id, e.sequence),
        )


# ------------------------------------------------------------------ registry


class FirestoreRegistryRepository(_Repository):
    """The agent catalog. A published version is immutable, transactionally."""

    @staticmethod
    def _agent_id(agent_id: str, version: str) -> str:
        return f"{agent_id}@{version}"

    async def publish(self, descriptor: AgentDescriptor) -> AgentDescriptor:
        store = self._store("registry_agents")
        document_id = self._agent_id(descriptor.agent_id, descriptor.version)
        ref = store.ref(document_id)
        document = encode(
            descriptor,
            agent_id=descriptor.agent_id,
            version=descriptor.version,
            publisher_department=str(descriptor.publisher_department),
            loop=str(descriptor.loop),
        )

        async def _publish(transaction: Any) -> AgentDescriptor:
            snapshot = await ref.get(transaction=transaction)
            if snapshot.exists:
                existing = decode(AgentDescriptor, snapshot.to_dict() or {})
                if existing != descriptor:
                    raise AppendOnlyViolationError(
                        "a published agent version is immutable; publish a new version instead",
                        details={"agent_ref": descriptor.ref},
                    )
                return existing
            transaction.create(ref, document)
            return descriptor

        result: AgentDescriptor = await _commit(
            _publish, store=store, entity=f"agent {descriptor.ref}"
        )
        return result

    async def get_agent(self, agent_id: str, version: str) -> AgentDescriptor | None:
        document = await self._store("registry_agents").get(self._agent_id(agent_id, version))
        return decode(AgentDescriptor, document) if document else None

    async def list_agents(
        self, *, publisher_department: str | None = None
    ) -> Sequence[AgentDescriptor]:
        filters = (
            [("publisher_department", "==", publisher_department)]
            if publisher_department is not None
            else []
        )
        documents = await self._store("registry_agents").list(filters)
        return sorted(decode_all(AgentDescriptor, documents), key=lambda d: (d.agent_id, d.version))

    async def subscribe(self, subscription: Subscription) -> Subscription:
        target = await self.get_agent(subscription.agent_id, subscription.pinned_version)
        if target is None:
            raise NotFoundError(
                "cannot subscribe to an unpublished agent version",
                details={"agent_ref": subscription.ref},
            )
        await self._store("registry_subscriptions").put(
            subscription.subscription_id,
            encode(
                subscription,
                subscription_id=subscription.subscription_id,
                subscriber_department=str(subscription.subscriber_department),
                agent_id=subscription.agent_id,
            ),
        )
        return subscription

    async def list_subscriptions(
        self, *, subscriber_department: str | None = None
    ) -> Sequence[Subscription]:
        filters = (
            [("subscriber_department", "==", subscriber_department)]
            if subscriber_department is not None
            else []
        )
        documents = await self._store("registry_subscriptions").list(filters)
        return sorted(decode_all(Subscription, documents), key=lambda s: s.subscription_id)

    async def resolve_pinned(
        self, subscriber_department: str, agent_id: str
    ) -> AgentDescriptor | None:
        documents = await self._store("registry_subscriptions").list(
            [
                ("subscriber_department", "==", subscriber_department),
                ("agent_id", "==", agent_id),
            ]
        )
        subscriptions = sorted(decode_all(Subscription, documents), key=lambda s: s.subscription_id)
        if not subscriptions:
            return None
        return await self.get_agent(agent_id, subscriptions[0].pinned_version)


# -------------------------------------------------------------------- grants


class FirestoreGrantRepository(_Repository):
    async def store_incident_grant(self, grant: IncidentGrant) -> IncidentGrant:
        await self._store("incident_grants").put(
            grant.grant_id,
            encode(grant, grant_id=grant.grant_id, incident_id=grant.incident_id),
        )
        return grant

    async def get_incident_grant(self, grant_id: str) -> IncidentGrant | None:
        document = await self._store("incident_grants").get(grant_id)
        return decode(IncidentGrant, document) if document else None

    async def revoke_incident_grant(self, grant_id: str, *, at: datetime) -> IncidentGrant:
        store = self._store("incident_grants")
        ref = store.ref(grant_id)

        async def _revoke(transaction: Any) -> IncidentGrant:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError("grant not found", details={"grant_id": grant_id})
            stored = decode(IncidentGrant, snapshot.to_dict() or {})
            revoked = stored.revoke(at=at)
            transaction.set(
                ref,
                encode(revoked, grant_id=revoked.grant_id, incident_id=revoked.incident_id),
            )
            return revoked

        result: IncidentGrant = await _commit(_revoke, store=store, entity=f"grant {grant_id}")
        return result

    async def store_standing_grant(self, grant: StandingGrant) -> StandingGrant:
        await self._store("standing_grants").put(
            grant.agent_id, encode(grant, agent_id=grant.agent_id)
        )
        return grant

    async def get_standing_grant(self, agent_id: str) -> StandingGrant | None:
        document = await self._store("standing_grants").get(agent_id)
        return decode(StandingGrant, document) if document else None


# --------------------------------------------------------------- slow-loop work


class FirestoreQueueRepository(_Repository):
    async def replace_district_queue(
        self, district_id: str, entries: Sequence[SurveyQueueEntry]
    ) -> Sequence[SurveyQueueEntry]:
        store = self._store("queue_entries")
        ordered = sorted(entries, key=lambda e: e.rank)
        existing = await store.list([("district_id", "==", district_id)])
        keep = {entry.entry_id for entry in ordered}
        for document in existing:
            previous = decode(SurveyQueueEntry, document)
            if previous.entry_id not in keep:
                await store.delete(previous.entry_id)
        for entry in ordered:
            await store.put(entry.entry_id, self._document(entry))
        return ordered

    async def list_for_district(self, district_id: str) -> Sequence[SurveyQueueEntry]:
        documents = await self._store("queue_entries").list([("district_id", "==", district_id)])
        return sorted(decode_all(SurveyQueueEntry, documents), key=lambda e: e.rank)

    async def get(self, entry_id: str) -> SurveyQueueEntry | None:
        document = await self._store("queue_entries").get(entry_id)
        return decode(SurveyQueueEntry, document) if document else None

    async def save(self, entry: SurveyQueueEntry) -> SurveyQueueEntry:
        store = self._store("queue_entries")
        if await store.get(entry.entry_id) is None:
            raise NotFoundError("queue entry not found", details={"entry_id": entry.entry_id})
        await store.put(entry.entry_id, self._document(entry))
        return entry

    @staticmethod
    def _document(entry: SurveyQueueEntry) -> dict[str, Any]:
        return encode(
            entry,
            entry_id=entry.entry_id,
            district_id=entry.district_id,
            address_id=entry.address_id,
            rank=entry.rank,
        )


class FirestoreReferralRepository(_Repository):
    async def add(self, referral: ReferralRecord) -> ReferralRecord:
        created = await self._store("referrals").create(
            referral.referral_id, self._document(referral)
        )
        if not created:
            raise ValidationError(
                "referral already exists", details={"referral_id": referral.referral_id}
            )
        return referral

    async def get(self, referral_id: str) -> ReferralRecord | None:
        document = await self._store("referrals").get(referral_id)
        return decode(ReferralRecord, document) if document else None

    async def save(self, referral: ReferralRecord) -> ReferralRecord:
        store = self._store("referrals")
        if await store.get(referral.referral_id) is None:
            raise NotFoundError("referral not found", details={"referral_id": referral.referral_id})
        await store.put(referral.referral_id, self._document(referral))
        return referral

    async def list_open(self) -> Sequence[ReferralRecord]:
        closed = {ReferralStatus.REJECTED, ReferralStatus.WITHDRAWN}
        documents = await self._store("referrals").list()
        return sorted(
            (r for r in decode_all(ReferralRecord, documents) if r.status not in closed),
            key=lambda r: r.referral_id,
        )

    @staticmethod
    def _document(referral: ReferralRecord) -> dict[str, Any]:
        return encode(
            referral,
            referral_id=referral.referral_id,
            address_id=referral.address_id,
            status=str(referral.status),
        )


class FirestoreApprovalRepository(_Repository):
    async def stage(self, approval: ApprovalRequest) -> ApprovalRequest:
        created = await self._store("approvals").create(
            approval.approval_id, self._document(approval)
        )
        if not created:
            raise ValidationError(
                "approval already staged", details={"approval_id": approval.approval_id}
            )
        return approval

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        document = await self._store("approvals").get(approval_id)
        return decode(ApprovalRequest, document) if document else None

    async def save(self, approval: ApprovalRequest) -> ApprovalRequest:
        store = self._store("approvals")
        if await store.get(approval.approval_id) is None:
            raise NotFoundError("approval not found", details={"approval_id": approval.approval_id})
        await store.put(approval.approval_id, self._document(approval))
        return approval

    async def list_for_incident(self, incident_id: str) -> Sequence[ApprovalRequest]:
        documents = await self._store("approvals").list([("incident_id", "==", incident_id)])
        return sorted(decode_all(ApprovalRequest, documents), key=lambda a: a.approval_id)

    @staticmethod
    def _document(approval: ApprovalRequest) -> dict[str, Any]:
        return encode(
            approval,
            approval_id=approval.approval_id,
            incident_id=approval.incident_id,
            status=str(approval.status),
        )


class FirestoreSurveyRepository(_Repository):
    async def add(self, survey: SurveyRecord) -> SurveyRecord:
        created = await self._store("surveys").create(
            survey.survey_id,
            encode(survey, survey_id=survey.survey_id, address_id=survey.address_id),
        )
        if not created:
            raise AppendOnlyViolationError(
                "survey already recorded", details={"survey_id": survey.survey_id}
            )
        return survey

    async def get(self, survey_id: str) -> SurveyRecord | None:
        document = await self._store("surveys").get(survey_id)
        return decode(SurveyRecord, document) if document else None

    async def list_for_address(self, address_id: str) -> Sequence[SurveyRecord]:
        documents = await self._store("surveys").list([("address_id", "==", address_id)])
        return sorted(decode_all(SurveyRecord, documents), key=lambda s: s.survey_id)


class FirestoreWriteActionRepository(_Repository):
    """Every external write and its receipt, for audit and for rollback."""

    async def record(self, action: WriteAction) -> WriteAction:
        await self._store("write_actions").put(
            action.action_id,
            encode(
                action,
                action_id=action.action_id,
                target=action.target,
                idempotency_key=action.idempotency_key,
                status=str(action.status),
            ),
        )
        return action

    async def get(self, action_id: str) -> WriteAction | None:
        document = await self._store("write_actions").get(action_id)
        return decode(WriteAction, document) if document else None

    async def find_by_idempotency_key(self, target: str, key: str) -> WriteAction | None:
        documents = await self._store("write_actions").list(
            [("target", "==", target), ("idempotency_key", "==", key)]
        )
        actions = sorted(
            decode_all(WriteAction, documents), key=lambda a: (a.created_at, a.action_id)
        )
        return actions[0] if actions else None

    async def save_receipt(self, receipt: WriteReceipt) -> WriteReceipt:
        await self._store("write_receipts").put(
            receipt.action_id, encode(receipt, action_id=receipt.action_id)
        )
        return receipt

    async def get_receipt(self, action_id: str) -> WriteReceipt | None:
        document = await self._store("write_receipts").get(action_id)
        return decode(WriteReceipt, document) if document else None


# --------------------------------------------------------------------- locks


class FirestoreLockRepository(_Repository):
    """Leased, fenced locks in one transaction each.

    The fence document survives release: the counter must never repeat, or a
    process that slept through its lease could wake up holding a fence that
    looks current.

    **Exhausted attempts is not the same as a clean loss**, and reading it as
    one is a livelock. A clean loss means somebody else holds the lock, so
    standing down is correct: their pass produces the result ours would have.
    Exhaustion means *nobody* could commit -- and if every contender reads that
    as a loss, every contender stands down and the work never happens. Two
    instances polling one district would both decline and the profile would go
    unmaterialized until the next scheduler tick.

    So exhaustion re-reads the lock and asks the question that actually
    matters: is it held now? If it is, this was a loss. If it is free, nobody
    won and this contender tries again, after a delay derived from its own
    owner id so two contenders do not retry in lockstep and collide forever.

    Found by the concurrency contract tests failing about one run in three,
    with *both* instances reporting they had not run.
    """

    async def acquire(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None:
        if lease <= timedelta(0):
            raise ValidationError("a lock lease must be positive", details={"lock_id": lock_id})
        store = self._store("locks")
        ref = store.ref(lock_id)

        async def _acquire(transaction: Any) -> LockLease | None:
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            fence = int(data.get("fence", 0))
            if data.get("payload") and not data.get("released"):
                held = decode(LockLease, data)
                if not held.is_expired(now) and held.owner != owner:
                    return None
            granted = LockLease(
                lock_id=lock_id,
                owner=owner,
                acquired_at=now,
                expires_at=now + lease,
                fence=fence + 1,
            )
            document = encode(granted, lock_id=lock_id, owner=owner, fence=granted.fence)
            document["released"] = False
            transaction.set(ref, document)
            return granted

        for attempt in range(1, LOCK_ACQUIRE_ATTEMPTS + 1):
            try:
                granted: LockLease | None = await _commit(
                    _acquire, store=store, entity=f"lock {lock_id}"
                )
                return granted
            except WriteContentionError:
                # Somebody else holds it: a real loss, and standing down is the
                # correct response.
                if await self._is_held_by_another(store, lock_id, owner=owner, now=now):
                    logger.info(
                        "lock_lost_to_holder",
                        extra={"lock_id": lock_id, "attempt": attempt},
                    )
                    return None
                if attempt == LOCK_ACQUIRE_ATTEMPTS:
                    # Nobody holds it and nobody can commit. Refusing is still
                    # better than a caller that believes it holds a lock it
                    # does not, and the next tick will try again.
                    logger.warning(
                        "lock_contention_unresolved",
                        extra={"lock_id": lock_id, "attempts": attempt},
                    )
                    return None
                # Derived from the owner, not drawn from a PRNG: two contenders
                # get different delays, and a replay reproduces the timing it
                # recorded.
                delay_ms = backoff_ms(attempt + 1, policy=LOCK_RETRY_POLICY, seed=owner)
                await asyncio.sleep(delay_ms / 1000.0)
        return None  # pragma: no cover - the loop always returns

    async def _is_held_by_another(
        self, store: DocumentStore, lock_id: str, *, owner: str, now: datetime
    ) -> bool:
        """Whether a live lease belonging to somebody else exists right now."""
        data = await store.get(lock_id)
        if not data or not data.get("payload") or data.get("released"):
            return False
        held = decode(LockLease, data)
        return not held.is_expired(now) and held.owner != owner

    async def renew(
        self, lock_id: str, *, owner: str, now: datetime, lease: timedelta
    ) -> LockLease | None:
        store = self._store("locks")
        ref = store.ref(lock_id)

        async def _renew(transaction: Any) -> LockLease | None:
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            if not data.get("payload") or data.get("released"):
                return None
            held = decode(LockLease, data)
            if not held.is_held_by(owner, now=now):
                return None
            renewed = held.renewed(now=now, lease=lease)
            document = encode(renewed, lock_id=lock_id, owner=owner, fence=renewed.fence)
            document["released"] = False
            transaction.set(ref, document)
            return renewed

        try:
            renewed_lease: LockLease | None = await _commit(
                _renew, store=store, entity=f"lock {lock_id}"
            )
        except WriteContentionError:
            return None
        return renewed_lease

    async def release(self, lock_id: str, *, owner: str) -> bool:
        store = self._store("locks")
        ref = store.ref(lock_id)

        async def _release(transaction: Any) -> bool:
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            if not data.get("payload") or data.get("released"):
                return False
            held = decode(LockLease, data)
            if held.owner != owner:
                return False
            # The fence survives the release; the lease does not.
            transaction.set(
                ref,
                {"lock_id": lock_id, "fence": held.fence, "released": True, "owner": owner},
            )
            return True

        try:
            result: bool = await _commit(_release, store=store, entity=f"lock {lock_id}")
        except WriteContentionError:
            return False
        return result

    async def get(self, lock_id: str) -> LockLease | None:
        document = await self._store("locks").get(lock_id)
        if not document or not document.get("payload") or document.get("released"):
            return None
        return decode(LockLease, document)


class FirestoreIdempotencyRepository(_Repository):
    """Durable memory of which keys have already been acted on."""

    async def claim(self, record: IdempotencyRecord) -> IdempotencyClaim:
        store = self._store("idempotency")
        ref = store.ref(record.storage_id)
        document = self._document(record)

        async def _claim(transaction: Any) -> IdempotencyClaim:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                transaction.create(ref, document)
                return IdempotencyClaim(outcome=IdempotencyOutcome.FRESH, record=record)
            stored = decode(IdempotencyRecord, snapshot.to_dict() or {})
            if stored.request_hash != record.request_hash:
                raise IdempotencyMismatchError(
                    "this idempotency key was already used for a different request",
                    details={"scope": record.scope, "key": record.key},
                )
            if stored.status is IdempotencyStatus.COMPLETED:
                return IdempotencyClaim(outcome=IdempotencyOutcome.REPLAY, record=stored)
            if stored.is_claimable(record.claimed_at):
                transaction.set(ref, document)
                return IdempotencyClaim(outcome=IdempotencyOutcome.FRESH, record=record)
            return IdempotencyClaim(outcome=IdempotencyOutcome.IN_PROGRESS, record=stored)

        result: IdempotencyClaim = await _commit(
            _claim, store=store, entity=f"idempotency {record.scope}"
        )
        return result

    async def complete(
        self, scope: str, key: str, *, at: datetime, result_ref: str | None = None
    ) -> IdempotencyRecord:
        store = self._store("idempotency")
        ref = store.ref(storage_id_for(scope, key))

        async def _complete(transaction: Any) -> IdempotencyRecord:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError(
                    "no idempotency claim to complete", details={"scope": scope, "key": key}
                )
            stored = decode(IdempotencyRecord, snapshot.to_dict() or {})
            if stored.status is IdempotencyStatus.COMPLETED:
                return stored
            completed = stored.completed(at=at, result_ref=result_ref)
            transaction.set(ref, self._document(completed))
            return completed

        result: IdempotencyRecord = await _commit(
            _complete, store=store, entity=f"idempotency {scope}"
        )
        return result

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        document = await self._store("idempotency").get(storage_id_for(scope, key))
        return decode(IdempotencyRecord, document) if document else None

    @staticmethod
    def _document(record: IdempotencyRecord) -> dict[str, Any]:
        return encode(
            record,
            scope=record.scope,
            idempotency_key=record.key,
            status=str(record.status),
        )


class FirestoreAgentRunRepository(_Repository):
    """Agent runs, their checkpoints, and their terminal states."""

    async def start(self, run: AgentRunRecord) -> AgentRunRecord:
        created = await self._store("agent_runs").create(run.run_id, self._document(run))
        if not created:
            raise ValidationError("agent run already exists", details={"run_id": run.run_id})
        return run

    async def get(self, run_id: str) -> AgentRunRecord | None:
        document = await self._store("agent_runs").get(run_id)
        return decode(AgentRunRecord, document) if document else None

    async def save(self, run: AgentRunRecord) -> AgentRunRecord:
        store = self._store("agent_runs")
        ref = store.ref(run.run_id)
        document = self._document(run)

        async def _save(transaction: Any) -> AgentRunRecord:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError("agent run not found", details={"run_id": run.run_id})
            stored = decode(AgentRunRecord, snapshot.to_dict() or {})
            if stored.is_terminal:
                raise ValidationError(
                    "an agent run in a terminal state cannot be overwritten",
                    details={"run_id": run.run_id, "status": str(stored.status)},
                )
            transaction.set(ref, document)
            return run

        result: AgentRunRecord = await _commit(_save, store=store, entity=f"run {run.run_id}")
        return result

    async def checkpoint(self, run_id: str, checkpoint: RunCheckpoint) -> AgentRunRecord:
        store = self._store("agent_runs")
        ref = store.ref(run_id)

        async def _checkpoint(transaction: Any) -> AgentRunRecord:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError("agent run not found", details={"run_id": run_id})
            stored = decode(AgentRunRecord, snapshot.to_dict() or {})
            updated = stored.checkpointed(checkpoint)
            transaction.set(ref, self._document(updated))
            return updated

        result: AgentRunRecord = await _commit(_checkpoint, store=store, entity=f"run {run_id}")
        return result

    async def list_resumable(self, *, agent_id: str | None = None) -> Sequence[AgentRunRecord]:
        filters = [("agent_id", "==", agent_id)] if agent_id is not None else []
        documents = await self._store("agent_runs").list(filters)
        return sorted(
            (run for run in decode_all(AgentRunRecord, documents) if run.is_resumable),
            key=lambda r: r.run_id,
        )

    async def find_by_idempotency_key(self, key: str) -> AgentRunRecord | None:
        documents = await self._store("agent_runs").list([("idempotency_key", "==", key)])
        runs = sorted(decode_all(AgentRunRecord, documents), key=lambda r: (r.started_at, r.run_id))
        return runs[0] if runs else None

    @staticmethod
    def _document(run: AgentRunRecord) -> dict[str, Any]:
        return encode(
            run,
            run_id=run.run_id,
            agent_id=run.agent_id,
            idempotency_key=run.idempotency_key,
            status=str(run.status),
        )


class FirestoreCompensationRepository(_Repository):
    """Recorded obligations to undo executed writes."""

    async def record(self, compensation: CompensationRecord) -> CompensationRecord:
        store = self._store("compensations")
        created = await store.create(compensation.compensation_id, self._document(compensation))
        if created:
            return compensation
        existing = await store.get(compensation.compensation_id)
        if existing is None:  # pragma: no cover - create said it existed
            raise NotFoundError(
                "compensation vanished between create and read",
                details={"compensation_id": compensation.compensation_id},
            )
        return decode(CompensationRecord, existing)

    async def get(self, compensation_id: str) -> CompensationRecord | None:
        document = await self._store("compensations").get(compensation_id)
        return decode(CompensationRecord, document) if document else None

    async def save(self, compensation: CompensationRecord) -> CompensationRecord:
        store = self._store("compensations")
        if await store.get(compensation.compensation_id) is None:
            raise NotFoundError(
                "compensation record not found",
                details={"compensation_id": compensation.compensation_id},
            )
        await store.put(compensation.compensation_id, self._document(compensation))
        return compensation

    async def list_outstanding(self) -> Sequence[CompensationRecord]:
        documents = await self._store("compensations").list(
            [("status", "==", str(CompensationStatus.RECORDED))]
        )
        return sorted(decode_all(CompensationRecord, documents), key=lambda c: c.compensation_id)

    async def list_for_action(self, action_id: str) -> Sequence[CompensationRecord]:
        documents = await self._store("compensations").list([("action_id", "==", action_id)])
        return sorted(decode_all(CompensationRecord, documents), key=lambda c: c.compensation_id)

    @staticmethod
    def _document(compensation: CompensationRecord) -> dict[str, Any]:
        return encode(
            compensation,
            compensation_id=compensation.compensation_id,
            action_id=compensation.action_id,
            status=str(compensation.status),
        )
