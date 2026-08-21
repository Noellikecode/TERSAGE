"""Grants, audit events, and replay -- against the real repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firstdue.container import Container, build_container
from firstdue.domain.enums import LogEntryType, Scope
from firstdue.domain.incidents import Incident
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.errors import GrantExpiredError, NotAuthorizedError
from firstdue.ports.audit import AuditEventKind
from firstdue.services.grants import GrantService
from firstdue.services.replay import IncidentReplay, compare, ordered_summary
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
INCIDENT = "inc-1"
ADDRESS = "sf-0450-hayes"


@pytest.fixture
def container(tmp_path: Path) -> Container:
    return build_container(
        Settings(
            app_env=AppEnv.TEST,
            use_fake_agents=True,
            fixtures_dir=REPO_ROOT / "fixtures",
            demo_state_dir=tmp_path / ".demo-state",
            log_json=False,
        )
    )


@pytest.fixture
def grants(container: Container) -> GrantService:
    return GrantService(
        grants=container.grants,
        audit=container.audit,
        clock=container.clock,
        ids=container.ids,
    )


# ------------------------------------------------------------------ grants


@pytest.mark.authorization
async def test_an_incident_grant_carries_every_binding(
    container: Container, grants: GrantService
) -> None:
    grant = await grants.mint_incident_grant(
        agent_id="incident-controller",
        incident_id=INCIDENT,
        address_id=ADDRESS,
        jurisdiction_id="sf-city-county",
        responding_agency_id="sffd",
        alarm_level=2,
    )

    assert grant.incident_id == INCIDENT
    assert grant.address_id == ADDRESS
    assert grant.alarm_level == 2
    assert grant.jurisdiction_id == "sf-city-county"
    assert grant.responding_agency_id == "sffd"
    assert grant.mutual_aid_agreement_id is None
    assert grant.ttl_seconds(container.clock.now()) > 0

    assert grant.covers_incident(INCIDENT)
    assert not grant.covers_incident("inc-2")
    assert grant.covers_address(ADDRESS)
    assert not grant.covers_address("sf-1215-fell")
    assert grant.covers_agency("sffd")
    assert not grant.covers_agency("daly-city-fd")


@pytest.mark.authorization
async def test_a_grant_expires_on_its_own(grants: GrantService) -> None:
    grant = await grants.mint_incident_grant(
        agent_id="incident-controller",
        incident_id=INCIDENT,
        address_id=ADDRESS,
        jurisdiction_id="sf-city-county",
        responding_agency_id="sffd",
        alarm_level=1,
        ttl=timedelta(minutes=30),
    )
    later = grant.issued_at + timedelta(hours=1)
    assert grant.is_expired(later)
    assert grant.ttl_seconds(later) == 0.0
    with pytest.raises(GrantExpiredError):
        grant.assert_scope(Scope.READ_PROFILE, now=later)


@pytest.mark.authorization
async def test_close_time_revocation_ends_authority(grants: GrantService) -> None:
    grant = await grants.mint_incident_grant(
        agent_id="incident-controller",
        incident_id=INCIDENT,
        address_id=ADDRESS,
        jurisdiction_id="sf-city-county",
        responding_agency_id="sffd",
        alarm_level=1,
    )
    revoked = await grants.revoke_for_incident(INCIDENT, grant.grant_id)

    assert revoked.revoked_at is not None
    after = revoked.revoked_at + timedelta(seconds=1)
    assert revoked.is_expired(after)
    with pytest.raises(GrantExpiredError):
        revoked.assert_scope(Scope.READ_PROFILE, now=after)


@pytest.mark.authorization
async def test_revocation_is_idempotent(grants: GrantService) -> None:
    grant = await grants.mint_incident_grant(
        agent_id="incident-controller",
        incident_id=INCIDENT,
        address_id=ADDRESS,
        jurisdiction_id="sf-city-county",
        responding_agency_id="sffd",
        alarm_level=1,
    )
    first = await grants.revoke(grant.grant_id, reason="incident closed")
    second = await grants.revoke(grant.grant_id, reason="incident closed again")
    assert first.revoked_at == second.revoked_at


@pytest.mark.authorization
async def test_a_standing_grant_cannot_hold_person_scope(grants: GrantService) -> None:
    """Enforced by the model, so no service method can create one."""
    from firstdue.errors import ValidationError

    with pytest.raises(ValidationError):
        await grants.standing_grant("records-watcher", scopes=frozenset({Scope.READ_EMS_DERIVED}))


@pytest.mark.authorization
async def test_a_missing_scope_is_refused_not_widened(grants: GrantService) -> None:
    grant = await grants.standing_grant("records-watcher")
    with pytest.raises(NotAuthorizedError):
        grant.assert_scope(Scope.WRITE_RMS, now=grant.issued_at)
    # Holding every read scope adds up to no write scope.
    assert grant.read_scopes
    assert Scope.WRITE_RMS not in grant.write_scopes


# ------------------------------------------------------------------- audit


async def test_grant_issuance_and_revocation_are_audited(
    container: Container, grants: GrantService
) -> None:
    grant = await grants.mint_incident_grant(
        agent_id="incident-controller",
        incident_id=INCIDENT,
        address_id=ADDRESS,
        jurisdiction_id="sf-city-county",
        responding_agency_id="sffd",
        alarm_level=3,
    )
    await grants.revoke(grant.grant_id, reason="incident closed")

    minted = await container.audit.list_events(kind=AuditEventKind.GRANT_MINTED)
    revoked = await container.audit.list_events(kind=AuditEventKind.GRANT_REVOKED)

    assert len(minted) == 1
    assert minted[0].detail["grant_id"] == grant.grant_id
    assert minted[0].detail["alarm_level"] == "3"
    assert len(revoked) == 1
    assert revoked[0].detail["reason"] == "incident closed"


async def test_the_slow_loop_records_writes_approvals_and_injection_blocks(
    container: Container,
) -> None:
    """One demo pass produces the audit trail an investigator would read."""
    from firstdue.demo.scenario import run_slow_loop

    await run_slow_loop(container)

    kinds = {e.kind for e in await container.audit.list_events(limit=500)}
    assert AuditEventKind.INJECTION_BLOCKED in kinds
    assert AuditEventKind.WRITE_EXECUTED in kinds
    assert AuditEventKind.APPROVAL_GRANTED in kinds

    blocked = await container.audit.list_events(kind=AuditEventKind.INJECTION_BLOCKED)
    assert blocked[0].detail["patterns"]
    # The audit record names the pattern, never the offending text.
    assert "ignore all previous" not in str(blocked[0].detail).lower()


# ------------------------------------------------------------------ replay


async def _seed_incident(container: Container) -> str:
    profile = await container.profiles.get(ADDRESS)
    if profile is None:
        from firstdue.domain.profiles import BuildingProfile

        profile = await container.profiles.create(
            BuildingProfile(address_id=ADDRESS, district_id="sffd-district-03")
        )
    snapshot = await container.snapshots.put(profile.snapshot(read_at=container.clock.now()))

    await container.incidents.create(
        Incident(
            incident_id=INCIDENT,
            address_id=ADDRESS,
            district_id="sffd-district-03",
            cad_ref="CAD-0001",
            alarm_level=2,
            jurisdiction_id="sf-city-county",
            responding_agency_id="sffd",
            grant_id="grant-1",
            profile_snapshot_id=snapshot.snapshot_id,
            dispatched_at=container.clock.now(),
            opened_at=container.clock.now(),
        )
    )
    for sequence, entry_type in enumerate(
        (LogEntryType.BRIEF_EMITTED, LogEntryType.BENCHMARK, LogEntryType.POLICY_DECISION)
    ):
        await container.incident_log.append(
            IncidentLogEntry(
                entry_id=f"entry-{sequence}",
                incident_id=INCIDENT,
                sequence=sequence,
                entry_type=entry_type,
                occurred_at=container.clock.now(),
                profile_snapshot_id=snapshot.snapshot_id,
                agent_versions={"incident-controller": "1.0.0"},
                content={"stage": "INSTANT", "index": sequence},
            )
        )
    return snapshot.snapshot_id


@pytest.mark.invariant
async def test_replay_reconstructs_the_same_ordered_output(container: Container) -> None:
    snapshot_id = await _seed_incident(container)
    replay = IncidentReplay(
        incidents=container.incidents,
        incident_log=container.incident_log,
        snapshots=container.snapshots,
        audit=container.audit,
    )

    first = await replay.replay(INCIDENT)
    second = await replay.replay(INCIDENT)

    assert compare(first, second)
    assert first.digest == second.digest
    assert ordered_summary(first) == ordered_summary(second)
    assert [e.sequence for e in first.entries] == [0, 1, 2]
    assert first.profile_snapshot_id == snapshot_id
    assert first.snapshot_available is True
    assert first.agent_versions == {"incident-controller": "1.0.0"}
    assert first.is_intact


@pytest.mark.invariant
async def test_replay_detects_content_edited_under_its_own_hash(container: Container) -> None:
    """The common tampering shape: change the entry, leave the hash alone.

    Caught by recomputing each entry's hash from its stored content. The
    ordered digest is unchanged -- the stored hashes were not touched -- so the
    detection has to come from the recomputation, and it does.
    """
    await _seed_incident(container)
    replay = IncidentReplay(
        incidents=container.incidents,
        incident_log=container.incident_log,
        snapshots=container.snapshots,
        audit=container.audit,
    )
    before = await replay.replay(INCIDENT)
    assert before.is_intact

    stored = await container.incident_log.get_log(INCIDENT)
    tampered = stored.entries[1].model_copy(update={"content": {"stage": "TAMPERED"}})
    # Reach past the repository to simulate a store-level edit. The append-only
    # API has no method that could do this, which is the point.
    object.__setattr__(stored, "entries", (stored.entries[0], tampered, *stored.entries[2:]))

    after = await replay.replay(INCIDENT)
    assert after.tampered_sequences == (1,)
    assert not after.is_intact


@pytest.mark.invariant
async def test_replay_detects_a_rehashed_entry_through_the_digest(
    container: Container,
) -> None:
    """The thorough tampering shape: change the entry *and* its hash.

    The per-entry check passes, because the entry is now self-consistent. The
    ordered digest is what catches it, which is why the replay reports both.
    """
    await _seed_incident(container)
    replay = IncidentReplay(
        incidents=container.incidents,
        incident_log=container.incident_log,
        snapshots=container.snapshots,
        audit=container.audit,
    )
    before = await replay.replay(INCIDENT)

    stored = await container.incident_log.get_log(INCIDENT)
    rehashed = stored.entries[1].model_copy(update={"content": {"stage": "TAMPERED"}}).sealed()
    object.__setattr__(stored, "entries", (stored.entries[0], rehashed, *stored.entries[2:]))

    after = await replay.replay(INCIDENT)
    assert after.is_intact  # self-consistent, and still not the same incident
    assert after.digest != before.digest


async def test_replaying_an_unknown_incident_is_a_not_found(container: Container) -> None:
    from firstdue.errors import NotFoundError

    replay = IncidentReplay(
        incidents=container.incidents,
        incident_log=container.incident_log,
        snapshots=container.snapshots,
        audit=container.audit,
    )
    with pytest.raises(NotFoundError):
        await replay.replay("inc-does-not-exist")
