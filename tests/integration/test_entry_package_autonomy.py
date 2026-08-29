"""The loop composing an entry package for itself, and refusing to do it twice.

Everything the package needs already existed; what did not exist was a moment
at which the fleet decided the record was good enough to hand a crew a plan.
These tests are that moment: when it fires, when it deliberately does not, what
a fallback composition has to admit about itself, and how a console watching
``/log/stream`` finds out a package is waiting for two signatures.

The other half of the budget is here too. A wall the vision model cannot read
used to be picked again on every call, so one bad face consumed the whole sweep
and the three walls behind it were never flown.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.conflicts import ConflictResolution
from firstdue.domain.enums import Classification, FaceLabel, LogEntryType, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.profiles import ProfileEvent, ProfileEventType
from firstdue.domain.values import TextValue
from firstdue.domain.vision import VisionResult
from firstdue.domain.work import ApprovalStatus
from firstdue.errors import NotFoundError
from firstdue.extraction.coercion import coerce_value
from firstdue.incident.autonomy import (
    COMPOSE_DEADLINE,
    COMPOSITION_CAP,
    DELIVERY_ALLOWANCE,
    HARD_CEILING,
    TOTAL_BUDGET,
    AutonomyTrigger,
)
from firstdue.incident.drone import synthetic_frame
from firstdue.incident.intake import IntakeChannel
from firstdue.incident.packages import BRIEF_HALF, PATH_HALF, PackageStatus
from firstdue.incident.readiness import HAZARD_KEYS
from firstdue.incident.session import (
    PACKAGE_WORK_RESERVE_MS,
    IncidentSession,
    _brief_deadline_ms,
)
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"

NARRATIVE = (
    "Third floor of the apartment building is showing heavy smoke, "
    "two people still inside, and the gate is locked."
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=True,
        fixtures_dir=REPO_ROOT / "fixtures",
        demo_state_dir=tmp_path / ".demo-state",
        log_json=False,
    )


@pytest.fixture
def container(settings: Settings) -> Container:
    return build_container(settings)


@pytest.fixture
def session(container: Container) -> IncidentSession:
    return IncidentSession(container)


async def _open(
    container: Container, session: IncidentSession, *, answerable_at: datetime | None = None
) -> str:
    await run_slow_loop(container, approve=False)
    if answerable_at is not None:
        await _answerable(container, answerable_at)
    opened = await session.controller.open(
        address=DISPUTED_ADDRESS_ID, cad_ref="CAD-0001", alarm_level=2
    )
    await session.emit_instant(opened)
    return str(opened.incident.incident_id)


async def _answerable(container: Container, epoch: datetime) -> None:
    """Settle everything the slow loop left open, before the incident opens.

    The demo address is deliberately a mess -- an open disagreement about a
    load-bearing attribute and hazard attributes nobody ever filed -- which is
    why every package this scenario produces is a fallback. Four of the six
    criteria read the profile *snapshot*, and that is frozen at dispatch by
    design, so an incident cannot argue its way to ready once it has opened:
    the record either supported an entry plan when the bell went off or it did
    not. Reaching the ready trigger therefore means fixing the profile first,
    which is the slow loop's whole job.
    """
    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    base_version = profile.profile_version
    for index, key in enumerate(HAZARD_KEYS):
        if key in profile.facts and profile.facts[key].value.is_known:
            continue
        value = coerce_value(key, "false") or TextValue(text="not present")
        profile = profile.with_fact(
            StructuralFact(
                fact_id=f"fact_ready_{index}",
                address_id=DISPUTED_ADDRESS_ID,
                canonical_key=key,
                value=value,
                source_type=SourceType.FIRE_INSPECTION,
                source_ref="inspection/ready-fixture",
                source_snapshot_id="snapshot-ready-fixture",
                observed_at=epoch - timedelta(days=1),
                ingested_at=epoch - timedelta(days=1),
                confidence=0.9,
                classification=Classification.PUBLIC,
            ),
            event=ProfileEvent(
                event_id=f"pevt_ready_{index}",
                sequence=profile.next_sequence,
                occurred_at=epoch - timedelta(days=1),
                type=ProfileEventType.FACT_WRITTEN,
                actor="records-watcher",
                summary=f"{key} filed",
                canonical_keys=(key,),
                fact_ids=(f"fact_ready_{index}",),
            ),
        )

    settled = ConflictResolution(
        resolved_at=epoch - timedelta(days=1),
        resolving_record_id="inspection/ready-fixture",
        resolving_fact_id="fact_ready_0",
        resolved_by="inspector-1",
        note="Settled on a pre-incident inspection.",
    )
    profile = profile.model_copy(
        update={
            "conflicts": tuple(c.resolve(settled) for c in profile.conflicts),
            "profile_version": base_version + 1,
        }
    )
    await container.profiles.save(profile, expected_version=base_version)


async def _sweep(session: IncidentSession, incident_id: str, *, passes: int = 4) -> None:
    for index in range(passes):
        await session.run_drone_sweep_step(incident_id, correlation_id=f"corr-sweep-{index}")


async def _package_entries(container: Container, incident_id: str) -> list[dict[str, Any]]:
    stored = await container.incident_log.get_log(incident_id)
    return [
        entry.content for entry in stored.entries if entry.entry_type is LogEntryType.ENTRY_PACKAGE
    ]


# ------------------------------------------------------- composing on ready


async def test_the_loop_composes_a_package_the_first_time_the_record_supports_one(
    container: Container, session: IncidentSession, epoch: datetime
) -> None:
    """Nobody asked. The last wall landed, six criteria passed, a plan exists."""
    incident_id = await _open(container, session, answerable_at=epoch)
    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    await _sweep(session, incident_id)

    packages = await session.list_entry_packages(incident_id)
    assert len(packages) == 1
    package = packages[0]
    assert package.assessment.ready is True
    assert package.status is PackageStatus.AWAITING_APPROVAL

    entries = await _package_entries(container, incident_id)
    assert entries[0]["autonomy_trigger"] == str(AutonomyTrigger.READY)


async def test_composing_stages_two_approvals_and_sends_nothing(
    container: Container, session: IncidentSession
) -> None:
    """Autonomy composes and stages. It does not sign and it does not send."""
    incident_id = await _open(container, session)
    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    await _sweep(session, incident_id)

    package = (await session.list_entry_packages(incident_id))[0]
    assert package.sent_at is None
    assert package.outstanding_halves == (PATH_HALF, BRIEF_HALF)
    for approval_id in (package.path_approval_id, package.brief_approval_id):
        staged = await container.approvals.get(approval_id)
        assert staged is not None
        assert staged.status is ApprovalStatus.STAGED


async def test_it_composes_once_and_not_once_per_frame(
    container: Container, session: IncidentSession
) -> None:
    """Four faces, one package.

    The guard is the readiness verdict, not a counter: three more sweep steps
    against an unchanged record produce nothing, because nothing an entry
    package would be different about has moved.
    """
    incident_id = await _open(container, session)
    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    await _sweep(session, incident_id, passes=7)

    assert len(await session.list_entry_packages(incident_id)) == 1


# --------------------------------------------------------------- the fallback


async def test_a_fallback_composition_names_every_criterion_that_did_not_pass(
    container: Container, session: IncidentSession
) -> None:
    """No narrative was ever read, so the intake criterion cannot pass.

    The sweep terminating is the fallback, and the package it produces has to
    be legible as one: not ready, the failed criteria on the document and
    flattened onto the entry a console reads, and the trigger saying the fleet
    ran out of inputs rather than judging the record complete.
    """
    incident_id = await _open(container, session)
    await _sweep(session, incident_id)

    packages = await session.list_entry_packages(incident_id)
    assert len(packages) == 1
    package = packages[0]
    assert package.assessment.ready is False
    assert "intake.access-bound" in package.assessment.failed_ids
    assert package.assessment.summary.startswith("NOT READY")

    entry = (await _package_entries(container, incident_id))[0]
    assert entry["ready"] is False
    assert entry["autonomy_trigger"] == str(AutonomyTrigger.SWEEP_TERMINATED)
    assert "intake.access-bound" in entry["failed_criteria"]


async def test_a_refused_sweep_is_a_terminal_state_and_composes_immediately(
    container: Container, session: IncidentSession
) -> None:
    """A sweep that will never fly is not a sweep worth waiting out.

    ``sweep_permitted`` refuses a generated frame against a live vision model.
    No further coverage is coming, so the loop stages what it has rather than
    holding the deadline open for an aircraft that was refused takeoff.
    """
    incident_id = await _open(container, session)
    container.vision.model_ref = "vertex/gemini-vision"  # type: ignore[misc]

    result = await session.run_drone_sweep_step(incident_id, correlation_id="corr-sweep-0")
    assert result["flown"] is False

    packages = await session.list_entry_packages(incident_id)
    assert len(packages) == 1
    assert packages[0].assessment.ready is False
    assert "thermal.coverage" in packages[0].assessment.failed_ids


async def test_the_deadline_composes_when_nothing_else_ever_does(
    container: Container, session: IncidentSession
) -> None:
    """The trigger of last resort, driven the way the sleeping task drives it.

    The timer is not armed under ``AppEnv.TEST`` -- a test process must not
    schedule three-quarters of a minute it will never wait for -- so what is
    asserted here is the decision the task takes when it wakes, which is the
    part that can be wrong.
    """
    incident_id = await _open(container, session)
    assert await session.list_entry_packages(incident_id) == ()

    await session._consider_entry_package(incident_id, deadline_elapsed=True)

    packages = await session.list_entry_packages(incident_id)
    assert len(packages) == 1
    entry = (await _package_entries(container, incident_id))[0]
    assert entry["autonomy_trigger"] == str(AutonomyTrigger.DEADLINE)
    assert entry["ready"] is False


async def test_a_fallback_is_replaced_when_the_record_materially_changes(
    container: Container, session: IncidentSession, epoch: datetime
) -> None:
    """Composed early with gaps, composed again once the gaps closed.

    "Exactly once unless something materially changed" cuts both ways: the
    second composition here is the one an officer wants, because the first one
    said the walls were unscanned and they no longer are.
    """
    incident_id = await _open(container, session, answerable_at=epoch)
    await session._consider_entry_package(incident_id, deadline_elapsed=True)
    first = await session.list_entry_packages(incident_id)
    assert len(first) == 1
    assert first[0].assessment.ready is False

    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    await _sweep(session, incident_id)

    packages = await session.list_entry_packages(incident_id)
    assert len(packages) == 2
    assert packages[1].assessment.ready is True
    assert packages[1].package_id != packages[0].package_id


async def test_autonomy_is_off_when_a_department_turns_it_off(tmp_path: Path) -> None:
    container = build_container(
        Settings(
            app_env=AppEnv.TEST,
            use_fake_agents=True,
            fixtures_dir=REPO_ROOT / "fixtures",
            demo_state_dir=tmp_path / ".demo-state",
            log_json=False,
            entry_package_autonomy=False,
        )
    )
    session = IncidentSession(container)
    incident_id = await _open(container, session)
    await _sweep(session, incident_id)
    await session._consider_entry_package(incident_id, deadline_elapsed=True)

    assert await session.list_entry_packages(incident_id) == ()


# ------------------------------------------------------ the wall it cannot read


class _BlindOnAlpha:
    """A vision client that cannot read one wall and reads the rest.

    Keyed on the exact bytes the sweep generates for that face, because the
    synthetic frames are deterministic per address and face -- so this refuses
    Alpha however many times Alpha is offered, which is the failure the
    abandonment rule exists for.
    """

    model_ref = "fake/vision-1"

    def __init__(self, blind_to: bytes, delegate: Any) -> None:
        self._blind_to = blind_to
        self._delegate = delegate

    async def observe(self, *, image: bytes, mime_type: str, deadline_ms: int) -> VisionResult:
        if image == self._blind_to:
            return VisionResult(
                accepted=False,
                rejection_reason="the vision model did not answer within the deadline",
                model_ref=self.model_ref,
            )
        result: VisionResult = await self._delegate.observe(
            image=image, mime_type=mime_type, deadline_ms=deadline_ms
        )
        return result


async def test_one_unreadable_wall_does_not_consume_the_whole_sweep(
    container: Container, session: IncidentSession
) -> None:
    """Alpha never registers. Bravo, Charlie and Delta still get flown.

    Before the abandonment rule the sweep picked the first UNSCANNED face on
    every call, so a wall that could not be read was retried for ever and the
    three behind it were never reached at all -- the single worst thing that
    could happen to the 90 s budget.
    """
    incident_id = await _open(container, session)
    blind = synthetic_frame(address_id=DISPUTED_ADDRESS_ID, face=FaceLabel.ALPHA)
    container.vision = _BlindOnAlpha(blind, container.vision)  # type: ignore[assignment]
    session.fusion._vision = container.vision  # type: ignore[assignment]

    flown: list[str] = []
    for index in range(6):
        result = await session.run_drone_sweep_step(
            incident_id, correlation_id=f"corr-sweep-{index}"
        )
        if result.get("flown"):
            flown.append(str(result["face"]))
        if result.get("complete"):
            break

    assert flown.count("ALPHA") == 2, "one retry, then the sweep moves on"
    assert {"BRAVO", "CHARLIE", "DELTA"} <= set(flown)

    stored = await container.incident_log.get_log(incident_id)
    headlines = [
        str(entry.content.get("headline", ""))
        for entry in stored.entries
        if entry.entry_type is LogEntryType.AGENT_ANALYSIS
    ]
    assert any("gave up on the ALPHA face" in line for line in headlines)

    # Given up on is not read as clear. The face is still UNSCANNED, so the
    # criterion that measures coverage still fails and says which wall.
    assessment = await session.assess_entry_readiness(incident_id)
    thermal = next(c for c in assessment.criteria if c.criterion_id == "thermal.coverage")
    assert thermal.passed is False
    assert "ALPHA" in thermal.refs


# -------------------------------------------------------- what the console sees


def test_the_log_stream_alone_tells_a_console_a_package_awaits_approval(
    app_client: TestClient,
) -> None:
    """The detection contract a UI is built against.

    One frame, one entry type, one field. A console filtering ``/log/stream``
    for ``entry_type == "ENTRY_PACKAGE"`` and ``content.status ==
    "AWAITING_APPROVAL"`` has everything it needs to raise the card: which
    package, which halves are outstanding, which approval endpoints to post to,
    whether the readiness verdict was ready, and which criteria were not.
    """
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    opened = app_client.post(
        f"{PREFIX}/incidents",
        json={
            "address": DISPUTED_ADDRESS_ID,
            "cad_ref": "CAD-0001",
            "alarm_level": 2,
            "intake_narrative": NARRATIVE,
        },
    )
    assert opened.status_code == 201
    incident_id = opened.json()["incident_id"]
    for _ in range(4):
        app_client.post(f"{PREFIX}/incidents/{incident_id}/drone-sweep")

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/log/stream") as response:
        assert response.status_code == 200
        raw = response.read().decode()

    awaiting = [
        json.loads(line.partition(":")[2].strip())
        for line in raw.splitlines()
        if line.startswith("data:") and '"ENTRY_PACKAGE"' in line
    ]
    assert len(awaiting) == 1
    frame = awaiting[0]
    assert frame["entry_type"] == "ENTRY_PACKAGE"

    content = frame["content"]
    assert content["status"] == "AWAITING_APPROVAL"
    assert content["outstanding"] == [PATH_HALF, BRIEF_HALF]
    assert content["path_approval_id"].endswith(PATH_HALF)
    assert content["brief_approval_id"].endswith(BRIEF_HALF)
    # This scenario's profile carries an unsettled disagreement, so what the
    # console gets here is the fallback -- and it is legible as one from the
    # same frame. Nothing about "awaiting approval" changes; what changes is
    # that the card has to render the gaps rather than a clean verdict.
    assert content["autonomy_trigger"] == str(AutonomyTrigger.SWEEP_TERMINATED)
    assert content["ready"] is False
    assert content["failed_criteria"]
    # And the whole document is on the same frame, so the card renders without
    # a second request.
    assert content["package"]["package_id"] == content["package_id"]

    # The console posts to exactly the ids the frame gave it.
    for half in (PATH_HALF, BRIEF_HALF):
        signed = app_client.post(
            f"{PREFIX}/incidents/{incident_id}/entry-packages/{content['package_id']}"
            f"/approvals/{half}"
        )
        assert signed.status_code == 200


# ------------------------------------------ what a live model can and cannot cost
#
# Everything above ran in fake mode, where the model answers in microseconds.
# Live it does not, and the composition runs inside a run the runtime cancels at
# the ``incident-interceptor`` descriptor's six seconds. That is the difference
# between a fake-mode suite that passed and three real incidents that produced
# no card at all, so it is tested with a model that behaves the way the bad one
# did: it accepts the call and never comes back.


class SilentModel:
    """Accepts every ``compose`` and never answers it. Nothing else is reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def triage(self, **kwargs: Any) -> Any:  # pragma: no cover - not on this path
        raise AssertionError("composing a package must not triage")

    async def extract(self, **kwargs: Any) -> Any:  # pragma: no cover - not on this path
        raise AssertionError("composing a package must not extract")

    def compose_stream(self, **kwargs: Any) -> Any:  # pragma: no cover - not on this path
        raise AssertionError("composing a package does not stream")

    async def explain(self, **kwargs: Any) -> Any:  # pragma: no cover - not on this path
        raise AssertionError("composing a package must not explain")


