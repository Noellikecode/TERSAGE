"""The gateway: five outcomes, default deny, and the grant failure modes.

Every clause of the authorization model gets a test that asserts the *refusal*,
not just the success. A rule nobody has watched fail is a rule nobody has
tested.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.enums import (
    ApprovalThreshold,
    Classification,
    Department,
    Operation,
    PolicyAction,
    Scope,
)
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.gateway.engine import (
    FALLBACK_RULE_ID,
    POLICY_VERSION,
    AccessRequest,
    PolicyEngine,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
INCIDENT = "inc-1"
ADDRESS = "sf-0450-hayes"
JURISDICTION = "sf-city-county"
AGENCY = "sffd"


@pytest.fixture
def engine(ids) -> PolicyEngine:
    return PolicyEngine(ids=ids)


def _incident_grant(**overrides: object) -> IncidentGrant:
    payload: dict[str, object] = {
        "grant_id": "grant-1",
        "agent_id": "incident-controller",
        "holder_department": Department.FIRE,
        "scopes": frozenset(
            {
                Scope.READ_PROFILE,
                Scope.READ_GEOMETRY,
                Scope.READ_TIER_II_METADATA,
                Scope.READ_EMS_DERIVED,
                Scope.WRITE_RMS,
                Scope.NOTIFY_AGENCY,
            }
        ),
        "issued_at": NOW,
        "incident_id": INCIDENT,
        "address_id": ADDRESS,
        "alarm_level": 2,
        "jurisdiction_id": JURISDICTION,
        "responding_agency_id": AGENCY,
        "expires_at": NOW + timedelta(hours=12),
    }
    payload.update(overrides)
    return IncidentGrant(**payload)  # type: ignore[arg-type]


def _standing_grant(**overrides: object) -> StandingGrant:
    payload: dict[str, object] = {
        "grant_id": "grant-standing",
        "agent_id": "records-watcher",
        "holder_department": Department.FIRE,
        "scopes": frozenset({Scope.READ_PUBLIC_RECORDS, Scope.WRITE_PROFILE}),
        "issued_at": NOW,
    }
    payload.update(overrides)
    return StandingGrant(**payload)  # type: ignore[arg-type]


def _request(**overrides: object) -> AccessRequest:
    payload: dict[str, object] = {
        "agent_id": "incident-controller",
        "agent_version": "1.0.0",
        "grant": _incident_grant(),
        "target": "profile",
        "operation": Operation.READ,
        "classification": Classification.PUBLIC,
        "scope": Scope.READ_PROFILE,
        "now": NOW + timedelta(minutes=1),
        "incident_id": INCIDENT,
        "address_id": ADDRESS,
        "responding_agency_id": AGENCY,
    }
    payload.update(overrides)
    return AccessRequest(**payload)  # type: ignore[arg-type]


# ------------------------------------------------------------ five outcomes


def test_allow(engine: PolicyEngine) -> None:
    decision = engine.decide(_request())
    assert decision.action is PolicyAction.ALLOW
    assert decision.rule_id == "read.scope-held"
    assert decision.released_raw_record is True


def test_derive(engine: PolicyEngine) -> None:
    """PHI has no ALLOW anywhere in the policy. The best outcome is DERIVE."""
    decision = engine.decide(
        _request(
            classification=Classification.PHI,
            scope=Scope.READ_EMS_DERIVED,
            target="ems-derived",
        )
    )
    assert decision.action is PolicyAction.DERIVE
    assert decision.derivation_function == "derive_ems_life_safety"
    # DERIVE never releases the underlying record.
    assert decision.released_raw_record is False


def test_withhold_jurisdiction(engine: PolicyEngine) -> None:
    """Withheld, not hidden: the officer learns the record exists."""
    decision = engine.decide(
        _request(
            grant=_incident_grant(
                jurisdiction_id="daly-city",
                mutual_aid_agreement_id="aid-sf-dalycity-2024",
            ),
            classification=Classification.TIER_II_CONFIDENTIAL,
            scope=Scope.READ_TIER_II_METADATA,
            record_jurisdiction_id=JURISDICTION,
        )
    )
    assert decision.action is PolicyAction.WITHHOLD_JURISDICTION
    assert decision.mutual_aid_agreement_id == "aid-sf-dalycity-2024"
    assert "withheld" in decision.justification.lower()


def test_require_approval(engine: PolicyEngine) -> None:
    decision = engine.decide(
        _request(
            grant=_standing_grant(
                scopes=frozenset({Scope.WRITE_REFERRAL}), agent_id="referral-clerk"
            ),
            agent_id="referral-clerk",
            operation=Operation.WRITE,
            scope=Scope.WRITE_REFERRAL,
            target="building-referral-intake",
            incident_id=None,
            address_id=None,
            responding_agency_id=None,
        )
    )
    assert decision.action is PolicyAction.REQUIRE_APPROVAL
    assert decision.approval_threshold is ApprovalThreshold.SUPERVISOR


def test_deny(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(scope=Scope.WRITE_RMS, operation=Operation.READ))
    assert decision.action is PolicyAction.DENY


@pytest.mark.invariant
def test_every_outcome_is_reachable(engine: PolicyEngine) -> None:
    """All five, from one engine, at one policy version."""
    seen = {
        engine.decide(_request()).action,
        engine.decide(
            _request(classification=Classification.PHI, scope=Scope.READ_EMS_DERIVED)
        ).action,
        engine.decide(
            _request(
                grant=_incident_grant(jurisdiction_id="daly-city"),
                classification=Classification.TIER_II_CONFIDENTIAL,
                scope=Scope.READ_TIER_II_METADATA,
                record_jurisdiction_id=JURISDICTION,
            )
        ).action,
        engine.decide(
            _request(
                grant=_standing_grant(scopes=frozenset({Scope.WRITE_REFERRAL})),
                operation=Operation.WRITE,
                scope=Scope.WRITE_REFERRAL,
                incident_id=None,
                address_id=None,
                responding_agency_id=None,
            )
        ).action,
        engine.decide(_request(scope=Scope.READ_AUDIT)).action,
    }
    assert seen == set(PolicyAction)


# ------------------------------------------------------------ default deny


@pytest.mark.invariant
def test_an_engine_with_no_rules_denies_everything(ids) -> None:
    """A policy gap costs a refusal, never a leak."""
    empty = PolicyEngine(ids=ids, rules=())
    decision = empty.decide(_request())
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == FALLBACK_RULE_ID


# ------------------------------------------------------- grant failure modes


@pytest.mark.authorization
def test_an_expired_grant_authorizes_nothing(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(now=NOW + timedelta(hours=13)))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "grant.expired-or-revoked"


@pytest.mark.authorization
def test_a_revoked_grant_authorizes_nothing(engine: PolicyEngine) -> None:
    revoked = _incident_grant().revoke(at=NOW + timedelta(minutes=30))
    decision = engine.decide(_request(grant=revoked, now=NOW + timedelta(hours=1)))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "grant.expired-or-revoked"


@pytest.mark.authorization
def test_a_grant_for_another_address_is_refused(engine: PolicyEngine) -> None:
    """A dispatch opens one building, not the block."""
    decision = engine.decide(_request(address_id="sf-1215-fell"))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "incident-grant.wrong-address"


@pytest.mark.authorization
def test_a_grant_for_another_incident_is_refused(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(incident_id="inc-2"))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "incident-grant.wrong-incident"


@pytest.mark.authorization
def test_a_grant_used_by_another_agency_is_refused(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(responding_agency_id="daly-city-fd"))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "incident-grant.wrong-agency"


@pytest.mark.authorization
def test_a_scope_the_grant_does_not_hold_is_refused(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(scope=Scope.WRITE_REFERRAL, operation=Operation.WRITE))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "grant.scope-not-held"


# -------------------------------------------------- read never implies write


@pytest.mark.authorization
@pytest.mark.invariant
def test_read_permission_never_grants_write_permission(engine: PolicyEngine) -> None:
    """Every read scope in the system, tried as a write. All refused."""
    reader = _incident_grant(
        scopes=frozenset(
            {
                Scope.READ_PROFILE,
                Scope.READ_GEOMETRY,
                Scope.READ_PUBLIC_RECORDS,
                Scope.READ_TIER_II_METADATA,
                Scope.READ_EMS_DERIVED,
                Scope.READ_AUDIT,
            }
        )
    )
    for scope in (
        Scope.READ_PROFILE,
        Scope.READ_GEOMETRY,
        Scope.READ_PUBLIC_RECORDS,
        Scope.READ_TIER_II_METADATA,
        Scope.READ_AUDIT,
    ):
        decision = engine.decide(
            _request(grant=reader, operation=Operation.WRITE, scope=scope, target="department-rms")
        )
        assert decision.action is PolicyAction.DENY, scope
        assert decision.rule_id == "grant.read-scope-cannot-write"


@pytest.mark.authorization
def test_a_write_scope_is_not_a_read_scope(engine: PolicyEngine) -> None:
    decision = engine.decide(_request(operation=Operation.READ, scope=Scope.WRITE_RMS))
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "grant.write-scope-is-not-a-read"


@pytest.mark.authorization
def test_a_standing_grant_cannot_reach_person_level_records(engine: PolicyEngine) -> None:
    """Between incidents the fleet reads buildings and never people."""
    decision = engine.decide(
        _request(
            grant=_standing_grant(scopes=frozenset({Scope.READ_PUBLIC_RECORDS})),
            classification=Classification.PHI,
            scope=Scope.READ_PUBLIC_RECORDS,
            incident_id=None,
            address_id=None,
            responding_agency_id=None,
        )
    )
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "standing-grant.no-person-level-access"


@pytest.mark.authorization
def test_phi_is_never_written(engine: PolicyEngine) -> None:
    decision = engine.decide(
        _request(
            classification=Classification.PHI,
            operation=Operation.WRITE,
            scope=Scope.WRITE_RMS,
        )
    )
    assert decision.action is PolicyAction.DENY
    assert decision.rule_id == "phi.no-writes"


# ------------------------------------------------------------- the record


@pytest.mark.invariant
def test_every_decision_carries_the_whole_record(engine: PolicyEngine) -> None:
    decision = engine.decide(_request())

    assert decision.decision_id
    assert decision.agent_id == "incident-controller"
    assert decision.agent_version == "1.0.0"
    assert decision.grant_id == "grant-1"
    assert decision.target == "profile"
    assert decision.operation is Operation.READ
    assert decision.classification is Classification.PUBLIC
    assert decision.action is PolicyAction.ALLOW
    assert decision.rule_id
    assert decision.justification
    assert decision.policy_version == POLICY_VERSION
    assert decision.decided_at == NOW + timedelta(minutes=1)


@pytest.mark.invariant
def test_no_model_makes_a_decision(engine: PolicyEngine) -> None:
    """A constant on the record, so the claim is checkable in the audit log."""
    assert engine.decide(_request()).decided_by == "deterministic-policy-engine"


@pytest.mark.invariant
def test_no_model_code_is_reachable_from_policy_evaluation() -> None:
    """The gateway must not import, transitively, anything that calls a model.

    Checked by walking the import graph rather than by inspection, so a future
    import of an extraction or model module fails this test rather than
    quietly putting a model on the authorization path.
    """
    import firstdue.gateway.derivation
    import firstdue.gateway.engine
    import firstdue.gateway.jurisdiction

    forbidden = ("firstdue.ports.model", "firstdue.extraction", "firstdue.adapters.fake.model")
    for module in (
        firstdue.gateway.engine,
        firstdue.gateway.derivation,
        firstdue.gateway.jurisdiction,
    ):
        source = inspect.getsource(module)
        for name in forbidden:
            assert name not in source, f"{module.__name__} references {name}"


def test_evaluation_is_deterministic(engine: PolicyEngine) -> None:
    first = engine.evaluate(_request())
    second = engine.evaluate(_request())
    assert first == second


# ------------------------------------------------------ emergency exception


@pytest.mark.authorization
def test_an_emergency_exception_promotes_a_withholding_and_is_named(
    engine: PolicyEngine,
) -> None:
    """An incident commander can override a jurisdictional withholding.

    It is a distinct rule id so the audit log shows an exception was declared
    rather than an ordinary allow.
    """
    decision = engine.decide(
        _request(
            grant=_incident_grant(jurisdiction_id="daly-city"),
            classification=Classification.TIER_II_CONFIDENTIAL,
            scope=Scope.READ_TIER_II_METADATA,
            record_jurisdiction_id=JURISDICTION,
            emergency_exception=True,
        )
    )
    assert decision.action is PolicyAction.ALLOW
    assert decision.rule_id == "emergency.exception-declared"


@pytest.mark.authorization
def test_an_emergency_exception_cannot_promote_a_denial(engine: PolicyEngine) -> None:
    """The override reaches jurisdiction and nothing else."""
    for overrides in (
        {"now": NOW + timedelta(hours=13)},
        {"address_id": "sf-1215-fell"},
        {"scope": Scope.WRITE_REFERRAL, "operation": Operation.WRITE},
    ):
        decision = engine.decide(_request(emergency_exception=True, **overrides))
        assert decision.action is PolicyAction.DENY
