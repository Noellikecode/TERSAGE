"""Instant briefs are model-free, and nothing transmits before it is persisted."""

from __future__ import annotations

import pytest

from firstdue.domain.briefs import BriefEmission, BriefItem, BriefSection, BriefSectionKey
from firstdue.domain.enums import AssertionStatus, BriefStage
from firstdue.errors import BriefNotPersistedError, ValidationError

pytestmark = pytest.mark.invariant


def _emission(**overrides) -> BriefEmission:
    payload = {
        "emission_id": "em-1",
        "incident_id": "inc-1",
        "version": 1,
        "stage": BriefStage.INSTANT,
        "profile_snapshot_id": "snap-1",
        "produced_at": __import__("datetime").datetime(
            2026, 8, 20, 8, 0, tzinfo=__import__("datetime").UTC
        ),
    }
    payload.update(overrides)
    return BriefEmission(**payload)  # type: ignore[arg-type]


def test_instant_stage_cannot_invoke_a_model() -> None:
    with pytest.raises(ValidationError):
        _emission(model_invoked=True)


def test_instant_stage_cannot_carry_a_narrative() -> None:
    with pytest.raises(ValidationError):
        _emission(narrative="the building is...", narrative_available=True)


def test_instant_stage_is_always_version_one() -> None:
    with pytest.raises(ValidationError):
        _emission(version=2)


def test_enriched_stage_may_carry_a_narrative() -> None:
    emission = _emission(
        stage=BriefStage.ENRICHED,
        version=2,
        narrative="Two-storey wood-frame dwelling.",
        narrative_available=True,
        model_invoked=True,
    )
    assert emission.narrative_available is True


def test_narrative_flag_must_match_the_narrative() -> None:
    with pytest.raises(ValidationError):
        _emission(stage=BriefStage.ENRICHED, version=2, narrative_available=True)


def test_degraded_enriched_stage_lands_without_a_narrative() -> None:
    """If Vertex AI is down the brief still emits and says the narrative is
    unavailable."""
    emission = _emission(
        stage=BriefStage.ENRICHED,
        version=2,
        narrative=None,
        narrative_available=False,
        model_invoked=True,
    )
    assert emission.narrative_available is False


def test_transmission_requires_persistence() -> None:
    with pytest.raises(BriefNotPersistedError):
        _emission().require_persisted()


def test_persisted_emission_may_transmit(epoch) -> None:
    emission = _emission().mark_persisted(at=epoch)
    assert emission.require_persisted() is emission
    assert emission.persisted_at == epoch


def test_content_hash_is_stable_across_persistence(epoch) -> None:
    emission = _emission().sealed()
    before = emission.content_hash
    after = emission.mark_persisted(at=epoch).content_hash
    assert before == after != ""


def test_content_hash_changes_with_content(epoch) -> None:
    a = _emission().sealed()
    b = _emission(unknowns=("structure.stories",)).sealed()
    assert a.content_hash != b.content_hash


def test_gaps_are_reported() -> None:
    emission = _emission(unknowns=("suppression.sprinklered",))
    assert emission.has_gaps is True
    assert _emission().has_gaps is False


def test_sections_carry_status_per_item() -> None:
    emission = _emission(
        sections=(
            BriefSection(
                key=BriefSectionKey.CONSTRUCTION,
                items=(
                    BriefItem(
                        label="Stories",
                        value_render="2 / 3",
                        status=AssertionStatus.DISPUTED,
                    ),
                ),
            ),
        )
    )
    assert emission.sections[0].items[0].status is AssertionStatus.DISPUTED