@pytest.mark.invariant
async def test_a_model_that_never_answers_still_stages_the_package(
    container: Container, session: IncidentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure, end to end and through the runtime.

    This is the one that was costing the user the card. The crew brief's model
    deadline was a flat 4 s written beside a run the runtime caps at 6 s, and
    nothing bounded the call itself -- so a slow or hung model ran the run out,
    the handler was cancelled part-way through ``compose_entry_package``, no
    package was ever stored, and the exception that reached
    ``_consider_entry_package`` was swallowed by design. No document, no
    approval cards, no log entry, and a two-minute clock running out on an empty
    screen.

    What has to be true instead: the wording is given up, the package is staged
    anyway, and the reason the wording was given up is written on it.
    """
    # The wording is worth four seconds; nothing here needs to spend four
    # seconds proving it is bounded, and the bound is the same bound. The stub
    # ignores the deadline it is handed, so the only thing that can end this
    # call is the timeout ``compose`` wraps around it.
    monkeypatch.setattr("firstdue.incident.crewbrief.CREW_BRIEF_DEADLINE_MS", 700)
    incident_id = await _open(container, session)
    container.model = SilentModel()  # type: ignore[assignment]

    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-silent"
    )

    assert package.status is PackageStatus.AWAITING_APPROVAL
    assert package.brief.prose_source == "deterministic"
    # Stated, never blank: an empty rejection means "no model was wired", which
    # is a different and much less interesting thing to have happened.
    assert package.brief.prose_rejection == "UPSTREAM_TIMEOUT"
    assert package.brief.prose
    assert package.brief.claims
    # And it is in the record, which is what the console reads.
    assert (await _package_entries(container, incident_id))[0]["status"] == "AWAITING_APPROVAL"
    stored = await session.list_entry_packages(incident_id)
    assert [one.package_id for one in stored] == [package.package_id]


async def test_a_run_almost_out_of_time_gives_up_the_wording_not_the_package(
    container: Container, session: IncidentSession
) -> None:
    """The clamp, from the other side, and it costs no wall clock to prove.

    The composition is handed a run deadline that the assessment and the solve
    have all but spent. Before, the flat four-second model deadline was spent
    anyway and the runtime cancelled the handler on top of it. Now the wording
    is refused for time -- in time to stage the document it was going inside.
    """
    incident_id = await _open(container, session)
    container.model = SilentModel()  # type: ignore[assignment]

    package = await session.compose_entry_package(
        incident_id,
        deadline=container.clock.now() + timedelta(milliseconds=PACKAGE_WORK_RESERVE_MS),
    )

    assert container.model.calls == 0
    assert package.status is PackageStatus.AWAITING_APPROVAL
    assert package.brief.prose_source == "deterministic"
    assert package.brief.prose_rejection == "NO_MODEL_BUDGET"


async def test_the_wording_never_gets_the_budget_the_staging_needs(
    container: Container, session: IncidentSession
) -> None:
    """The reserve, as arithmetic rather than as a hope.

    Five record writes follow the synthesis -- two approval cards, the package
    entry, two analysis entries -- and they are what makes the package exist. A
    model deadline that did not leave room for them is a model deadline that
    trades the package for a paragraph.
    """
    started = container.clock.now()
    run_deadline = started + timedelta(milliseconds=6_000)
    assert _brief_deadline_ms(run_deadline, started) == 6_000 - PACKAGE_WORK_RESERVE_MS
    # And it shrinks with what the assessment and the solve already spent.
    late = started + timedelta(milliseconds=4_000)
    assert _brief_deadline_ms(run_deadline, late) == 2_000 - PACKAGE_WORK_RESERVE_MS
    # No declared deadline leaves the crew brief's own default standing.
    assert _brief_deadline_ms(None, started) is None


# ------------------------------------------------- saying why there is no card


async def test_a_failed_composition_is_shrugged_at_but_not_forgotten(
    container: Container, session: IncidentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy is unchanged. What it leaves behind is not.

    A composition that raises must never break the sweep -- that is why the
    handler swallows everything. It used to swallow the reason too, and three
    live incidents in a row were diagnosed from a log line reading
    ``NotFoundError`` and nothing else.
    """
    incident_id = await _open(container, session)

    async def refuses(*args: Any, **kwargs: Any) -> Any:
        raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})

    monkeypatch.setattr(session, "run_entry_package", refuses)

    # Swallowed: the caller is told nothing and nothing propagates.
    assert await session._consider_entry_package(incident_id, deadline_elapsed=True) is None

    report = await session.describe_autonomy(incident_id)
    assert report.attempts == 1
    assert report.failures == 1
    assert report.failed_trigger == str(AutonomyTrigger.DEADLINE)
    assert report.failed_error_type == "NotFoundError"
    assert "profile snapshot is missing" in report.failed_error_message
    # Which criteria were outstanding at the time, so "it died" and "it was
    # never triggered" are answerable apart.
    assert report.failed_criteria
    assert report.composed_package_id == ""


