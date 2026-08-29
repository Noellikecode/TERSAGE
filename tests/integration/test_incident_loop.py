"""The incident loop, end to end.

Every clause of the phase's acceptance criteria has a test here: the instant
brief lands inside its budget with no model call, it is in the log before it can
be transmitted, degraded sources still produce a brief that says what is
missing, a cold profile says the structure is unknown, an IC resolution creates
an amendment and bumps the profile version, and closing revokes the grant and
seals the log.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.enums import BenchmarkType, BriefStage, FaceLabel, LogEntryType, PolicyAction
from firstdue.domain.keys import Keys
from firstdue.errors import BriefNotPersistedError, GrantExpiredError
from firstdue.incident.drone import SYNTHETIC_SOURCE
from firstdue.incident.fusion import THERMAL_CAVEAT, ThermalFrame
from firstdue.incident.intake import IntakeChannel
from firstdue.incident.session import IncidentSession
from firstdue.ports.audit import AuditEventKind
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


async def test_concurrent_resource_requests_do_not_cross_their_outcomes(
    container: Container, session: IncidentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agencies notified at once, each told about the right one.

    The outcome a handler produces was parked under the *incident* id, which is
    correct only while one request is in flight. The console sent them one at a
    time, so it held -- until the console sent them together, at which point one
    caller popped the other's outcome and a notification to one agency was
    reported under another's name. Nothing in the runtime serialises these.

    The yield is injected rather than waited for. Fake mode completes a request
    without ever suspending, so the two tasks never interleave and the bug is
    invisible; against real services every one of these is a network call. The
    suspension goes on the run-record save -- a Firestore write in production,
    and the last thing that happens between a handler filing its outcome and its
    caller collecting it. That is exactly the window the outcome can be taken in.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    saved = container.runs.save

    async def slow_save(record: object) -> object:
        await asyncio.sleep(0)
        return await saved(record)  # type: ignore[arg-type]

    monkeypatch.setattr(container.runs, "save", slow_save)

    # The same seven the console sends on dispatch, together.
    kinds = [
        "water-supply",
        "mutual-aid",
        "county-oem",
        "public-works",
        "exposure",
        "building-department",
        "utility-conditions",
    ]
    outcomes = await asyncio.gather(
        *(
            session.run_resource_request(
                incident_id,
                correlation_id=f"corr_{kind}",
                kind_id=kind,
                detail="requested by the console",
                approval_id=None,
            )
            for kind in kinds
        )
    )

    # Each request got its own answer back, about the kind it asked for.
    assert [o.kind_id for o in outcomes] == kinds


async def test_every_incident_agent_leaves_a_trace_the_console_can_read(
    container: Container, session: IncidentSession
) -> None:
    """The console's evidence is the audit log, not the incident log.

    They answer different questions: the incident log is the record of *the
    fire*, the audit log is the record of *the fleet*. `sensor-fusion` and
    `incident-recorder` did their whole job through the recorder, which wrote
    the incident log and nothing else -- so the console, which reads only the
    audit log, drew both as idle for the length of an incident they were busy
    through. `incident-interceptor` had the same problem for a different reason:
    a brief does not go through the recorder's ordinary append path at all.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_intake(
        incident_id,
        narrative="Heavy smoke from the top floor, flames in two windows.",
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr_trace",
    )
    await session.run_resource_request(
        incident_id,
        correlation_id="corr_trace_water",
        kind_id="water-supply",
        detail="hydrant use",
        approval_id=None,
    )

    events = await container.audit.list_events(limit=500)
    actors = {e.actor for e in events if e.kind is AuditEventKind.AGENT_STEP}
    assert {"incident-recorder", "incident-interceptor"} <= actors

    # Every step names the entry it stands for and where that entry sits, which
    # is enough to find it -- and nothing copied out of it. The content lives in
    # the incident log with its provenance; a second uncited copy here would be
    # a claim nobody could check.
    #
    # Scoped to this incident: the slow-loop watchers write `AGENT_STEP` too,
    # about districts and addresses, and they carry their own detail shape.
    for event in events:
        if event.kind is AuditEventKind.AGENT_STEP and event.target == incident_id:
            assert event.detail.get("entry"), event
            assert event.detail.get("sequence"), event
            assert " " not in event.detail["entry"], event


