"""The behaviours every durable-memory backend must have.

Each test here runs against both the in-memory and the Firestore repositories.
The list is not arbitrary -- it is the set of properties the rest of the system
assumes and would silently violate if a backend got them wrong:

* optimistic concurrency on profiles (409, not last-write-wins);
* append-only facts, conflicts, timelines, and incident logs;
* gapless incident-log sequences under concurrency;
* stable snapshot ids, so an incident replays the state it briefed from;
* pinned agent versions that stay pinned when newer ones are published;
* leased, fenced distributed locks;
* idempotency records that turn a duplicate into exactly one effect;
* agent runs that reach a terminal state and never leave it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.container import Stores
from firstdue.domain.conflicts import Conflict, ConflictResolution, ConflictStatus
from firstdue.domain.enums import (
    AgentRunStatus,
    ApprovalThreshold,
    Capability,
    Classification,
    Department,
    LogEntryType,
    Loop,
    Operation,
    PolicyAction,
    Scope,
    SourceType,
    WriteActionStatus,
)
from firstdue.domain.facts import StructuralFact
from firstdue.domain.idempotency import (
    DEFAULT_CLAIM_TTL,
    IdempotencyOutcome,
    IdempotencyRecord,
    request_hash,
)
from firstdue.domain.keys import Keys
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.policy import PolicyDecision
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.domain.runs import AgentRunRecord, CompensationRecord, RunCheckpoint
from firstdue.domain.values import IntegerValue
from firstdue.domain.work import WriteAction
from firstdue.errors import (
    AppendOnlyViolationError,
    FirstDueError,
    IdempotencyMismatchError,
    StaleVersionError,
    ValidationError,
)
from firstdue.ports.audit import AuditEvent, AuditEventKind

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
DISTRICT = "sffd-district-03"


def _event(sequence: int, *, summary: str = "recorded") -> ProfileEvent:
    return ProfileEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        occurred_at=NOW,
        type=ProfileEventType.FACT_WRITTEN,
        actor="contract-test",
        summary=summary,
    )


def _fact(fact_id: str, *, stories: int = 2) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        value=IntegerValue(integer=stories),
        source_type=SourceType.PERMIT,
        source_ref="permit/2018-04871",
        source_snapshot_id="snapshot-1",
        observed_at=NOW - timedelta(days=100),
        ingested_at=NOW,
        confidence=0.9,
        classification=Classification.PUBLIC,
    )


def _descriptor(version: str, *, summary: str = "Polls municipal records.") -> AgentDescriptor:
    return AgentDescriptor(
        agent_id="records-watcher",
        version=version,
        publisher_department=Department.BUILDING,
        loop=Loop.SLOW,
        role_summary=summary,
        capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_PUBLIC_RECORDS}),
        classifications_accessed=frozenset({Classification.PUBLIC}),
        approval_threshold=ApprovalThreshold.NONE,
        input_schema_ref="firstdue.schemas.SourcePollRequest",
        output_schema_ref="firstdue.schemas.FactBatch",
        latency_target_ms=120_000,
        published_at=NOW,
    )


def _run(run_id: str = "run-1", *, key: str = "run-key-000001") -> AgentRunRecord:
    return AgentRunRecord(
        run_id=run_id,
        agent_id="records-watcher",
        agent_version="1.0.0",
        correlation_id="corr-1",
        idempotency_key=key,
        started_at=NOW,
    )


# ------------------------------------------------------- optimistic concurrency


@pytest.mark.concurrency
async def test_a_stale_profile_write_is_rejected_with_409(stores: Stores) -> None:
    await stores.profiles.create(BuildingProfile(address_id=ADDRESS, district_id=DISTRICT))
    reader_a = await stores.profiles.get(ADDRESS)
    reader_b = await stores.profiles.get(ADDRESS)
    assert reader_a is not None and reader_b is not None

    await stores.profiles.save(reader_a.append_event(_event(0)), expected_version=0)

    with pytest.raises(StaleVersionError) as excinfo:
        await stores.profiles.save(reader_b.append_event(_event(0)), expected_version=0)
    assert excinfo.value.http_status == 409


@pytest.mark.concurrency
async def test_concurrent_writers_never_both_succeed(stores: Stores) -> None:
    """The safety property optimistic concurrency actually guarantees.

    *At most one* writer commits. Not "exactly one": under heavy contention a
    store may abort every racer and leave them all to retry, and asserting that
    someone always wins the first round would be asserting liveness the system
    does not promise. What it does promise is that two writers never both
    succeed, and that a loser is told so in a way it can act on -- a 409, never
    an untranslated client error.
    """
    await stores.profiles.create(BuildingProfile(address_id=ADDRESS, district_id=DISTRICT))
    stored = await stores.profiles.get(ADDRESS)
    assert stored is not None

    async def write(index: int) -> bool:
        try:
            await stores.profiles.save(
                stored.append_event(_event(0, summary=f"writer-{index}")), expected_version=0
            )
        except StaleVersionError as exc:
            assert exc.http_status == 409
            return False
        return True

    results = await asyncio.gather(*(write(n) for n in range(5)))
    assert sum(results) <= 1

    final = await stores.profiles.get(ADDRESS)
    assert final is not None
    # Whatever happened, the store is consistent: the version counts the writes
    # that landed, and the timeline is exactly that long.
    assert final.profile_version == len(final.timeline)
    assert final.profile_version <= 1


@pytest.mark.concurrency
async def test_a_writer_that_lost_the_race_makes_progress_on_retry(stores: Stores) -> None:
    """The liveness the system does promise: re-read, rebuild, and it commits."""
    await stores.profiles.create(BuildingProfile(address_id=ADDRESS, district_id=DISTRICT))

    for expected_writes in range(1, 4):
        current = await stores.profiles.get(ADDRESS)
        assert current is not None
        await stores.profiles.save(
            current.append_event(_event(current.next_sequence)),
            expected_version=current.profile_version,
        )
        final = await stores.profiles.get(ADDRESS)
        assert final is not None
        assert final.profile_version == expected_writes


@pytest.mark.concurrency
async def test_a_profile_write_may_not_shorten_the_timeline(stores: Stores) -> None:
    await stores.profiles.create(BuildingProfile(address_id=ADDRESS, district_id=DISTRICT))
    stored = await stores.profiles.get(ADDRESS)
    assert stored is not None
    grown = stored.append_event(_event(0))
    await stores.profiles.save(grown, expected_version=0)

    truncated = grown.model_copy(update={"timeline": (), "profile_version": 2})
    with pytest.raises(AppendOnlyViolationError):
        await stores.profiles.save(truncated, expected_version=1)


# -------------------------------------------------------------- append-only


@pytest.mark.invariant
async def test_a_fact_cannot_be_written_twice(stores: Stores) -> None:
    await stores.facts.append(_fact("fact-1"))
    with pytest.raises(AppendOnlyViolationError):
        await stores.facts.append(_fact("fact-1", stories=3))

    stored = await stores.facts.get("fact-1")
    assert stored is not None
    assert stored.value.unwrap() == 2


@pytest.mark.invariant
async def test_conflicting_facts_both_persist(stores: Stores) -> None:
    await stores.facts.append(_fact("fact-permit", stories=2))
    await stores.facts.append(_fact("fact-lidar", stories=3))
    facts = await stores.facts.list_for_address(ADDRESS)
    assert {f.fact_id for f in facts} == {"fact-permit", "fact-lidar"}
    assert sorted(f.value.unwrap() for f in facts) == [2, 3]


@pytest.mark.invariant
async def test_a_conflict_is_recorded_once_and_resolved_by_a_human(stores: Stores) -> None:
    conflict = Conflict(
        conflict_id="conflict-1",
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        rule_id="permit-vs-lidar-story-count",
        severity=4,
        fact_ids=("fact-permit", "fact-lidar"),
        summary="Permit records 2 storeys; lidar DSM measures 3.",
        detected_at=NOW,
    )
    await stores.conflicts.add(conflict)
    with pytest.raises(AppendOnlyViolationError):
        await stores.conflicts.add(conflict)

    assert len(await stores.conflicts.list_open()) == 1

    resolved = await stores.conflicts.resolve(
        "conflict-1",
        ConflictResolution(
            resolved_at=NOW,
            resolving_record_id="survey-1",
            resolving_fact_id="fact-survey",
            resolved_by="capt-alvarez",
        ),
    )
    assert resolved.status is ConflictStatus.RESOLVED
    assert await stores.conflicts.list_open() == []


# ------------------------------------------------------- incident log sequences


@pytest.mark.invariant
async def test_the_incident_log_is_gapless_and_refuses_a_skipped_sequence(
    stores: Stores,
) -> None:
    for sequence in range(3):
        await stores.incident_log.append(_log_entry(sequence))

    assert await stores.incident_log.next_sequence("inc-1") == 3

    with pytest.raises(AppendOnlyViolationError):
        await stores.incident_log.append(_log_entry(4))

    log = await stores.incident_log.get_log("inc-1")
    assert [entry.sequence for entry in log.entries] == [0, 1, 2]


@pytest.mark.concurrency
async def test_concurrent_log_appends_never_share_a_sequence(stores: Stores) -> None:
    """Two writers racing for sequence 0. At most one lands, and it stays gapless.

    The refused writer's correct move is to re-read ``next_sequence`` and try
    again -- which is why the refusal must be a domain error it can recognise,
    not a client exception leaking through.
    """
    results = await asyncio.gather(
        stores.incident_log.append(_log_entry(0, entry_id="a")),
        stores.incident_log.append(_log_entry(0, entry_id="b")),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(accepted) <= 1
    assert all(isinstance(r, FirstDueError) for r in refused)
    assert all(r.http_status == 409 for r in refused if isinstance(r, FirstDueError))

    log = await stores.incident_log.get_log("inc-1")
    sequences = [entry.sequence for entry in log.entries]
    # The invariant: gapless from zero, and never two entries at one sequence.
    assert sequences == list(range(len(accepted)))

    # And the loser makes progress on retry.
    entry = await stores.incident_log.append(
        _log_entry(await stores.incident_log.next_sequence("inc-1"))
    )
    assert entry.sequence == len(accepted)


@pytest.mark.invariant
async def test_a_sealed_log_accepts_nothing_further(stores: Stores) -> None:
    await stores.incident_log.append(_log_entry(0))
    sealed = await stores.incident_log.seal("inc-1", at=NOW)
    assert sealed.sealed_at is not None

    with pytest.raises(AppendOnlyViolationError):
        await stores.incident_log.append(_log_entry(1))


async def test_unflushed_entries_are_listed_until_the_records_system_takes_them(
    stores: Stores,
) -> None:
    entry = await stores.incident_log.append(_log_entry(0))
    assert len(await stores.incident_log.list_unflushed()) == 1

    await stores.incident_log.mark_written_to_rms("inc-1", entry.entry_id, at=NOW)
    assert await stores.incident_log.list_unflushed() == []


def _log_entry(sequence: int, *, entry_id: str | None = None) -> IncidentLogEntry:
    return IncidentLogEntry(
        entry_id=entry_id or f"entry-{sequence}",
        incident_id="inc-1",
        sequence=sequence,
        entry_type=LogEntryType.BRIEF_EMITTED,
        occurred_at=NOW,
        profile_snapshot_id="snap-1",
        content={"stage": "INSTANT"},
    )


# ------------------------------------------------------------------ snapshots


async def test_a_snapshot_id_is_stable_and_re_putting_it_returns_the_original(
    stores: Stores,
) -> None:
    profile = BuildingProfile(address_id=ADDRESS, district_id=DISTRICT).append_event(_event(0))
    first = profile.snapshot(read_at=NOW)
    stored = await stores.snapshots.put(first)
    assert stored.snapshot_id == first.snapshot_id

    # A second read of the same profile version, taken later, must not become a
    # second snapshot: the incident briefed from the first one.
    later = profile.snapshot(read_at=NOW + timedelta(hours=3))
    assert later.snapshot_id == first.snapshot_id
    replayed = await stores.snapshots.put(later)
    assert replayed.read_at == first.read_at

    assert len(await stores.snapshots.list_for_address(ADDRESS)) == 1


# ------------------------------------------------------------------- registry


async def test_a_published_version_is_immutable(stores: Stores) -> None:
    await stores.registry.publish(_descriptor("1.0.0"))
    await stores.registry.publish(_descriptor("1.0.0"))  # identical: a no-op

    with pytest.raises(AppendOnlyViolationError) as excinfo:
        await stores.registry.publish(_descriptor("1.0.0", summary="Something else entirely."))
    # A version somebody pinned must not turn into different code underneath them.
    assert excinfo.value.http_status == 409


async def test_a_pinned_version_stays_pinned_when_a_newer_one_is_published(
    stores: Stores,
) -> None:
    await stores.registry.publish(_descriptor("1.0.0"))
    await stores.registry.subscribe(
        Subscription(
            subscription_id="sub-1",
            subscriber_department=Department.FIRE,
            agent_id="records-watcher",
            pinned_version="1.0.0",
            subscribed_at=NOW,
        )
    )

    await stores.registry.publish(_descriptor("1.1.0", summary="Now with hazmat metadata."))
    await stores.registry.publish(_descriptor("2.0.0", summary="Rewritten extraction."))

    resolved = await stores.registry.resolve_pinned(str(Department.FIRE), "records-watcher")
    assert resolved is not None
    assert resolved.version == "1.0.0"
    assert len(await stores.registry.list_agents()) == 3


async def test_subscribing_to_an_unpublished_version_is_refused(stores: Stores) -> None:
    from firstdue.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await stores.registry.subscribe(
            Subscription(
                subscription_id="sub-1",
                subscriber_department=Department.FIRE,
                agent_id="records-watcher",
                pinned_version="9.9.9",
                subscribed_at=NOW,
            )
        )


# ---------------------------------------------------------------------- locks


@pytest.mark.concurrency
async def test_a_lock_is_held_by_exactly_one_owner(stores: Stores) -> None:
    lease = timedelta(minutes=5)
    first = await stores.locks.acquire("district:03", owner="instance-a", now=NOW, lease=lease)
    second = await stores.locks.acquire("district:03", owner="instance-b", now=NOW, lease=lease)

    assert first is not None
    assert second is None
    assert first.fence == 1


@pytest.mark.concurrency
async def test_simultaneous_contenders_produce_exactly_one_holder(stores: Stores) -> None:
    """A lock nobody wins is a livelock, not a lock.

    This is the liveness property a lock *does* promise, and it is the opposite
    of the safety property above. Optimistic concurrency on a profile may
    legitimately abort every writer and leave them to retry -- nothing is lost,
    because the next attempt recomputes the same result. A lock cannot do that:
    every contender standing down means the work behind it never happens at
    all. Two Cloud Run instances polling one district would both decline and
    the profile would go unmaterialized until the next scheduler tick.

    The Firestore implementation used to read "my transaction exhausted its
    attempts" as "somebody else holds it", which under real contention made
    *every* contender lose. Exhaustion now re-reads the document and asks
    whether anybody actually holds it.

    Eight contenders, because that is where the old behaviour reproduced: at
    two it livelocked about a third of the time and at eight about half, so a
    smaller number would be a test that passed either way and protected
    nothing.
    """
    lease = timedelta(minutes=5)

    async def contend(name: str) -> object | None:
        return await stores.locks.acquire("district:contended", owner=name, now=NOW, lease=lease)

    results = await asyncio.gather(*(contend(f"instance-{i}") for i in range(8)))
    holders = [lease_ for lease_ in results if lease_ is not None]

    assert len(holders) == 1, "a lock that nobody acquires is a livelock"
    assert holders[0].fence == 1


@pytest.mark.concurrency
async def test_an_expired_lock_is_reclaimable_and_the_fence_advances(stores: Stores) -> None:
    lease = timedelta(minutes=5)
    first = await stores.locks.acquire("district:03", owner="instance-a", now=NOW, lease=lease)
    assert first is not None

    later = NOW + timedelta(minutes=6)
    second = await stores.locks.acquire("district:03", owner="instance-b", now=later, lease=lease)
    assert second is not None
    assert second.owner == "instance-b"
    # The fence is what makes the dead holder's late write recognisable.
    assert second.fence == first.fence + 1


async def test_only_the_holder_releases_or_renews(stores: Stores) -> None:
    lease = timedelta(minutes=5)
    held = await stores.locks.acquire("district:03", owner="instance-a", now=NOW, lease=lease)
    assert held is not None

    assert await stores.locks.release("district:03", owner="instance-b") is False
    assert await stores.locks.renew("district:03", owner="instance-b", now=NOW, lease=lease) is None

    renewed = await stores.locks.renew(
        "district:03", owner="instance-a", now=NOW + timedelta(minutes=1), lease=lease
    )
    assert renewed is not None
    assert renewed.expires_at > held.expires_at
    assert renewed.fence == held.fence  # same holder, same fence

    assert await stores.locks.release("district:03", owner="instance-a") is True
    assert await stores.locks.get("district:03") is None


async def test_a_released_lock_does_not_reuse_its_fence(stores: Stores) -> None:
    lease = timedelta(minutes=5)
    first = await stores.locks.acquire("district:03", owner="instance-a", now=NOW, lease=lease)
    assert first is not None
    await stores.locks.release("district:03", owner="instance-a")
    second = await stores.locks.acquire("district:03", owner="instance-b", now=NOW, lease=lease)
    assert second is not None
    assert second.fence == first.fence + 1


# --------------------------------------------------------------- idempotency


def _claim(
    key: str, *, scope: str = "records-watcher", payload: object = "body"
) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=key,
        scope=scope,
        request_hash=request_hash(payload),
        claimed_at=NOW,
        claim_expires_at=NOW + DEFAULT_CLAIM_TTL,
    )


@pytest.mark.idempotency
async def test_a_duplicate_key_replays_instead_of_executing_twice(stores: Stores) -> None:
    first = await stores.idempotency.claim(_claim("dedupe-key-0001"))
    assert first.outcome is IdempotencyOutcome.FRESH
    assert first.should_execute

    # A second worker arrives while the first is still running.
    second = await stores.idempotency.claim(_claim("dedupe-key-0001"))
    assert second.outcome is IdempotencyOutcome.IN_PROGRESS
    assert not second.should_execute

    await stores.idempotency.complete(
        "records-watcher", "dedupe-key-0001", at=NOW, result_ref="fact-1"
    )

    third = await stores.idempotency.claim(_claim("dedupe-key-0001"))
    assert third.outcome is IdempotencyOutcome.REPLAY
    assert third.record.result_ref == "fact-1"


@pytest.mark.idempotency
async def test_the_same_key_with_a_different_body_is_a_conflict(stores: Stores) -> None:
    await stores.idempotency.claim(_claim("dedupe-key-0002", payload="one"))
    with pytest.raises(IdempotencyMismatchError) as excinfo:
        await stores.idempotency.claim(_claim("dedupe-key-0002", payload="two"))
    assert excinfo.value.http_status == 409


@pytest.mark.idempotency
async def test_the_same_key_in_a_different_scope_is_a_different_claim(stores: Stores) -> None:
    """Two consumers of one event must each get to act on it once."""
    first = await stores.idempotency.claim(_claim("dedupe-key-0003", scope="conflict-detector"))
    second = await stores.idempotency.claim(_claim("dedupe-key-0003", scope="survey-ranker"))
    assert first.should_execute
    assert second.should_execute


@pytest.mark.idempotency
async def test_an_abandoned_claim_is_reclaimable(stores: Stores) -> None:
    await stores.idempotency.claim(_claim("dedupe-key-0004"))
    later = NOW + DEFAULT_CLAIM_TTL + timedelta(minutes=1)
    retry = IdempotencyRecord(
        key="dedupe-key-0004",
        scope="records-watcher",
        request_hash=request_hash("body"),
        claimed_at=later,
        claim_expires_at=later + DEFAULT_CLAIM_TTL,
    )
    claim = await stores.idempotency.claim(retry)
    assert claim.outcome is IdempotencyOutcome.FRESH


@pytest.mark.idempotency
async def test_a_duplicate_external_write_resolves_to_one_action(stores: Stores) -> None:
    action = WriteAction(
        action_id="action-1",
        agent_id="referral-clerk",
        agent_version="1.0.0",
        target="building-referral-intake",
        receiving_department=Department.BUILDING,
        operation=Operation.WRITE,
        idempotency_key="referral-key-0001",
        payload_hash="a" * 16,
        intent="File a referral for an unpermitted storey.",
        compensating_action="Withdraw the referral.",
        status=WriteActionStatus.DRAFTED,
        created_at=NOW,
    )
    await stores.write_actions.record(action)
    await stores.write_actions.record(action)

    found = await stores.write_actions.find_by_idempotency_key(
        "building-referral-intake", "referral-key-0001"
    )
    assert found is not None
    assert found.action_id == "action-1"


# ---------------------------------------------------------------- agent runs


async def test_a_run_reaches_a_terminal_state_and_cannot_leave_it(stores: Stores) -> None:
    await stores.runs.start(_run())
    running = await stores.runs.get("run-1")
    assert running is not None
    await stores.runs.save(running.running())

    current = await stores.runs.get("run-1")
    assert current is not None
    finished = current.finished(AgentRunStatus.COMPLETED, at=NOW + timedelta(seconds=4))
    await stores.runs.save(finished)

    with pytest.raises(ValidationError):
        await stores.runs.save(finished.model_copy(update={"error_code": "LATE_WRITE"}))


async def test_checkpoints_are_appended_and_resumable(stores: Stores) -> None:
    await stores.runs.start(_run())
    for sequence in range(3):
        await stores.runs.checkpoint(
            "run-1",
            RunCheckpoint(
                checkpoint_id=f"cp-{sequence}",
                sequence=sequence,
                taken_at=NOW,
                stage="poll:sf-permits",
                cursor=str((sequence + 1) * 50),
                items_done=(sequence + 1) * 50,
            ),
        )

    with pytest.raises(AppendOnlyViolationError):
        await stores.runs.checkpoint(
            "run-1",
            RunCheckpoint(
                checkpoint_id="cp-out-of-order",
                sequence=1,
                taken_at=NOW,
                stage="poll:sf-permits",
            ),
        )

    run = await stores.runs.get("run-1")
    assert run is not None
    failed = run.finished(
        AgentRunStatus.FAILED,
        at=NOW + timedelta(minutes=2),
        error_code="SOURCE_UNAVAILABLE",
    )
    await stores.runs.save(failed)

    resumable = await stores.runs.list_resumable()
    assert [r.run_id for r in resumable] == ["run-1"]
    resume_point = resumable[0].resume_point
    assert resume_point is not None
    assert resume_point.cursor == "150"


async def test_a_run_is_findable_by_the_event_that_caused_it(stores: Stores) -> None:
    await stores.runs.start(_run(key="dispatch-key-0001"))
    found = await stores.runs.find_by_idempotency_key("dispatch-key-0001")
    assert found is not None
    assert found.run_id == "run-1"


# ------------------------------------------------------------- compensations


async def test_a_compensation_is_recorded_once_and_stays_outstanding(stores: Stores) -> None:
    compensation = CompensationRecord(
        compensation_id="comp-1",
        action_id="action-1",
        run_id="run-1",
        target="building-referral-intake",
        compensating_action="Withdraw the referral.",
        idempotency_key="withdraw-key-0001",
        reason="The run failed after filing.",
        recorded_at=NOW,
    )
    await stores.compensations.record(compensation)
    await stores.compensations.record(compensation)

    outstanding = await stores.compensations.list_outstanding()
    assert [c.compensation_id for c in outstanding] == ["comp-1"]

    await stores.compensations.save(
        compensation.executed(at=NOW + timedelta(minutes=1), external_ref="REF-9001")
    )
    assert await stores.compensations.list_outstanding() == []
    assert len(await stores.compensations.list_for_action("action-1")) == 1


# ------------------------------------------------------ audit and decisions


def _decision(decision_id: str, *, incident_id: str | None = None) -> PolicyDecision:
    return PolicyDecision(
        decision_id=decision_id,
        incident_id=incident_id,
        agent_id="incident-controller",
        agent_version="1.0.0",
        target="ems-derived",
        operation=Operation.READ,
        classification=Classification.PHI,
        action=PolicyAction.DERIVE,
        rule_id="phi-derive-only",
        justification="Occupant mobility summarised; the record itself is never released.",
        policy_version="1",
        decided_at=NOW,
        derivation_function="summarise_mobility",
    )


async def test_policy_decisions_are_stored_and_filtered(stores: Stores) -> None:
    await stores.audit.record_decision(_decision("dec-1", incident_id="inc-1"))
    await stores.audit.record_decision(_decision("dec-2", incident_id="inc-2"))

    for_incident = await stores.audit.list_decisions(incident_id="inc-1")
    assert [d.decision_id for d in for_incident] == ["dec-1"]

    by_agent = await stores.audit.list_decisions(agent_id="incident-controller")
    assert len(by_agent) == 2
    # The constant that makes "no model decides" checkable in the record itself.
    assert all(d.decided_by == "deterministic-policy-engine" for d in by_agent)


@pytest.mark.invariant
async def test_audit_detail_is_redacted_on_the_way_in(stores: Stores) -> None:
    """A record that never held contents cannot leak them later."""
    await stores.audit.record_event(
        AuditEvent(
            audit_id="audit-1",
            kind=AuditEventKind.WRITE_EXECUTED,
            occurred_at=NOW,
            actor="referral-clerk",
            actor_version="1.0.0",
            target="building-referral-intake",
            incident_id="inc-1",
            correlation_id="corr-1",
            detail={
                "document_text": "Occupant J. Marsh, apt 3, reports a blocked stairwell",
                "case_number": "REF-9001",
            },
        )
    )
    stored = await stores.audit.list_events(incident_id="inc-1")
    assert len(stored) == 1
    assert stored[0].detail["document_text"] == "[REDACTED]"
    # Field names and identifiers survive; contents do not.
    assert stored[0].detail["case_number"] == "REF-9001"


async def test_audit_events_are_filtered_by_kind_and_limited(stores: Stores) -> None:
    for index in range(3):
        await stores.audit.record_event(
            AuditEvent(
                audit_id=f"audit-{index}",
                kind=AuditEventKind.DEAD_LETTERED,
                occurred_at=NOW + timedelta(seconds=index),
                actor="conflict-detector",
                correlation_id="corr-1",
            )
        )
    await stores.audit.record_event(
        AuditEvent(
            audit_id="audit-write",
            kind=AuditEventKind.WRITE_EXECUTED,
            occurred_at=NOW,
            actor="referral-clerk",
            correlation_id="corr-1",
        )
    )

    dead = await stores.audit.list_events(kind=AuditEventKind.DEAD_LETTERED)
    assert len(dead) == 3
    assert len(await stores.audit.list_events(kind=AuditEventKind.DEAD_LETTERED, limit=2)) == 2