async def test_the_diagnostic_says_which_of_the_silent_declines_happened(
    container: Container, session: IncidentSession
) -> None:
    """Four ways to have no card, and they have to look different from outside."""
    incident_id = await _open(container, session)

    waiting = await session.describe_autonomy(incident_id)
    assert waiting.autonomy_enabled is True
    assert waiting.tracked is True
    assert waiting.failures == 0
    assert waiting.packages == 0
    # The record has gaps and there is still time: the ready trigger is waiting
    # on exactly these, and the deadline is the guarantee that it stops waiting.
    assert waiting.outstanding_criteria
    assert waiting.assessment_error == ""
    assert waiting.deadline_at == waiting.opened_at + COMPOSE_DEADLINE
    # Not armed under AppEnv.TEST -- a test process must not schedule two
    # minutes of wall clock -- and the report says so rather than implying a
    # timer that does not exist.
    assert waiting.deadline_armed is False

    await session._consider_entry_package(incident_id, deadline_elapsed=True)
    composed = await session.describe_autonomy(incident_id)
    assert composed.composed_trigger == str(AutonomyTrigger.DEADLINE)
    assert composed.composed_package_id
    assert composed.packages == 1
    assert composed.failures == 0

    # An incident this process never opened declines for a fourth reason again,
    # and it is not a failure -- there is simply nobody here holding a deadline.
    unknown = await session.describe_autonomy("inc-not-ours")
    assert unknown.tracked is False
    assert unknown.deadline_at is None
    assert unknown.assessment_error.startswith("NotFoundError")
    assert unknown.outstanding_criteria == ()