async def test_a_declared_synthetic_sweep_marks_every_reading_it_produces(
    container: Container, session: IncidentSession
) -> None:
    """The condition the permission is granted on.

    A generated frame read by a real model is allowed only because the record
    says what it is everywhere it appears. If that labelling ever stops
    happening, the refusal in `sweep_permitted` is protecting nothing and the
    console is showing an unmarked reading of an imaginary building -- so this
    asserts the label, not merely that the sweep ran.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    flown = await session.run_drone_sweep_step(incident_id, correlation_id="corr_sweep")
    assert flown["flown"] is True
    assert flown["source"] == SYNTHETIC_SOURCE

    entries = (await container.incident_log.get_log(incident_id)).entries
    readings = [
        e
        for e in entries
        if e.entry_type is LogEntryType.AGENT_ANALYSIS
        and (e.content or {}).get("agent_ref", "").startswith("sensor-fusion")
    ]
    assert readings, "the sweep recorded nothing under sensor-fusion"
    for reading in readings:
        content = reading.content or {}
        # In the headline an officer actually reads, not buried in a detail tail.
        assert "SIMULATED" in str(content.get("headline", "")), content
        # And in the references, so the record itself names the source.
        assert SYNTHETIC_SOURCE in list(content.get("refs", [])), content


async def test_a_refused_sweep_is_recorded_rather_than_returned_silently(
    container: Container, session: IncidentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining is this agent working, and it left no trace anywhere.

    The reason went back to the caller as a string and the console printed it in
    a corner. `sensor-fusion` -- asked to do the one thing it exists for, and
    having given a considered answer -- read as an agent that had done nothing
    at all for the length of the incident.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    # A live model with no simulation declared: the ordinary refusal.
    monkeypatch.setattr(type(container.vision), "model_ref", property(lambda _: "gemini-3.5-flash"))

    result = await session.run_drone_sweep_step(incident_id, correlation_id="corr_refused")
    assert result["flown"] is False

    entries = (await container.incident_log.get_log(incident_id)).entries
    declined = [
        e
        for e in entries
        if e.entry_type is LogEntryType.AGENT_ANALYSIS
        and "declined" in str((e.content or {}).get("headline", ""))
    ]
    assert declined, "a refusal that records nothing is indistinguishable from an idle agent"
    assert "generated" in str((declined[0].content or {}).get("detail", ""))


def _analyses(entries, agent_id: str) -> list:
    """Every analysis one agent filed under its own id."""
    return [
        e
        for e in entries
        if e.entry_type is LogEntryType.AGENT_ANALYSIS
        and str((e.content or {}).get("agent_ref", "")).startswith(agent_id)
    ]


# ------------------------------------------------ the slow loop, cited on the card
#
# The incident agents' cards described their own work and stopped. Read down the
# stream, `sensor-fusion` looked like it had worked out which wall it was
# pointing at, and the interceptor looked like it had decided six criteria on
# the spot. Neither is true: the footprint was measured before the bell and the
# criteria are checks on facts other agents filed. These tests are that
# dependency made visible, and -- the harder half -- kept honest.


async def _cards(container: Container, incident_id: str, agent_id: str) -> list[str]:
    """Headline and detail of every card one agent filed, as one string each."""
    entries = (await container.incident_log.get_log(incident_id)).entries
    return [
        f"{e.content['headline']} :: {e.content['detail']}" for e in _analyses(entries, agent_id)
    ]


def _card(cards: list[str], needle: str) -> str:
    matched = [card for card in cards if needle in card]
    assert matched, f"no card matching {needle!r} in {cards}"
    return matched[0]


async def test_the_sweeps_cards_name_whose_measurement_resolved_the_wall(
    container: Container, session: IncidentSession
) -> None:
    """`sensor-fusion` cannot pick a wall without work the slow loop already did.

    The footprint decides which wall a bearing points at -- not the caller and
    not the model -- so the headline says whose loop supplied it, and the detail
    names the agents that filed the attributes it is a function of. Read off
    ``produced_by_agent`` on the snapshot, never mapped from a source type.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_drone_sweep_step(incident_id, correlation_id="corr-sweep-0")

    read = _card(await _cards(container, incident_id, "sensor-fusion"), "resolved it to")
    assert "on the footprint the slow loop measured" in read
    assert "the structural facts it is a function of were filed by" in read
    assert "records-watcher" in read

    entries = (await container.incident_log.get_log(incident_id)).entries
    resolved = [
        e
        for e in _analyses(entries, "sensor-fusion")
        if "resolved it to" in str(e.content["headline"])
    ]
    # In the refs as well as the prose: an agent id is an id, and a console
    # filtering the stream should be able to find the dependency without
    # parsing a sentence.
    assert "records-watcher" in resolved[0].content["refs"]


