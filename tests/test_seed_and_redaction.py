"""Deterministic demo state, decay, and redaction."""

from __future__ import annotations

from datetime import timedelta

import pytest

from firstdue.city.san_francisco import SanFranciscoAdapter
from firstdue.demo.seed import (
    COLD_START_ADDRESS_ID,
    DISPUTED_ADDRESS_ID,
    RECORDS_ONLY,
    build_seed,
    profiles_from_seed,
)
from firstdue.domain.conflict_engine import PermitVersusLidarStoryCount
from firstdue.domain.decay import decayed_confidence, staleness
from firstdue.domain.enums import SourceType
from firstdue.domain.keys import Keys
from firstdue.observability.redaction import redact_mapping, redact_text


@pytest.fixture
def seed_document(settings, epoch):
    city = SanFranciscoAdapter(settings.fixtures_dir)
    return build_seed(addresses=list(city.list_addresses()), epoch=epoch, seed=settings.demo_seed)


def test_seed_is_byte_identical_on_rebuild(settings, epoch) -> None:
    city = SanFranciscoAdapter(settings.fixtures_dir)
    first = build_seed(addresses=list(city.list_addresses()), epoch=epoch, seed=settings.demo_seed)
    second = build_seed(addresses=list(city.list_addresses()), epoch=epoch, seed=settings.demo_seed)
    assert first["content_hash"] == second["content_hash"]
    assert first == second


def test_a_different_seed_produces_different_state(settings, epoch) -> None:
    city = SanFranciscoAdapter(settings.fixtures_dir)
    a = build_seed(addresses=list(city.list_addresses()), epoch=epoch, seed="seed-a")
    b = build_seed(addresses=list(city.list_addresses()), epoch=epoch, seed="seed-b")
    assert a["content_hash"] != b["content_hash"]


def test_seeded_profiles_revalidate_every_invariant(seed_document, settings) -> None:
    profiles = profiles_from_seed(seed_document)
    # Every reference address gets a profile except the cold-start one, which is
    # deliberately left with nothing on record. Asserted as that relationship
    # rather than as a count: the address fixture is generated from the city's
    # real parcel feed and grows, and a literal here would fail on size while
    # saying nothing about the property it is guarding.
    city = SanFranciscoAdapter(settings.fixtures_dir)
    assert len(profiles) == len(list(city.list_addresses())) - len(RECORDS_ONLY)
    for profile in profiles:
        assert [e.sequence for e in profile.timeline] == list(range(len(profile.timeline)))


def test_the_disputed_address_keeps_both_stories_facts(seed_document) -> None:
    profiles = {p.address_id: p for p in profiles_from_seed(seed_document)}
    disputed = profiles[DISPUTED_ADDRESS_ID]
    stories = disputed.fact_sets[Keys.STORIES]
    assert len(stories.facts) == 2
    sources = {f.source_type for f in stories.facts}
    assert sources == {SourceType.PERMIT, SourceType.LIDAR_DSM}
    assert len(disputed.open_conflicts) == 1
    # The seed runs the production conflict engine, so the rule id on the demo
    # profile is the one the engine cites -- not a hand-written string.
    assert disputed.open_conflicts[0].rule_id == PermitVersusLidarStoryCount.rule_id


def test_the_disputed_level_is_marked_in_the_geometry(seed_document) -> None:
    profiles = {p.address_id: p for p in profiles_from_seed(seed_document)}
    geometry = profiles[DISPUTED_ADDRESS_ID].geometry
    assert geometry is not None
    assert geometry.has_disputed_mass is True
    assert geometry.collapse_zone_radius_m > 0


def test_the_cold_start_address_has_nothing_on_record(seed_document, epoch) -> None:
    profiles = {p.address_id: p for p in profiles_from_seed(seed_document)}
    cold = profiles[COLD_START_ADDRESS_ID]
    snapshot = cold.snapshot(snapshot_id="snap-cold", read_at=epoch)
    assert snapshot.is_cold_start is True


def test_a_missing_sprinkler_filing_is_unknown_not_false(seed_document) -> None:
    profiles = {p.address_id: p for p in profiles_from_seed(seed_document)}
    fact = profiles[DISPUTED_ADDRESS_ID].facts[Keys.SUPPRESSION_SPRINKLERED]
    assert fact.value.render() == "UNKNOWN"
    assert fact.is_known is False


def test_seed_is_marked_synthetic(seed_document) -> None:
    assert seed_document["synthetic"] is True


# ------------------------------------------------------------------ decay ---


def test_confidence_decays_with_age(make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch - timedelta(days=1), confidence=1.0)
    fresh = decayed_confidence(fact, now=epoch)
    old = decayed_confidence(fact, now=epoch + timedelta(days=1825))
    assert fresh > old


def test_intervening_events_reduce_confidence(make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch - timedelta(days=430), confidence=0.9)
    quiet = decayed_confidence(fact, now=epoch, events_since_observation=0)
    churned = decayed_confidence(fact, now=epoch, events_since_observation=2)
    assert churned < quiet


def test_decay_is_deterministic(make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch - timedelta(days=100))
    assert decayed_confidence(fact, now=epoch) == decayed_confidence(fact, now=epoch)


def test_decay_stays_in_range(make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch - timedelta(days=20000), confidence=1.0)
    value = decayed_confidence(fact, now=epoch)
    assert 0.0 <= value <= 1.0
    assert 0.0 <= staleness(fact, now=epoch) <= 1.0


def test_a_live_observation_decays_faster_than_a_filed_record(make_fact, epoch) -> None:
    observed_at = epoch - timedelta(days=2)
    thermal = make_fact(
        source_type=SourceType.THERMAL_SENSOR, observed_at=observed_at, confidence=1.0
    )
    permit = make_fact(source_type=SourceType.PERMIT, observed_at=observed_at, confidence=1.0)
    assert decayed_confidence(thermal, now=epoch) < decayed_confidence(permit, now=epoch)


def test_negative_event_counts_are_rejected(make_fact, epoch) -> None:
    with pytest.raises(ValueError, match="negative"):
        decayed_confidence(make_fact(), now=epoch, events_since_observation=-1)


# -------------------------------------------------------------- redaction ---


def test_sensitive_keys_are_redacted() -> None:
    result = redact_mapping({"document_text": "narrative", "api_key": "abc", "address_id": "sf-1"})
    assert result["document_text"] == "[REDACTED]"
    assert result["api_key"] == "[REDACTED]"
    # The address is the subject of the system and stays readable.
    assert result["address_id"] == "sf-1"


def test_sensitive_values_are_redacted_under_innocent_keys() -> None:
    result = redact_mapping({"note": "reach me at resident@example.com or 415-555-0142"})
    assert "example.com" not in result["note"]
    assert "555-0142" not in result["note"]


def test_bucket_uris_never_surface() -> None:
    assert "internal-plans" not in redact_text("failed reading gs://internal-plans/a.pdf")


def test_google_api_keys_are_redacted() -> None:
    fake_key = "AIza" + "B" * 35
    assert fake_key not in redact_text(f"key={fake_key}")


def test_nested_structures_are_redacted() -> None:
    result = redact_mapping({"outer": {"ssn": "123-45-6789", "ok": "value"}})
    assert result["outer"]["ssn"] == "[REDACTED]"
    assert result["outer"]["ok"] == "value"


def test_long_strings_are_bounded() -> None:
    assert len(redact_text("x" * 10_000)) < 2_200
