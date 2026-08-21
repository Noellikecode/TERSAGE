"""Idempotency keys, approval records, and the renderable geometry spec."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.enums import (
    ApprovalThreshold,
    AssertionStatus,
    Department,
    FaceLabel,
    Operation,
    ReferralStatus,
    SourceType,
    SurveyOutcome,
    WriteActionStatus,
)
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    Obstruction,
    ObstructionType,
    RoofSegment,
    collapse_zone_radius,
)
from firstdue.domain.keys import Keys
from firstdue.domain.values import QuantityValue, UnscannedValue
from firstdue.domain.work import (
    ApprovalRequest,
    ApprovalStatus,
    RankReason,
    ReferralRecord,
    SurveyQueueEntry,
    SurveyRecord,
    WriteAction,
)
from firstdue.errors import ValidationError

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _action(**overrides) -> WriteAction:
    payload = {
        "action_id": "wa-1",
        "agent_id": "delta-ranker",
        "agent_version": "1.0.0",
        "target": "building-referral-intake",
        "receiving_department": Department.BUILDING,
        "operation": Operation.WRITE,
        "idempotency_key": "referral-sf0450hayes-c1",
        "payload_hash": "0123456789abcdef",
        "intent": "File an unpermitted-construction referral",
        "compensating_action": "withdraw_referral",
        "created_at": NOW,
    }
    payload.update(overrides)
    return WriteAction(**payload)  # type: ignore[arg-type]


@pytest.mark.idempotency
def test_a_write_cannot_exist_without_an_idempotency_key() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length 8
        _action(idempotency_key="")


@pytest.mark.idempotency
def test_short_idempotency_keys_are_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length 8
        _action(idempotency_key="abc")


def test_every_write_names_its_compensating_action() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length
        _action(compensating_action="")


def test_executed_write_records_when_it_executed() -> None:
    with pytest.raises(ValidationError):
        _action(status=WriteActionStatus.EXECUTED)
    ok = _action(status=WriteActionStatus.EXECUTED, executed_at=NOW)
    assert ok.executed_at == NOW


def test_approved_write_names_its_approval() -> None:
    with pytest.raises(ValidationError):
        _action(status=WriteActionStatus.APPROVED)


def test_approval_cannot_have_threshold_none() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            approval_id="ap-1",
            action_id="wa-1",
            threshold=ApprovalThreshold.NONE,
            receiving_department=Department.UTILITY,
            prefilled_summary="Request gas shutoff at 450 Hayes St",
            rule_id="commit-requires-approval",
            staged_at=NOW,
        )


def test_decided_approval_records_the_human() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            approval_id="ap-2",
            action_id="wa-1",
            threshold=ApprovalThreshold.SUPERVISOR,
            receiving_department=Department.UTILITY,
            prefilled_summary="Request gas shutoff",
            rule_id="commit-requires-approval",
            staged_at=NOW,
            status=ApprovalStatus.GRANTED,
        )


def test_referral_cannot_be_filed_without_approval_and_case_number() -> None:
    base = {
        "referral_id": "ref-1",
        "address_id": "sf-0450-hayes",
        "conflict_id": "conf-1",
        "supporting_fact_ids": ("fact_a", "fact_b"),
        "narrative": "Lidar measures a third storey with no permit of record.",
        "idempotency_key": "referral-sf0450hayes-conf1",
        "drafted_at": NOW,
    }
    with pytest.raises(ValidationError):
        ReferralRecord(**base, status=ReferralStatus.FILED)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ReferralRecord(  # type: ignore[arg-type]
            **base,
            status=ReferralStatus.FILED,
            filed_at=NOW,
            case_number="BLD-2026-1188",
        )
    ok = ReferralRecord(  # type: ignore[arg-type]
        **base,
        status=ReferralStatus.FILED,
        filed_at=NOW,
        case_number="BLD-2026-1188",
        approved_by="captain.reyes",
    )
    assert ok.case_number == "BLD-2026-1188"


def test_completed_survey_must_verify_something() -> None:
    base = {
        "survey_id": "sv-1",
        "address_id": "sf-0450-hayes",
        "company": "E-05",
        "surveyor": "capt.reyes",
        "started_at": NOW,
        "completed_at": NOW + timedelta(minutes=40),
    }
    with pytest.raises(ValidationError):
        SurveyRecord(**base, outcome=SurveyOutcome.COMPLETED)  # type: ignore[arg-type]
    ok = SurveyRecord(  # type: ignore[arg-type]
        **base, outcome=SurveyOutcome.COMPLETED, verified_keys=(Keys.STORIES,)
    )
    assert ok.verified_keys == (Keys.STORIES,)


def test_a_queue_row_must_carry_a_reason() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length 1
        SurveyQueueEntry(
            entry_id="q-1",
            address_id="sf-0450-hayes",
            district_id="sffd-district-03",
            rank=1,
            score=0.9,
            reasons=(),
            created_at=NOW,
            ranked_by_version="1.0.0",
        )


def test_a_queue_reason_cites_its_rule() -> None:
    entry = SurveyQueueEntry(
        entry_id="q-1",
        address_id="sf-0450-hayes",
        district_id="sffd-district-03",
        rank=1,
        score=0.91,
        reasons=(
            RankReason(
                rule_id="unpermitted-story-count",
                canonical_key=Keys.STORIES,
                detail="permit says 2 stories, lidar measures 3",
                weight=0.6,
                conflict_id="conf-1",
            ),
        ),
        created_at=NOW,
        ranked_by_version="1.0.0",
    )
    assert entry.reasons[0].rule_id == "unpermitted-story-count"


# --------------------------------------------------------------- geometry ---


def _spec(**overrides) -> GeometrySpec:
    payload = {
        "address_id": "sf-0450-hayes",
        "generated_at": NOW,
        "footprint": ((0.0, 0.0), (10.0, 0.0), (10.0, 20.0), (0.0, 20.0)),
        "levels": (
            Level(height_m=3.4, provenance=SourceType.PERMIT, status=AssertionStatus.CONFIRMED),
            Level(height_m=3.0, provenance=SourceType.LIDAR_DSM, status=AssertionStatus.DISPUTED),
        ),
        "roof_segments": (RoofSegment(pitch_deg=18.0, azimuth_deg=210.0),),
        "collapse_zone_radius_m": 9.6,
    }
    payload.update(overrides)
    return GeometrySpec(**payload)  # type: ignore[arg-type]


def test_disputed_mass_is_in_the_data_not_the_renderer() -> None:
    assert _spec().has_disputed_mass is True


def test_collapse_zone_is_deterministic() -> None:
    assert collapse_zone_radius(10.0) == 15.0
    assert collapse_zone_radius(6.4) == 9.6


def test_faces_default_to_unscanned() -> None:
    """A face without coverage renders UNSCANNED, never cool."""
    spec = _spec(faces=(Face(label=FaceLabel.BRAVO),))
    assert spec.unscanned_faces == (FaceLabel.BRAVO,)
    assert isinstance(spec.faces[0].thermal, UnscannedValue)


def test_a_measured_face_must_carry_its_timestamp() -> None:
    with pytest.raises(ValidationError):
        Face(label=FaceLabel.BRAVO, thermal=QuantityValue(magnitude=340.0, unit="C"))
    ok = Face(
        label=FaceLabel.BRAVO,
        thermal=QuantityValue(magnitude=340.0, unit="C"),
        observed_at=NOW,
    )
    assert ok.observed_at == NOW


def test_obstruction_must_reference_a_real_segment() -> None:
    with pytest.raises(ValidationError):
        _spec(
            obstructions=(
                Obstruction(
                    type=ObstructionType.SOLAR_ARRAY,
                    segment_index=9,
                    provenance=SourceType.SOLAR_API,
                ),
            )
        )


def test_duplicate_face_labels_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(faces=(Face(label=FaceLabel.ALPHA), Face(label=FaceLabel.ALPHA)))


def test_footprint_needs_at_least_three_points() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length 3
        _spec(footprint=((0.0, 0.0), (1.0, 1.0)))