async def test_the_readiness_cards_name_the_slow_loop_work_each_one_checks(
    container: Container, session: IncidentSession
) -> None:
    """Four of the six criteria are checks on another agent's output.

    Each cites what the record actually holds: an author for the facts, a *rule*
    for the conflict -- because a conflict records a ``rule_id`` and no actor --
    and, for freshness, neither, because what that one checks belongs to the
    read rather than to any agent.
    """
    await _warm(container)
    opened = await _open(session)
    await session.emit_instant(opened)
    await session.assess_entry_readiness(opened.incident.incident_id)

    cards = await _cards(container, opened.incident.incident_id, "incident-interceptor")

    geometry = _card(cards, "readiness geometry.present")
    assert "the attributes the geometry is a function of were filed by" in geometry
    assert "geometry-watcher" in geometry

    hazard = _card(cards, "readiness hazard.resolved")
    assert "these attributes were filed by records-watcher" in hazard

    conflicts = _card(cards, "readiness conflicts.load-bearing")
    # The rule, and explicitly not an agent: nothing on a conflict records one.
    assert "detected by rule permit-vs-lidar-story-count" in conflicts
    assert "structure-watch" not in conflicts

    fresh = _card(cards, "readiness snapshot.fresh")
    assert "the freshness belongs to the read rather than to any agent" in fresh

    # And the two criteria that check *this* incident's own agents are left
    # alone. Crediting the slow loop for a thermal frame or a 911 narrative
    # would be the fabrication the whole exercise is guarding against.
    for own in ("readiness thermal.coverage", "readiness intake.access-bound"):
        assert "filed by" not in _card(cards, own)


async def test_the_entry_path_card_names_what_priced_the_route(
    container: Container, session: IncidentSession
) -> None:
    """The A* is the cheap part. The cost terms are the slow loop's."""
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    for index in range(4):
        await session.run_drone_sweep_step(incident_id, correlation_id=f"corr-sweep-{index}")
    await session.solve_entry_path(incident_id, target_level=0)

    solved = _card(
        await _cards(container, incident_id, "incident-interceptor"), "solved the entry path"
    )
    assert "over the geometry the slow loop measured" in solved
    assert "hazard costs from facts filed by records-watcher" in solved
    assert "found by permit-vs-lidar-story-count" in solved


async def test_an_input_with_no_recorded_author_is_described_and_not_credited(
    container: Container, session: IncidentSession
) -> None:
    """The limit on all of the above, and the one that matters most.

    A cold address has a real, missing input -- no footprint -- and nobody on
    record who would have measured one. The card says the loop, because the
    absence of geometry is exactly what the snapshot carries; it must not say an
    agent, because "geometry-watcher never ran" is a claim about a process this
    incident cannot see and an append-only record would keep it forever.
    """
    await _warm(container)
    opened = await _open(session, address=COLD_ADDRESS)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    result = await session.analyze_imagery(
        incident_id,
        image=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        mime_type="image/png",
        camera_bearing_deg=90.0,
        source="ground-station",
    )
    assert result["cold_start"] is True

    refusal = _card(
        await _cards(container, incident_id, "sensor-fusion"), "cannot read this address"
    )
    assert "the slow loop never profiled it" in refusal
    assert "carries no footprint" in refusal
    # Named nobody. Not one of the agents that would plausibly have done it.
    for agent in ("geometry-watcher", "records-watcher", "hazard-watcher", "structure-watch"):
        assert agent not in refusal


