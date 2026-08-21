"""Provenance and human verification invariants."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from firstdue.domain.enums import Classification, SourceTier, SourceType
from firstdue.domain.facts import SourceSpan, StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.values import IntegerValue, UnknownValue
from firstdue.errors import (
    HumanVerificationForbiddenError,
    ProvenanceRequiredError,
    ValidationError,
)

pytestmark = pytest.mark.invariant


def test_fact_requires_source_ref(make_fact) -> None:
    with pytest.raises(ProvenanceRequiredError):
        make_fact(source_ref="   ")


def test_fact_requires_non_empty_snapshot(make_fact) -> None:
    with pytest.raises(ProvenanceRequiredError):
        make_fact(source_snapshot_id=" ")


def test_fact_cannot_omit_value(ids, epoch) -> None:
    """`value` is required, so "missing" is not an expressible state."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic missing field
        StructuralFact(  # type: ignore[call-arg]
            fact_id="f1",
            address_id="a1",
            canonical_key=Keys.STORIES,
            source_type=SourceType.PERMIT,
            source_ref="permit/1",
            source_snapshot_id="s1",
            observed_at=epoch,
            ingested_at=epoch,
            confidence=0.5,
            classification=Classification.PUBLIC,
        )


def test_extraction_cannot_set_human_verified(make_fact) -> None:
    with pytest.raises(HumanVerificationForbiddenError):
        make_fact(source_type=SourceType.PERMIT, human_verified=True)


def test_lidar_cannot_set_human_verified(make_fact) -> None:
    with pytest.raises(HumanVerificationForbiddenError):
        make_fact(source_type=SourceType.LIDAR_DSM, human_verified=True, survey_id="sv-1")


def test_survey_without_survey_id_cannot_verify(make_fact) -> None:
    with pytest.raises(HumanVerificationForbiddenError):
        make_fact(source_type=SourceType.HUMAN_SURVEY, human_verified=True, survey_id=None)


def test_survey_with_survey_id_may_verify(make_fact) -> None:
    fact = make_fact(source_type=SourceType.HUMAN_SURVEY, human_verified=True, survey_id="survey-1")
    assert fact.human_verified is True
    assert fact.tier is SourceTier.HUMAN_VERIFIED


def test_naive_timestamps_are_rejected(make_fact) -> None:
    with pytest.raises(ValidationError):
        make_fact(observed_at=datetime(2026, 1, 1))  # noqa: DTZ001 - that is the point


def test_extracted_document_value_requires_a_span(make_fact) -> None:
    """A model-produced value from a document must cite where it read it."""
    with pytest.raises(ProvenanceRequiredError):
        make_fact(
            source_type=SourceType.FIRE_INSPECTION,
            produced_by_agent="records-watcher",
            produced_by_version="1.0.0",
            extracted_by_model=True,
        )


def test_a_filed_column_is_not_an_extraction_and_needs_no_span(make_fact) -> None:
    """A number in a dataset column is a filing; there is no line to point at.

    The agent that read it is still recorded, so replay knows which version of
    which watcher wrote the fact.
    """
    fact = make_fact(
        source_type=SourceType.PERMIT,
        produced_by_agent="records-watcher",
        produced_by_version="1.0.0",
    )
    assert fact.source_span is None
    assert fact.extracted_by_model is False


def test_extracted_document_value_with_span_is_accepted(make_fact) -> None:
    fact = make_fact(
        source_type=SourceType.FIRE_INSPECTION,
        produced_by_agent="records-watcher",
        produced_by_version="1.0.0",
        extracted_by_model=True,
        source_span=SourceSpan(
            locator="inspection/1#p1", start_offset=0, end_offset=5, quoted_text="truss"
        ),
    )
    assert fact.source_span is not None


def test_unknown_extraction_needs_no_span(make_fact) -> None:
    """A model reporting UNKNOWN is not asserting anything, so no span is due."""
    fact = make_fact(
        value=UnknownValue(),
        source_type=SourceType.FIRE_INSPECTION,
        produced_by_agent="records-watcher",
        produced_by_version="1.0.0",
        extracted_by_model=True,
    )
    assert fact.is_known is False


def test_facts_are_frozen(make_fact) -> None:
    fact = make_fact()
    with pytest.raises(Exception):  # noqa: B017 - frozen model
        fact.confidence = 0.1  # type: ignore[misc]


def test_supersede_returns_a_new_fact(make_fact) -> None:
    original = make_fact()
    corrected = original.supersede(by_fact_id="fact-later")
    assert original.superseded_by is None
    assert corrected.superseded_by == "fact-later"
    assert corrected.is_active is False


def test_age_is_never_negative(make_fact, epoch) -> None:
    fact = make_fact(observed_at=epoch + timedelta(days=1))
    assert fact.age_seconds(epoch) == 0.0


def test_confidence_is_bounded(make_fact) -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic bound
        make_fact(confidence=1.5)


def test_canonical_key_must_be_dotted_lowercase(make_fact) -> None:
    with pytest.raises(Exception):  # noqa: B017 - pattern constraint
        make_fact(key="Structure Stories")


def test_value_union_is_discriminated(make_fact) -> None:
    fact = make_fact(value=IntegerValue(integer=4))
    round_tripped = StructuralFact.model_validate(fact.model_dump(mode="json"))
    assert round_tripped.value == IntegerValue(integer=4)
    assert round_tripped == fact