def test_a_console_with_no_card_can_ask_the_backend_why(app_client: TestClient) -> None:
    """The curl a human runs at two in the morning, over the real route."""
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    opened = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": DISPUTED_ADDRESS_ID, "cad_ref": "CAD-DIAG", "alarm_level": 2},
    )
    assert opened.status_code == 201
    incident_id = opened.json()["incident_id"]

    before = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/diagnostics")
    assert before.status_code == 200
    body = before.json()
    assert body["incident_id"] == incident_id
    assert body["autonomy_enabled"] is True
    assert body["tracked"] is True
    assert body["packages"] == 0
    assert body["outstanding_criteria"]

    for _ in range(4):
        app_client.post(f"{PREFIX}/incidents/{incident_id}/drone-sweep")

    after = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/diagnostics").json()
    assert after["packages"] == 1
    assert after["composed_trigger"] == str(AutonomyTrigger.SWEEP_TERMINATED)
    assert after["composed_package_id"]
    assert after["failures"] == 0
    # The route is not shadowed by the id-shaped one beside it: "diagnostics" is
    # never read as a package id, which is what the declaration order buys.
    missing = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/pkg-nope")
    assert missing.status_code == 404


def test_the_budget_arithmetic_leaves_room_for_the_composition_it_triggers() -> None:
    """The deadline is only defensible if what follows it fits in what is left.

    Against the ceiling, not the target, and that is the whole change. What a
    commander is promised is a card at two minutes; the deadline is the last
    instant a composition can start and still keep that promise, so the three
    terms have to *reach* 120 s rather than beat 90 s. Beating 90 s is the ready
    trigger's job and it composes the moment the record supports it.

    Equality is the pass condition here. A sum under the ceiling would mean the
    fallback fires earlier than it has to and pre-empts a sweep that was merely
    slow; a sum over it is a broken promise.
    """
    assert COMPOSE_DEADLINE + COMPOSITION_CAP + DELIVERY_ALLOWANCE == HARD_CEILING
    # And the fallback is genuinely the last resort: everything the loop does
    # on a healthy incident finishes inside the target it is measured against.
    assert TOTAL_BUDGET < COMPOSE_DEADLINE


