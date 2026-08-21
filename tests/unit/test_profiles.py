"""Append-only timelines and optimistic concurrency."""

from __future__ import annotations

from datetime import timedelta

import pytest

from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.values import IntegerValue
from firstdue.errors import AppendOnlyViolationError, StaleVersionError, ValidationError

pytestmark = pytest.mark.invariant


def _event(sequence: int, occurred_at, summary: str = "recorded") -> ProfileEvent:
    return ProfileEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        occurred_at=occurred_at,
        type=ProfileEventType.FACT_WRITTEN,
        actor="test",
        summary=summary,
    )


@pytest.fixture
def profile() -> BuildingProfile:
    return BuildingProfile(address_id="sf-0450-hayes", district_id="sffd-district-03")


def test_new_profile_starts_at_version_zero(profile: BuildingProfile) -> None:
    assert profile.profile_version == 0
    assert profile.timeline == ()


def test_appending_an_event_bumps_the_version(profile: BuildingProfile, epoch) -> None:
    updated = profile.append_event(_event(0, epoch))
    assert updated.profile_version == 1
    assert len(updated.timeline) == 1


def test_timeline_rejects_a_sequence_gap(profile: BuildingProfile, epoch) -> None:
    with pytest.raises(AppendOnlyViolationError):
        profile.append_event(_event(3, epoch))


def test_timeline_rejects_a_replayed_sequence(profile: BuildingProfile, epoch) -> None:
    once = profile.append_event(_event(0, epoch))
    with pytest.raises(AppendOnlyViolationError):
        once.append_event(_event(0, epoch))


def test_a_profile_with_a_gapped_timeline_cannot_be_constructed(epoch) -> None:
    with pytest.raises(AppendOnlyViolationError):
        BuildingProfile(
            address_id="a",
            district_id="d",
            timeline=(_event(0, epoch), _event(2, epoch)),
        )


def test_history_is_never_rewritten(profile: BuildingProfile, epoch) -> None:
    first = profile.append_event(_event(0, epoch, "first"))
    second = first.append_event(_event(1, epoch, "second"))
    assert second.timeline[0].summary == "first"
    assert first.timeline == (second.timeline[0],)


def test_check_version_rejects_a_stale_write(profile: BuildingProfile, epoch) -> None:
    current = profile.append_event(_event(0, epoch))
    with pytest.raises(StaleVersionError) as excinfo:
        current.check_version(0)
    assert excinfo.value.http_status == 409


def test_with_fact_records_fact_and_event(profile: BuildingProfile, make_fact, epoch) -> None:
    fact = make_fact()
    updated = profile.with_fact(fact, event=_event(0, epoch))
    assert updated.profile_version == 1
    assert updated.facts[fact.canonical_key].fact_id == fact.fact_id


def test_conflicting_facts_both_persist_on_the_profile(
    profile: BuildingProfile, make_fact, epoch
) -> None:
    from firstdue.domain.enums import SourceType

    permit = make_fact(value=IntegerValue(integer=2))
    lidar = make_fact(value=IntegerValue(integer=3), source_type=SourceType.LIDAR_DSM)
    updated = profile.with_fact(permit, event=_event(0, epoch)).with_fact(
        lidar, event=_event(1, epoch)
    )
    stored = updated.fact_sets[permit.canonical_key]
    assert len(stored.facts) == 2
    # The resolved view shows one; the store keeps both.
    assert len(updated.facts) == 1


def test_a_fact_for_another_address_is_rejected(profile: BuildingProfile, make_fact, epoch) -> None:
    with pytest.raises(ValidationError):
        profile.with_fact(make_fact(address_id="sf-1215-fell"), event=_event(0, epoch))


def test_snapshot_freezes_the_profile(profile: BuildingProfile, make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch - timedelta(days=365))
    populated = profile.with_fact(fact, event=_event(0, epoch))
    snapshot = populated.snapshot(snapshot_id="snap-1", read_at=epoch)
    assert snapshot.profile_version == populated.profile_version
    assert snapshot.snapshot_id == "snap-1"
    assert snapshot.facts[fact.canonical_key].fact_id == fact.fact_id
    # Staleness is computed deterministically for every resolved attribute.
    assert 0.0 <= snapshot.staleness[fact.canonical_key] <= 1.0


def test_empty_profile_snapshot_is_a_cold_start(profile: BuildingProfile, epoch) -> None:
    snapshot = profile.snapshot(snapshot_id="snap-cold", read_at=epoch)
    assert snapshot.is_cold_start is True


def test_populated_snapshot_is_not_a_cold_start(profile: BuildingProfile, make_fact, epoch) -> None:
    populated = profile.with_fact(make_fact(), event=_event(0, epoch))
    assert populated.snapshot(snapshot_id="s", read_at=epoch).is_cold_start is False
