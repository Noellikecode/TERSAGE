"""The deterministic conflict engine.

The rule the product is built around gets the most attention here: when the
permit says two storeys and the lidar measures three, the engine must surface
the disagreement, keep both facts, and cite what it used to decide -- and it
must do so identically on every run, forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.conflict_engine import (
    DEFAULT_REGISTRY,
    AuthoritativeSourceDisagreement,
    ConflictFinding,
    PermitVersusLidarStoryCount,
    RuleContext,
    RuleRegistry,
    SurveyContradictsRecord,
    conflict_id_for,
    detect,
    new_conflicts,
    survey_resolutions,
    values_disagree,
)
from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.enums import Classification, SourceType, SurveyOutcome
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.values import BooleanValue, EnumValue, IntegerValue, UnknownValue
from firstdue.domain.work import SurveyRecord
from firstdue.errors import ValidationError

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"


def _fact(
    fact_id: str,
    *,
    value: object,
    source_type: SourceType,
    key: str = Keys.STORIES,
    days_ago: int = 100,
    survey_id: str | None = None,
) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=key,
        value=value,  # type: ignore[arg-type]
        source_type=source_type,
        source_ref=f"{source_type.value.lower()}/ref",
        source_snapshot_id="snapshot-1",
        observed_at=NOW - timedelta(days=days_ago),
        ingested_at=NOW - timedelta(days=days_ago - 1),
        confidence=0.9,
        classification=Classification.PUBLIC,
        human_verified=survey_id is not None,
        survey_id=survey_id,
    )


PERMIT_TWO = _fact("fact-permit", value=IntegerValue(integer=2), source_type=SourceType.PERMIT)
LIDAR_THREE = _fact(
    "fact-lidar", value=IntegerValue(integer=3), source_type=SourceType.LIDAR_DSM, days_ago=10
)


# ------------------------------------------------- the permit-vs-lidar rule


@pytest.mark.invariant
def test_a_permit_that_disagrees_with_lidar_is_a_conflict() -> None:
    findings = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == PermitVersusLidarStoryCount.rule_id
    assert finding.canonical_key == Keys.STORIES
    assert finding.severity == 4
    assert finding.summary == "Permit records 2 storeys; lidar DSM measures 3."


@pytest.mark.invariant
def test_the_finding_cites_the_rule_and_both_fact_ids() -> None:
    """An officer must be able to open both documents the engine compared."""
    finding = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)[0]
    assert set(finding.fact_ids) == {"fact-permit", "fact-lidar"}
    conflict = finding.to_conflict(detected_at=NOW)
    assert conflict.rule_id == PermitVersusLidarStoryCount.rule_id
    assert set(conflict.fact_ids) == {"fact-permit", "fact-lidar"}


@pytest.mark.invariant
def test_both_conflicting_facts_are_preserved_and_neither_is_superseded() -> None:
    """Disagreement is signal. Nothing is averaged and nothing is dropped."""
    findings = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)
    cited = set(findings[0].fact_ids)
    assert cited == {PERMIT_TWO.fact_id, LIDAR_THREE.fact_id}
    assert PERMIT_TWO.is_active and LIDAR_THREE.is_active
    assert PERMIT_TWO.superseded_by is None and LIDAR_THREE.superseded_by is None


def test_a_two_storey_discrepancy_is_more_severe_than_one() -> None:
    lidar_four = _fact(
        "fact-lidar-4", value=IntegerValue(integer=4), source_type=SourceType.LIDAR_DSM, days_ago=5
    )
    assert detect(ADDRESS, [PERMIT_TWO, lidar_four], now=NOW)[0].severity == 5


def test_agreement_produces_no_conflict() -> None:
    lidar_two = _fact(
        "fact-lidar-2", value=IntegerValue(integer=2), source_type=SourceType.LIDAR_DSM, days_ago=5
    )
    assert detect(ADDRESS, [PERMIT_TWO, lidar_two], now=NOW) == ()


def test_an_unknown_value_is_not_a_disagreement() -> None:
    """UNKNOWN means "no record found", which contradicts nothing."""
    lidar_unknown = _fact(
        "fact-lidar-u",
        value=UnknownValue(checked_sources=("usgs-3dep",)),
        source_type=SourceType.LIDAR_DSM,
        days_ago=5,
    )
    assert detect(ADDRESS, [PERMIT_TWO, lidar_unknown], now=NOW) == ()


def test_a_superseded_fact_takes_no_part() -> None:
    amended = PERMIT_TWO.supersede(by_fact_id="fact-permit-amended")
    assert detect(ADDRESS, [amended, LIDAR_THREE], now=NOW) == ()


def test_only_one_side_present_is_not_a_conflict() -> None:
    assert detect(ADDRESS, [PERMIT_TWO], now=NOW) == ()
    assert detect(ADDRESS, [LIDAR_THREE], now=NOW) == ()


# ------------------------------------------------------------- determinism


@pytest.mark.invariant
def test_conflict_ids_are_derived_so_re_detection_is_idempotent() -> None:
    first = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)[0]
    later = detect(ADDRESS, [LIDAR_THREE, PERMIT_TWO], now=NOW + timedelta(days=30))[0]
    assert first.conflict_id == later.conflict_id


def test_the_conflict_id_changes_when_the_facts_do() -> None:
    a = conflict_id_for("rule", ADDRESS, Keys.STORIES, ["fact-a", "fact-b"])
    b = conflict_id_for("rule", ADDRESS, Keys.STORIES, ["fact-a", "fact-c"])
    assert a != b


def test_an_already_recorded_conflict_is_not_recorded_twice() -> None:
    findings = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)
    first = new_conflicts(findings, [], detected_at=NOW)
    assert len(first) == 1
    assert new_conflicts(findings, first, detected_at=NOW) == ()


def test_findings_are_ordered_most_severe_first() -> None:
    sprinklered_a = _fact(
        "fact-insp",
        value=BooleanValue(boolean=True),
        source_type=SourceType.FIRE_INSPECTION,
        key=Keys.SUPPRESSION_SPRINKLERED,
    )
    sprinklered_b = _fact(
        "fact-permit-s",
        value=BooleanValue(boolean=False),
        source_type=SourceType.PERMIT,
        key=Keys.SUPPRESSION_SPRINKLERED,
        days_ago=50,
    )
    findings = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE, sprinklered_a, sprinklered_b], now=NOW)
    assert [f.severity for f in findings] == sorted((f.severity for f in findings), reverse=True)


# --------------------------------------------------------- the rule registry


def test_the_default_registry_holds_the_built_in_rules() -> None:
    assert DEFAULT_REGISTRY.rule_ids == (
        AuthoritativeSourceDisagreement.rule_id,
        PermitVersusLidarStoryCount.rule_id,
        SurveyContradictsRecord.rule_id,
    )


def test_a_new_rule_can_be_registered_without_touching_the_engine() -> None:
    class RoofTypeChanged:
        rule_id = "roof-type-changed"

        def evaluate(self, context: RuleContext) -> tuple[ConflictFinding, ...]:
            facts = context.known(Keys.ROOF_TYPE)
            if len(facts) < 2:
                return ()
            return (
                ConflictFinding(
                    rule_id=self.rule_id,
                    address_id=context.address_id,
                    canonical_key=Keys.ROOF_TYPE,
                    severity=2,
                    fact_ids=tuple(sorted(f.fact_id for f in facts)),
                    summary="Roof type has more than one filed value.",
                ),
            )

    registry = RuleRegistry([RoofTypeChanged()])
    roof_a = _fact(
        "fact-roof-a",
        value=EnumValue(term="gable", vocabulary="roof"),
        source_type=SourceType.PERMIT,
        key=Keys.ROOF_TYPE,
    )
    roof_b = _fact(
        "fact-roof-b",
        value=EnumValue(term="flat", vocabulary="roof"),
        source_type=SourceType.ASSESSOR,
        key=Keys.ROOF_TYPE,
        days_ago=20,
    )
    findings = detect(ADDRESS, [roof_a, roof_b], now=NOW, registry=registry)
    assert [f.rule_id for f in findings] == ["roof-type-changed"]


def test_registering_the_same_rule_id_twice_is_an_error_not_a_silent_swap() -> None:
    registry = RuleRegistry([PermitVersusLidarStoryCount()])
    with pytest.raises(ValidationError):
        registry.register(PermitVersusLidarStoryCount())
    # Versioning a rule up is deliberate and explicit.
    registry.replace(PermitVersusLidarStoryCount())


def test_a_fact_for_a_different_address_is_refused() -> None:
    stray = PERMIT_TWO.model_copy(update={"address_id": "sf-1215-fell"})
    with pytest.raises(ValidationError):
        detect(ADDRESS, [PERMIT_TWO, stray], now=NOW)


# --------------------------------------------------- the other built-in rules


def test_two_filed_records_that_disagree_are_a_conflict() -> None:
    permit = _fact(
        "fact-permit-occ",
        value=EnumValue(term="residential", vocabulary="occupancy"),
        source_type=SourceType.PERMIT,
        key=Keys.OCCUPANCY_TYPE,
    )
    assessor = _fact(
        "fact-assessor-occ",
        value=EnumValue(term="mixed-use", vocabulary="occupancy"),
        source_type=SourceType.ASSESSOR,
        key=Keys.OCCUPANCY_TYPE,
        days_ago=40,
    )
    finding = detect(ADDRESS, [permit, assessor], now=NOW)[0]
    assert finding.rule_id == AuthoritativeSourceDisagreement.rule_id
    # Occupancy type is something a crew acts on inside the building.
    assert finding.severity == 3
    assert set(finding.fact_ids) == {"fact-permit-occ", "fact-assessor-occ"}


def test_case_and_spacing_are_not_a_disagreement() -> None:
    a = _fact(
        "fact-a",
        value=EnumValue(term="Wood-Frame", vocabulary="iso-construction"),
        source_type=SourceType.PERMIT,
        key=Keys.CONSTRUCTION_TYPE,
    )
    b = _fact(
        "fact-b",
        value=EnumValue(term="wood-frame", vocabulary="iso-construction"),
        source_type=SourceType.ASSESSOR,
        key=Keys.CONSTRUCTION_TYPE,
        days_ago=20,
    )
    assert not values_disagree(a, b)
    assert detect(ADDRESS, [a, b], now=NOW) == ()


def test_a_survey_that_contradicts_the_file_is_a_conflict() -> None:
    survey_fact = _fact(
        "fact-survey",
        value=BooleanValue(boolean=True),
        source_type=SourceType.HUMAN_SURVEY,
        key=Keys.LIGHTWEIGHT_TRUSS,
        days_ago=2,
        survey_id="survey-1",
    )
    filed = _fact(
        "fact-inspection",
        value=BooleanValue(boolean=False),
        source_type=SourceType.FIRE_INSPECTION,
        key=Keys.LIGHTWEIGHT_TRUSS,
        days_ago=400,
    )
    finding = detect(ADDRESS, [survey_fact, filed], now=NOW)[0]
    assert finding.rule_id == SurveyContradictsRecord.rule_id
    assert finding.severity == 4


# ------------------------------------------------------ human survey override


@pytest.mark.invariant
def test_only_a_survey_of_the_attribute_closes_its_conflict() -> None:
    conflict = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)[0].to_conflict(detected_at=NOW)
    survey = SurveyRecord(
        survey_id="survey-1",
        address_id=ADDRESS,
        company="E-05",
        surveyor="capt-alvarez",
        started_at=NOW,
        completed_at=NOW + timedelta(hours=1),
        outcome=SurveyOutcome.COMPLETED,
        verified_keys=(Keys.STORIES,),
    )
    resolutions = survey_resolutions(
        [conflict], survey, resolving_fact_ids={Keys.STORIES: "fact-survey-stories"}
    )
    assert len(resolutions) == 1
    conflict_id, resolution = resolutions[0]
    assert conflict_id == conflict.conflict_id
    assert resolution.resolving_record_id == "survey-1"
    assert resolution.resolving_fact_id == "fact-survey-stories"
    assert resolution.resolved_by == "capt-alvarez"

    resolved = conflict.resolve(resolution)
    assert resolved.status is ConflictStatus.RESOLVED
    # Both original facts are still cited on the resolved conflict.
    assert set(resolved.fact_ids) == {"fact-permit", "fact-lidar"}


def test_a_survey_of_a_different_attribute_leaves_the_conflict_open() -> None:
    conflict = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)[0].to_conflict(detected_at=NOW)
    survey = SurveyRecord(
        survey_id="survey-2",
        address_id=ADDRESS,
        company="E-05",
        surveyor="capt-alvarez",
        started_at=NOW,
        completed_at=NOW + timedelta(hours=1),
        outcome=SurveyOutcome.COMPLETED,
        verified_keys=(Keys.SUPPRESSION_SPRINKLERED,),
    )
    assert survey_resolutions([conflict], survey, resolving_fact_ids={}) == ()


def test_a_survey_that_could_not_get_in_resolves_nothing() -> None:
    conflict = detect(ADDRESS, [PERMIT_TWO, LIDAR_THREE], now=NOW)[0].to_conflict(detected_at=NOW)
    survey = SurveyRecord(
        survey_id="survey-3",
        address_id=ADDRESS,
        company="E-05",
        surveyor="capt-alvarez",
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=10),
        outcome=SurveyOutcome.NO_ACCESS,
        verified_keys=(),
    )
    assert survey_resolutions([conflict], survey, resolving_fact_ids={Keys.STORIES: "fact-x"}) == ()
