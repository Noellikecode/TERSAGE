"""Replay equivalence and snapshot stability.

The claim under test: *replaying identical events produces equivalent
materialized state.* Because events carry only identifiers and consumers re-read
the store, replay reduces to running the materializer twice over the same facts
-- and the second pass must change nothing at all. Not "nothing important":
nothing. Same conflicts, same decay, same version, same content hash.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.conflict_engine import PermitVersusLidarStoryCount
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.materialize import materialize, recompute_decay, timeline_event_id
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.snapshots import (
    profile_content_hash,
    snapshot_id_for,
    stable_snapshot_id,
)
from firstdue.domain.values import IntegerValue

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
ACTOR = "conflict-detector"
ACTOR_VERSION = "1.0.0"


def _fact(fact_id: str, *, stories: int, source_type: SourceType, days_ago: int) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        value=IntegerValue(integer=stories),
        source_type=source_type,
        source_ref="ref",
        source_snapshot_id="snapshot-1",
        observed_at=NOW - timedelta(days=days_ago),
        ingested_at=NOW - timedelta(days=days_ago - 1),
        confidence=0.9,
        classification=Classification.PUBLIC,
    )


def _profile_with_disagreement() -> BuildingProfile:
    profile = BuildingProfile(address_id=ADDRESS, district_id="sffd-district-03")
    for index, fact in enumerate(
        (
            _fact("fact-permit", stories=2, source_type=SourceType.PERMIT, days_ago=2870),
            _fact("fact-lidar", stories=3, source_type=SourceType.LIDAR_DSM, days_ago=410),
        )
    ):
        profile = profile.with_fact(
            fact,
            event=ProfileEvent(
                event_id=f"evt-{index}",
                sequence=profile.next_sequence,
                occurred_at=fact.ingested_at,
                type=ProfileEventType.FACT_WRITTEN,
                actor="records-watcher",
                summary="Recorded storey count.",
                fact_ids=(fact.fact_id,),
            ),
        )
    return profile


def test_materializing_detects_the_conflict_and_records_it_on_the_timeline() -> None:
    result = materialize(
        _profile_with_disagreement(), now=NOW, actor=ACTOR, actor_version=ACTOR_VERSION
    )

    assert result.changed
    assert len(result.new_conflicts) == 1
    conflict = result.new_conflicts[0]
    assert conflict.rule_id == PermitVersusLidarStoryCount.rule_id

    last_event = result.profile.timeline[-1]
    assert last_event.type is ProfileEventType.CONFLICT_DETECTED
    assert last_event.conflict_id == conflict.conflict_id
    assert last_event.actor == ACTOR
    assert last_event.actor_version == ACTOR_VERSION


@pytest.mark.idempotency
def test_a_second_pass_over_the_same_facts_changes_nothing() -> None:
    """The redelivery case: at-least-once delivery, exactly-once effect."""
    first = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR)
    second = materialize(first.profile, now=NOW, actor=ACTOR)

    assert second.changed is False
    assert second.new_conflicts == ()
    assert second.profile.profile_version == first.profile.profile_version
    assert second.profile.content_hash == first.profile.content_hash
    assert len(second.profile.timeline) == len(first.profile.timeline)


@pytest.mark.idempotency
def test_replaying_the_same_events_produces_equivalent_state() -> None:
    """Two independent replays from the same facts land on the same bytes."""
    run_a = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR)
    run_b = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR)

    assert run_a.profile.content_hash == run_b.profile.content_hash
    assert [c.conflict_id for c in run_a.new_conflicts] == [
        c.conflict_id for c in run_b.new_conflicts
    ]
    assert run_a.decay == run_b.decay
    assert run_a.profile.timeline == run_b.profile.timeline


def test_timeline_event_ids_are_derived_so_a_replay_reuses_them() -> None:
    assert timeline_event_id(ADDRESS, "conflict-1") == timeline_event_id(ADDRESS, "conflict-1")
    assert timeline_event_id(ADDRESS, "conflict-1") != timeline_event_id(ADDRESS, "conflict-2")


def test_decay_is_recomputed_over_the_resolved_fact_per_attribute() -> None:
    profile = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR).profile
    decay = recompute_decay(profile, now=NOW)
    assert set(decay) == {Keys.STORIES}
    assert 0.0 <= decay[Keys.STORIES] <= 1.0
    assert profile.confidence_decay == decay


def test_decay_moves_when_time_does_and_that_is_a_versioned_write() -> None:
    first = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR)
    later = materialize(first.profile, now=NOW + timedelta(days=365), actor=ACTOR)

    assert later.changed
    assert later.decay[Keys.STORIES] < first.decay[Keys.STORIES]
    assert later.profile.profile_version == first.profile.profile_version + 1
    # No new conflict: the facts did not change, only the clock did.
    assert later.new_conflicts == ()


# ------------------------------------------------------------- snapshot ids


def test_a_snapshot_id_is_a_function_of_the_profile_version_and_content() -> None:
    profile = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR).profile

    early = profile.snapshot(read_at=NOW)
    late = profile.snapshot(read_at=NOW + timedelta(hours=6))
    assert early.snapshot_id == late.snapshot_id == snapshot_id_for(profile)
    assert early.snapshot_id == stable_snapshot_id(
        profile.address_id, profile.profile_version, content_hash=profile_content_hash(profile)
    )


def test_a_changed_profile_gets_a_different_snapshot_id() -> None:
    profile = materialize(_profile_with_disagreement(), now=NOW, actor=ACTOR).profile
    moved_on = materialize(profile, now=NOW + timedelta(days=365), actor=ACTOR).profile
    assert profile.snapshot(read_at=NOW).snapshot_id != moved_on.snapshot(read_at=NOW).snapshot_id


def test_an_explicit_snapshot_id_is_still_honoured() -> None:
    profile = _profile_with_disagreement()
    assert profile.snapshot(read_at=NOW, snapshot_id="snap-explicit").snapshot_id == (
        "snap-explicit"
    )