def test_the_log_stream_delivers_the_incidents_very_first_entry(
    app_client: TestClient,
) -> None:
    """Sequence zero is an entry, not a resume point.

    Log sequences are zero-based and the first one is the dispatch benchmark --
    the moment the incident opened. A fresh connection has nothing to resume
    from, and that was expressed as ``resume_from = 0`` and filtered with
    ``sequence <= resume_from``, so entry 0 was skipped on every connection this
    stream ever served. The console watches the log *only* through this stream,
    so the first thing the incident recorded was permanently invisible there
    while sitting in plain view in ``GET /log``.

    Asserted against the document endpoint rather than against a literal, so
    this stays true if the loop ever records something else first.
    """
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    opened = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": DISPUTED_ADDRESS_ID, "cad_ref": "CAD-0001", "alarm_level": 2},
    )
    assert opened.status_code == 201
    incident_id = opened.json()["incident_id"]

    document = app_client.get(f"{PREFIX}/incidents/{incident_id}/log").json()
    recorded = [entry["sequence"] for entry in document["entries"]]
    assert recorded[0] == 0, "the log itself no longer starts at zero"

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/log/stream") as response:
        assert response.status_code == 200
        raw = response.read().decode()
    streamed = [
        int(line.partition(":")[2].strip()) for line in raw.splitlines() if line.startswith("id:")
    ]
    assert streamed == recorded

    # And a real resume still means "after this one". A tablet that has seen
    # entry 0 must not be sent it again.
    with app_client.stream(
        "GET",
        f"{PREFIX}/incidents/{incident_id}/log/stream",
        headers={"Last-Event-ID": "0"},
    ) as response:
        resumed = response.read().decode()
    assert 0 not in [
        int(line.partition(":")[2].strip())
        for line in resumed.splitlines()
        if line.startswith("id:")
    ]


