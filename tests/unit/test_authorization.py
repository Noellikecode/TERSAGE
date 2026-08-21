"""Grants: no standing access to citizen records, and scopes are separate."""

from __future__ import annotations

from datetime import timedelta

import pytest

from firstdue.domain.enums import Classification, Department, Scope
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.errors import GrantExpiredError, NotAuthorizedError, ValidationError

pytestmark = pytest.mark.authorization


def _standing(**overrides) -> StandingGrant:
    payload = {
        "grant_id": "sg-1",
        "agent_id": "records-watcher",
        "holder_department": Department.BUILDING,
        "scopes": frozenset({Scope.READ_PUBLIC_RECORDS}),
        "issued_at": __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
    }
    payload.update(overrides)
    return StandingGrant(**payload)  # type: ignore[arg-type]


def _incident(epoch, **overrides) -> IncidentGrant:
    payload = {
        "grant_id": "ig-1",
        "agent_id": "reconciler",
        "holder_department": Department.FIRE,
        "scopes": frozenset({Scope.READ_PROFILE, Scope.READ_EMS_DERIVED}),
        "issued_at": epoch,
        "incident_id": "inc-1",
        "address_id": "sf-0450-hayes",
        "alarm_level": 2,
        "jurisdiction_id": "sf-city-county",
        "responding_agency_id": "sffd",
        "expires_at": epoch + timedelta(hours=6),
    }
    payload.update(overrides)
    return IncidentGrant(**payload)  # type: ignore[arg-type]


def test_standing_grant_cannot_read_ems_derived() -> None:
    """Between incidents the fleet can read nothing about anyone."""
    with pytest.raises(ValidationError):
        _standing(scopes=frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_EMS_DERIVED}))


def test_standing_grant_cannot_carry_phi() -> None:
    with pytest.raises(ValidationError):
        _standing(allowed_classifications=frozenset({Classification.PUBLIC, Classification.PHI}))


def test_standing_grant_may_carry_tier_ii_metadata() -> None:
    grant = _standing(scopes=frozenset({Scope.READ_TIER_II_METADATA}))
    assert Classification.TIER_II_CONFIDENTIAL in grant.allowed_classifications
    assert grant.is_expired(grant.issued_at) is False


def test_incident_grant_expires(epoch) -> None:
    grant = _incident(epoch)
    assert grant.is_expired(epoch + timedelta(hours=5)) is False
    assert grant.is_expired(epoch + timedelta(hours=7)) is True


def test_expired_grant_denies_every_scope(epoch) -> None:
    grant = _incident(epoch)
    with pytest.raises(GrantExpiredError):
        grant.assert_scope(Scope.READ_PROFILE, now=epoch + timedelta(hours=7))


def test_missing_scope_is_denied(epoch) -> None:
    grant = _incident(epoch)
    with pytest.raises(NotAuthorizedError):
        grant.assert_scope(Scope.WRITE_REFERRAL, now=epoch)


def test_read_scope_does_not_imply_write_scope(epoch) -> None:
    """An agent authorized to read the permit system is not thereby authorized
    to file with it."""
    grant = _incident(epoch, scopes=frozenset({Scope.READ_PUBLIC_RECORDS}))
    grant.assert_scope(Scope.READ_PUBLIC_RECORDS, now=epoch)
    with pytest.raises(NotAuthorizedError):
        grant.assert_scope(Scope.WRITE_REFERRAL, now=epoch)


def test_revocation_denies_immediately(epoch) -> None:
    grant = _incident(epoch).revoke(at=epoch + timedelta(minutes=30))
    assert grant.is_expired(epoch + timedelta(minutes=31)) is True
    with pytest.raises(GrantExpiredError):
        grant.assert_scope(Scope.READ_PROFILE, now=epoch + timedelta(minutes=31))


def test_revocation_is_idempotent(epoch) -> None:
    once = _incident(epoch).revoke(at=epoch + timedelta(minutes=10))
    twice = once.revoke(at=epoch + timedelta(minutes=20))
    assert twice.revoked_at == once.revoked_at


def test_grant_must_expire_after_issue(epoch) -> None:
    with pytest.raises(ValidationError):
        _incident(epoch, expires_at=epoch - timedelta(hours=1))


def test_mutual_aid_marks_out_of_jurisdiction(epoch) -> None:
    grant = _incident(epoch, mutual_aid_agreement_id="agr-county-b-2026")
    assert grant.is_out_of_jurisdiction is True
    assert _incident(epoch).is_out_of_jurisdiction is False
