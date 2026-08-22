"""The incident loop, end to end.

Every clause of the phase's acceptance criteria has a test here: the instant
brief lands inside its budget with no model call, it is in the log before it can
be transmitted, degraded sources still produce a brief that says what is
missing, a cold profile says the structure is unknown, an IC resolution creates
an amendment and bumps the profile version, and closing revokes the grant and
seals the log.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.enums import BenchmarkType, BriefStage, FaceLabel, PolicyAction
from firstdue.domain.keys import Keys
from firstdue.errors import BriefNotPersistedError, GrantExpiredError
from firstdue.incident.fusion import THERMAL_CAVEAT, ThermalFrame
from firstdue.incident.session import IncidentSession
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COLD_ADDRESS = "sf-3120-24th"


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
def session(container: Container) -> IncidentSession:
    return IncidentSession(container)


async def _warm(container: Container) -> None:
    """Run the slow loop so there is a profile to brief from."""
    await run_slow_loop(container, approve=False)


async def _open(session: IncidentSession, address: str = DISPUTED_ADDRESS_ID):
    return await session.controller.open(address=address, cad_ref="CAD-0001", alarm_level=2)


# --------------------------------------------------------------- opening


async def test_opening_mints_a_grant_reads_one_snapshot_and_emits(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)

    # 1. A grant, bound to this incident and this address.
    assert opened.grant.incident_id == opened.incident.incident_id
    assert opened.grant.address_id == DISPUTED_ADDRESS_ID
    assert opened.grant.alarm_level == 2
    assert opened.grant.ttl_seconds(container.clock.now()) > 0

    # 2 and 3. One snapshot, recorded on the incident for replay.
    assert opened.snapshot.address_id == DISPUTED_ADDRESS_ID
    assert opened.incident.profile_snapshot_id == opened.snapshot.snapshot_id
    assert await container.snapshots.get(opened.snapshot_id) is not None

    # 4. The envelope carries identifiers and nothing else.
    published = [e for e in container.bus.published if str(e.topic) == "incident.opened"]
    assert len(published) == 1
    assert published[0].ids["incident_id"] == opened.incident.incident_id
    assert published[0].ids["profile_snapshot_id"] == opened.snapshot_id

    # 5. The elapsed clock runs from dispatch.
    assert session.controller.elapsed_seconds(opened.incident) >= 0.0


async def test_an_unknown_address_does_not_open_an_incident(session: IncidentSession) -> None:
    from firstdue.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await session.controller.open(address="1 Nowhere Ave", cad_ref="CAD-9")


# ---------------------------------------------------------- the instant brief


@pytest.mark.invariant
async def test_the_instant_brief_lands_in_budget_with_no_model_call(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)

    started = time.perf_counter()
    emission = await session.emit_instant(opened)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert emission.stage is BriefStage.INSTANT
    assert emission.version == 1
    # Not "no model was needed" -- the model cannot be invoked here at all.
    assert emission.model_invoked is False
    assert emission.narrative is None
    assert elapsed_ms < container.settings.instant_brief_budget_ms


async def test_the_instant_brief_carries_what_an_officer_reads_first(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    emission = await session.emit_instant(await _open(session))

    rendered = {
        item.canonical_key: item
        for section in emission.sections
        for item in section.items
        if item.canonical_key
    }
    assert Keys.CONSTRUCTION_TYPE in rendered
    assert Keys.STORIES in rendered
    assert Keys.OCCUPANCY_TYPE in rendered
    assert Keys.SUPPRESSION_SPRINKLERED in rendered

    # The disagreement is on the brief, marked as one.
    assert emission.conflict_ids
    assert rendered[Keys.STORIES].status.value == "DISPUTED"

    # Unknowns are listed rather than left off.
    assert emission.unknowns
    assert Keys.SUPPRESSION_SPRINKLERED in emission.unknowns

    # The collapse zone is the 1.5x convention applied to a measured height.
    collapse = [
        item
        for section in emission.sections
        for item in section.items
        if item.label == "collapse zone"
    ]
    assert collapse and "1.5x" in collapse[0].value_render


@pytest.mark.invariant
async def test_a_cold_profile_says_the_structure_is_unknown(
    container: Container, session: IncidentSession
) -> None:
    """New construction, nothing on file. The brief must say so."""
    opened = await _open(session, COLD_ADDRESS)
    assert opened.cold_start is True

    emission = await session.emit_instant(opened)
    assert Keys.CONSTRUCTION_TYPE in emission.unknowns
    assert Keys.STORIES in emission.unknowns
    values = {item.value_render for section in emission.sections for item in section.items}
    assert any("UNKNOWN" in value for value in values)
    # Nothing was invented to fill the gap.
    assert emission.conflict_ids == ()


# ------------------------------------------------------ persist before transmit


@pytest.mark.invariant
async def test_an_emission_is_in_the_log_before_it_can_be_transmitted(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    emission = await session.emit_instant(opened)

    # The persisted copy is the only one a transport will accept.
    assert emission.persisted_at is not None
    assert emission.content_hash
    emission.require_persisted()

    log = await container.incident_log.get_log(opened.incident.incident_id)
    hashes = [entry.content.get("content_hash") for entry in log.entries]
    assert emission.content_hash in hashes


@pytest.mark.invariant
async def test_an_unpersisted_emission_cannot_be_transmitted(
    container: Container, session: IncidentSession
) -> None:
    """The gate, asserted by its failure."""
    await _warm(container)
    opened = await _open(session)
    unpersisted = session.reconciler.instant(
        opened.snapshot, incident_id=opened.incident.incident_id
    )
    assert unpersisted.persisted_at is None
    with pytest.raises(BriefNotPersistedError):
        unpersisted.require_persisted()


# ------------------------------------------------------------- enrichment


async def test_the_enriched_stage_adds_prose_and_keeps_the_deterministic_brief(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    instant = await session.emit_instant(opened)
    enriched = await session.emit_enriched(opened.incident.incident_id)

    assert enriched.stage is BriefStage.ENRICHED
    assert enriched.version == instant.version + 1
    assert enriched.model_invoked is True
    # The deterministic sections are carried over untouched.
    assert enriched.sections == instant.sections
    assert enriched.unknowns == instant.unknowns


@pytest.mark.degraded
async def test_a_model_failure_still_produces_a_brief(
    container: Container, session: IncidentSession
) -> None:
    """Vertex being down costs the prose and nothing else."""
    from firstdue.adapters.fake.model import FakeModelClient
    from firstdue.incident.reconciler import Reconciler

    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)

    session.reconciler = Reconciler(
        clock=container.clock,
        ids=container.ids,
        model=FakeModelClient(unavailable=True),
    )
    enriched = await session.emit_enriched(opened.incident.incident_id)

    assert enriched.narrative is None
    assert enriched.narrative_available is False
    # The brief still landed, with everything the deterministic stage had.
    assert enriched.sections
    assert enriched.persisted_at is not None


@pytest.mark.degraded
async def test_a_rejected_model_response_leaves_the_brief_deterministic(
    container: Container, session: IncidentSession
) -> None:
    from firstdue.adapters.fake.model import FakeModelClient
    from firstdue.incident.reconciler import Reconciler

    await _warm(container)
    opened = await _open(session)
    instant = await session.emit_instant(opened)

    session.reconciler = Reconciler(
        clock=container.clock, ids=container.ids, model=FakeModelClient(reject_output=True)
    )
    enriched = await session.emit_enriched(opened.incident.incident_id)

    assert enriched.narrative_available is False
    assert enriched.sections == instant.sections


# ------------------------------------------------------------ the 360


@pytest.mark.invariant
async def test_an_ic_resolution_amends_the_brief_and_bumps_the_profile_version(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)

    before = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert before is not None
    conflict = before.open_conflicts[0]

    result = await session.resolve(
        opened.incident.incident_id,
        conflict_id=conflict.conflict_id,
        observed_value="3",
        resolved_by="bc-09",
        note="Walked the Charlie side; third floor confirmed.",
    )

    after = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert after is not None
    assert after.profile_version > before.profile_version
    assert result["profile_version"] == after.profile_version
    # The conflict is closed, and both original facts are still stored.
    assert not after.open_conflicts
    assert len(after.fact_sets[Keys.STORIES].facts) >= 3

    # The amendment is marked as one.
    latest = session.latest(opened.incident.incident_id)
    assert latest is not None
    assert latest.stage is BriefStage.AMENDMENT
    assert latest.version == result["brief_version"]

    # And it is in the log as an IC resolution.
    log = await container.incident_log.get_log(opened.incident.incident_id)
    assert any(str(e.entry_type) == "IC_RESOLUTION" for e in log.entries)


async def test_a_live_observation_outranks_the_filed_record(
    container: Container, session: IncidentSession
) -> None:
    """What the IC saw becomes what the profile shows."""
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None

    await session.resolve(
        opened.incident.incident_id,
        conflict_id=profile.open_conflicts[0].conflict_id,
        observed_value="3",
        resolved_by="bc-09",
        note="",
    )
    after = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert after is not None
    assert after.facts[Keys.STORIES].source_type.value == "IC_RESOLUTION"


# ------------------------------------------------------------ sensor fusion


async def test_thermal_frames_register_to_faces_and_the_rest_stay_unscanned(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)

    result = await session.register_thermal(
        opened.incident.incident_id,
        ThermalFrame(
            frame_id="frame-1",
            incident_id=opened.incident.incident_id,
            face=FaceLabel.ALPHA,
            observed_at=container.clock.now(),
            region_temps_c=(22.0, 24.0, 91.0),
            coverage=0.8,
        ),
    )

    # Three faces nobody flew are UNSCANNED, not cool.
    assert set(result["unscanned_faces"]) == {"BRAVO", "CHARLIE", "DELTA"}
    # And the delta on Alpha is reported as an observation.
    assert result["voids"] == 1

    coverage = session.fusion.coverage(opened.incident.incident_id, now=container.clock.now())
    alpha = next(c for c in coverage if c.face is FaceLabel.ALPHA)
    assert alpha.scanned
    assert THERMAL_CAVEAT in alpha.render
    charlie = next(c for c in coverage if c.face is FaceLabel.CHARLIE)
    assert not charlie.scanned
    assert "UNSCANNED" in charlie.render


@pytest.mark.invariant
async def test_thermal_coverage_lapses_rather_than_holding_a_stale_reading(
    container: Container, session: IncidentSession
) -> None:
    from firstdue.adapters.clock import FixedClock

    now = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    session.fusion.register(
        ThermalFrame(
            frame_id="frame-1",
            incident_id="inc-1",
            face=FaceLabel.ALPHA,
            observed_at=now,
            region_temps_c=(300.0,),
        )
    )
    assert FixedClock(now)  # the clock is injected, not read

    later = now + timedelta(minutes=30)
    alpha = next(
        c for c in session.fusion.coverage("inc-1", now=later) if c.face is FaceLabel.ALPHA
    )
    assert not alpha.scanned
    assert "lapsed" in alpha.render


# --------------------------------------------------------------- resources


@pytest.mark.authorization
async def test_a_notification_goes_out_and_a_commitment_waits_for_a_human(
    container: Container, session: IncidentSession
) -> None:
    """The line is drawn by gateway policy, not by this endpoint."""
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    incident_id = opened.incident.incident_id

    told = await session.request_resource(
        incident_id, kind_id="water-supply", detail="", approval_id=None
    )
    assert told.action is PolicyAction.ALLOW
    assert told.sent

    committed = await session.request_resource(
        incident_id, kind_id="gas-shutoff", detail="", approval_id=None
    )
    assert committed.action is PolicyAction.REQUIRE_APPROVAL
    assert committed.awaiting_human
    assert not committed.sent
    assert committed.approval_id

    staged = await container.approvals.get(committed.approval_id)
    assert staged is not None
    assert "gas shutoff" in staged.prefilled_summary.lower()


@pytest.mark.authorization
async def test_approving_a_staged_commitment_executes_it(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    incident_id = opened.incident.incident_id

    staged = await session.request_resource(
        incident_id, kind_id="road-closure", detail="", approval_id=None
    )
    assert staged.approval_id is not None

    result = await session.approve(incident_id, staged.approval_id, decided_by="bc-09")
    assert result["executed"] is True
    assert result["external_ref"]

    log = await container.incident_log.get_log(incident_id)
    assert any(str(e.entry_type) == "APPROVAL_GRANTED" for e in log.entries)


# --------------------------------------------------------------- closing


@pytest.mark.invariant
async def test_closing_revokes_the_grant_and_seals_the_log(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    incident_id = opened.incident.incident_id

    result = await session.controller.close(incident_id, closed_by="bc-09")

    assert result.grant_revoked_at is not None
    assert result.log_sealed_at is not None
    assert result.log_entries > 0

    grant = await container.grants.get_incident_grant(opened.grant.grant_id)
    assert grant is not None
    after = grant.revoked_at + timedelta(seconds=1)  # type: ignore[operator]
    assert grant.is_expired(after)
    with pytest.raises(GrantExpiredError):
        grant.assert_scope(next(iter(grant.scopes)), now=after)

    # A sealed log accepts nothing further.
    from firstdue.errors import AppendOnlyViolationError

    with pytest.raises(AppendOnlyViolationError):
        await session.recorder.record_benchmark(
            await session.controller.record_benchmark.__self__._require(incident_id)  # type: ignore[attr-defined]
            and __import__("firstdue.domain.incidents", fromlist=["Benchmark"]).Benchmark(
                benchmark_id="b-late",
                incident_id=incident_id,
                type=BenchmarkType.PAR,
                occurred_at=container.clock.now(),
                recorded_by="bc-09",
            )
        )


async def test_closing_produces_a_neris_draft_from_the_log(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    await session.emit_enriched(opened.incident.incident_id)

    result = await session.controller.close(opened.incident.incident_id, closed_by="bc-09")
    draft = result.neris_draft
    assert draft is not None
    assert draft.incident_id == opened.incident.incident_id
    assert draft.brief_versions == 2
    assert draft.log_entries == result.log_entries
    # Stated on the artifact: a draft, not a filing.
    assert "Not a filed report" in draft.disclaimer


@pytest.mark.degraded
async def test_an_unreachable_records_system_buffers_rather_than_blocks(
    container: Container, session: IncidentSession
) -> None:
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)

    rms = container.write_targets["department-rms"]
    rms.unavailable = True  # type: ignore[attr-defined]

    result = await session.controller.close(opened.incident.incident_id, closed_by="bc-09")
    # The incident closed anyway. The entries are buffered, not dropped.
    assert result.log_sealed_at is not None
    assert result.rms_still_buffered > 0

    rms.unavailable = False  # type: ignore[attr-defined]
    flush = await session.recorder.flush_to_rms(incident_id=opened.incident.incident_id)
    assert flush.flushed > 0
    assert flush.complete


async def test_the_incident_loop_runs_through_the_runtime(
    container: Container, session: IncidentSession
) -> None:
    """Incident agents run under the incident grant, not around it.

    The slow loop was routed through the runtime first; the incident loop kept
    calling its agents directly, so an incident produced no run records and the
    descriptors' scopes and latency targets applied to nothing on that path.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    emission = await session.run_enrichment(incident_id, correlation_id="corr_enrich")
    assert emission.stage.value == "ENRICHED"

    runtime = container.runtime
    invoked = {ref.split("@")[0] for ref, _ in runtime.invocations}  # type: ignore[attr-defined]
    assert "incident-interceptor" in invoked


async def test_an_incident_run_is_durable_and_names_its_version(
    container: Container, session: IncidentSession
) -> None:
    """A run record is what an investigation reads two years later."""
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    run = await session.fleet.run(
        "incident-interceptor",
        correlation_id="corr_enrich",
        ids={"incident_id": opened.incident.incident_id},
        grant=opened.grant,
    )
    assert run.completed
    assert run.record.agent_version
    assert run.record.finished_at is not None

    stored = await container.runs.get(run.record.run_id)
    assert stored is not None
    assert stored.status is run.record.status