async def test_a_readiness_criterion_that_moves_is_recorded_where_it_moved(
    container: Container, session: IncidentSession
) -> None:
    """The silent probe writes down what it learns, and only that.

    Readiness is re-evaluated at every point an input to it changes, and that
    evaluation recorded nothing -- so a wall being flown moved
    ``thermal.coverage`` from four faces UNSCANNED to three with no trace, and
    the loop read as idle between the intake and the package.

    Two claims, and the second is the one that keeps this from being padding:
    a criterion that moves leaves an entry at the moment it moved, and a
    criterion that did not move leaves nothing at all.
    """
    incident_id = await _open(container, session)

    async def headlines() -> list[str]:
        log = await container.incident_log.get_log(incident_id)
        return [
            str((entry.content or {}).get("headline", ""))
            for entry in log.entries
            if entry.entry_type is LogEntryType.AGENT_ANALYSIS
        ]

    # The first evaluation states where the incident starts: six criteria, each
    # under its own name.
    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel="CALL_911",
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    opening = [h for h in await headlines() if " opens " in h]
    assert len(opening) == 6, opening
    assert any("thermal.coverage opens NOT met" in h for h in opening)

    # A wall is flown. Coverage genuinely reads a different record, and that is
    # written down at the pass that caused it.
    before = len(await headlines())
    await session.run_drone_sweep_step(incident_id, correlation_id="corr-sweep-1")
    moved = [h for h in (await headlines())[before:] if h.startswith("readiness ")]
    assert any("thermal.coverage" in h for h in moved), moved
    # Nothing else moved: geometry, hazards, conflicts, freshness and the intake
    # all read the same record they did a moment ago.
    assert not any("geometry.present" in h for h in moved), moved
    assert not any("intake.access-bound" in h for h in moved), moved


def test_the_priced_leg_cap_covers_an_ordinary_route() -> None:
    """The per-leg entries are the route, not a stair count.

    A route into a low-rise runs to five or six legs. The cap sits above that so
    an ordinary solve records all of them, and exists only for the pathological
    case where a target storey turns the stairwell into a dozen legs.
    """
    assert IncidentSession.MAX_PRICED_LEGS >= 6
