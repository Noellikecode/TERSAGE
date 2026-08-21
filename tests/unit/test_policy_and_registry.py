"""Policy decisions are deterministic; registry versions are pinned."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.domain.enums import (
    ApprovalThreshold,
    Capability,
    Classification,
    Department,
    Loop,
    Operation,
    PolicyAction,
    Scope,
)
from firstdue.domain.policy import PolicyDecision
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.errors import ValidationError

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _decision(**overrides) -> PolicyDecision:
    payload = {
        "decision_id": "pd-1",
        "agent_id": "reconciler",
        "target": "ems-prior-runs",
        "operation": Operation.READ,
        "classification": Classification.PHI,
        "action": PolicyAction.ALLOW,
        "rule_id": "rule-1",
        "justification": "authorized department reading public permit data",
        "policy_version": "1.0.0",
        "decided_at": NOW,
    }
    payload.update(overrides)
    return PolicyDecision(**payload)  # type: ignore[arg-type]


def test_no_model_can_make_a_decision() -> None:
    decision = _decision()
    assert decision.decided_by == "deterministic-policy-engine"
    with pytest.raises(Exception):  # noqa: B017 - Literal is closed
        _decision(decided_by="gemini-3.5-flash")


def test_derive_must_name_its_derivation_function() -> None:
    with pytest.raises(ValidationError):
        _decision(action=PolicyAction.DERIVE)


def test_derive_never_releases_the_raw_record() -> None:
    decision = _decision(action=PolicyAction.DERIVE, derivation_function="ems_life_safety_scope_v1")
    assert decision.released_raw_record is False


def test_allow_releases_the_record() -> None:
    assert _decision(action=PolicyAction.ALLOW).released_raw_record is True


def test_only_derive_may_name_a_derivation_function() -> None:
    with pytest.raises(ValidationError):
        _decision(action=PolicyAction.ALLOW, derivation_function="ems_life_safety_scope_v1")


def test_withhold_must_cite_the_aid_agreement() -> None:
    with pytest.raises(ValidationError):
        _decision(action=PolicyAction.WITHHOLD_JURISDICTION)
    ok = _decision(
        action=PolicyAction.WITHHOLD_JURISDICTION,
        mutual_aid_agreement_id="agr-county-b-2026",
    )
    assert ok.mutual_aid_agreement_id == "agr-county-b-2026"


def test_require_approval_needs_a_real_threshold() -> None:
    with pytest.raises(ValidationError):
        _decision(action=PolicyAction.REQUIRE_APPROVAL, approval_threshold=ApprovalThreshold.NONE)
    ok = _decision(
        action=PolicyAction.REQUIRE_APPROVAL, approval_threshold=ApprovalThreshold.SUPERVISOR
    )
    assert ok.approval_threshold is ApprovalThreshold.SUPERVISOR


def test_every_decision_cites_a_rule() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length
        _decision(rule_id="")


# --------------------------------------------------------------- registry ---


def _descriptor(**overrides) -> AgentDescriptor:
    payload = {
        "agent_id": "hazard-watcher",
        "version": "2.4.1",
        "publisher_department": Department.COUNTY_OEM,
        "loop": Loop.SLOW,
        "role_summary": "Watches EPA, PHMSA, NREL and Tier II filings",
        "capabilities": frozenset({Capability.READ}),
        "required_scopes": frozenset({Scope.READ_TIER_II_METADATA}),
        "classifications_accessed": frozenset({Classification.TIER_II_CONFIDENTIAL}),
        "input_schema_ref": "schemas/hazard.in.json",
        "output_schema_ref": "schemas/hazard.out.json",
        "latency_target_ms": 30_000,
        "published_at": NOW,
    }
    payload.update(overrides)
    return AgentDescriptor(**payload)  # type: ignore[arg-type]


def test_version_must_be_semver() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pattern
        _descriptor(version="latest")


def test_write_capability_requires_targets_and_a_write_scope() -> None:
    with pytest.raises(ValidationError):
        _descriptor(capabilities=frozenset({Capability.READ, Capability.WRITE}))
    with pytest.raises(ValidationError):
        _descriptor(
            capabilities=frozenset({Capability.READ, Capability.WRITE}),
            write_targets=("building-referral-intake",),
        )
    ok = _descriptor(
        capabilities=frozenset({Capability.READ, Capability.WRITE}),
        write_targets=("building-referral-intake",),
        required_scopes=frozenset({Scope.READ_PUBLIC_RECORDS, Scope.WRITE_REFERRAL}),
    )
    assert ok.write_targets == ("building-referral-intake",)


def test_write_targets_require_declaring_write_capability() -> None:
    with pytest.raises(ValidationError):
        _descriptor(write_targets=("building-referral-intake",))


def test_agent_ref_is_pinned() -> None:
    assert _descriptor().ref == "hazard-watcher@2.4.1"


def test_subscription_pins_an_exact_version() -> None:
    sub = Subscription(
        subscription_id="sub-1",
        subscriber_department=Department.FIRE,
        agent_id="hazard-watcher",
        pinned_version="2.4.1",
        subscribed_at=NOW,
    )
    assert sub.ref == "hazard-watcher@2.4.1"
    assert sub.is_active(NOW) is True