async def test_a_finished_sweep_says_it_is_finished_instead_of_going_quiet(
    container: Container, session: IncidentSession
) -> None:
    """Deciding not to fly is a decision, and it looked like the sweep stalling.

    Three faces each produced a card and finishing produced nothing, so the
    console showed an agent that had been working and then stopped -- which is
    what a crashed agent also looks like.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    while (await session.run_drone_sweep_step(incident_id, correlation_id="corr_sweep"))["flown"]:
        pass

    entries = (await container.incident_log.get_log(incident_id)).entries
    analyses = _analyses(entries, "sensor-fusion")

    # Which wall is flown next, and why: the choice, not only its answer.
    chosen = [e for e in analyses if "flying a SIMULATED pass" in str(e.content["headline"])]
    assert chosen, "the agent picked a wall on every pass and recorded none of them"
    assert "UNSCANNED" in str(chosen[0].content["detail"])

    complete = [e for e in analyses if "complete" in str(e.content["headline"])]
    assert complete, "the sweep ended and the agent that ended it said nothing"
    assert complete[-1].sequence == max(e.sequence for e in analyses)


async def test_a_frame_on_an_unprofiled_address_is_recorded_as_a_refusal(
    container: Container, session: IncidentSession
) -> None:
    """Cold start is the two-loop dependency failing, and it was a return value.

    This agent cannot attribute a frame to a wall that nobody measured, so the
    wall stays UNSCANNED and somebody has to fly it again -- an operational fact
    that reached the caller as a string and the incident's record not at all.
    """
    await _warm(container)
    opened = await _open(session, address=COLD_ADDRESS)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    result = await session.analyze_imagery(
        incident_id,
        image=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        mime_type="image/png",
        camera_bearing_deg=90.0,
        source="ground-station",
    )
    assert result["registered"] is False
    assert result["cold_start"] is True

    entries = (await container.incident_log.get_log(incident_id)).entries
    refused = _analyses(entries, "sensor-fusion")
    assert refused, "the frame was refused and sensor-fusion read as idle"
    # The headline names the loop whose work is missing, because that is the
    # whole content of the refusal: this agent resolves a bearing against a
    # footprint and there is no footprint. It names no *agent* -- nothing on
    # this snapshot records who would have measured one.
    headline = str(refused[0].content["headline"])
    assert "the slow loop never profiled it" in headline
    detail = str(refused[0].content["detail"])
    assert "carries no footprint" in detail
    assert "The slow loop is what supplies it" in detail
    assert opened.incident.address_id in refused[0].content["refs"]


async def test_every_resource_request_records_the_gateways_answer(
    container: Container, session: IncidentSession
) -> None:
    """Only a *sent* notification was in the log.

    So the outcome the whole governance model exists to produce -- a commitment
    staged on a chief's card and sent to nobody -- left no entry at all, and an
    approval gate whose firing is invisible is indistinguishable from an agent
    that never asked.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    sent = await session.request_resource(
        incident_id, kind_id="water-supply", detail="hydrant in use", approval_id=None
    )
    staged = await session.request_resource(
        incident_id, kind_id="gas-shutoff", detail="", approval_id=None
    )
    assert sent.action is PolicyAction.ALLOW
    assert staged.action is PolicyAction.REQUIRE_APPROVAL

    entries = (await container.incident_log.get_log(incident_id)).entries
    decisions = _analyses(entries, "agency-notifier")
    assert len(decisions) == 2

    cleared = decisions[0].content
    assert "cleared" in str(cleared["headline"])
    assert sent.rule_id in cleared["refs"] and sent.decision_id in cleared["refs"]

    waiting = decisions[1].content
    assert "chief" in str(waiting["headline"])
    assert staged.approval_id in waiting["refs"]
    # And nothing about it claims a partner was told.
    assert "nobody has been told" in str(waiting["detail"])


async def test_the_enriched_stage_says_whether_prose_was_actually_written(
    container: Container, session: IncidentSession
) -> None:
    """A brief with no narrative because none was wanted and a brief with no
    narrative because the composition was refused look identical on screen.

    ``BRIEF_EMITTED`` carries both booleans as fields, which is the right shape
    for a record and the wrong shape for a card: nothing in it reads as an agent
    having composed anything.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    emission = await session.emit_enriched(incident_id)

    entries = (await container.incident_log.get_log(incident_id)).entries
    composed = [
        e
        for e in _analyses(entries, "incident-interceptor")
        if "composed the enriched brief" in str(e.content["headline"])
    ]
    assert len(composed) == 1
    detail = str(composed[0].content["detail"])
    assert ("accepted" in detail) is emission.narrative_available
    assert emission.emission_id in composed[0].content["refs"]


async def test_the_360_records_whether_the_observation_reached_the_profile(
    container: Container, session: IncidentSession
) -> None:
    """Two facts the resolution entry cannot carry.

    Whether the officer's words parsed as the type the attribute is measured in
    -- a resolution kept as free text is still authoritative and comparable to
    nothing -- and whether the durable write actually landed. Losing the race
    with a slow-loop pass is a correct outcome and a silent one, and it means
    the next incident at this address opens against the disagreement this one
    settled.
    """
    await _warm(container)
    opened = await _open(session)
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)

    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    conflict = profile.open_conflicts[0]
    await session.resolve(
        incident_id,
        conflict_id=conflict.conflict_id,
        observed_value="3",
        resolved_by="bc-09",
        note="Walked the Charlie side.",
    )

    entries = (await container.incident_log.get_log(incident_id)).entries
    settled = [
        e
        for e in _analyses(entries, "incident-interceptor")
        if "settled" in str(e.content["headline"])
    ]
    assert settled, "the 360 resolution left no entry naming the agent that wrote it"
    content = settled[0].content
    assert conflict.canonical_key in str(content["headline"])
    assert "parsed as" in str(content["detail"])
    assert conflict.conflict_id in content["refs"]
