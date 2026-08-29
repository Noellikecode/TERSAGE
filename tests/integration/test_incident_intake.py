"""The interceptor end to end: intake, amendment, routing, record.

The acceptance criteria for the merge, each with a test here: the instant brief
lands before the intake is read and is unaffected by it, a caller report never
becomes a structural fact and never displaces a filed one, routing wakes agents
by their declared capabilities under the incident's own grant, the whole thing
is in the incident log, and a cold profile still says it has nothing on file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.enums import AssertionStatus, BriefStage, LogEntryType
from firstdue.domain.keys import IntakeKeys, Keys
from firstdue.incident.intake import IntakeChannel
from firstdue.incident.reconciler import COLD_START_NOTE
from firstdue.incident.session import IncidentSession
from firstdue.services.grants import INCIDENT_SCOPES
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COLD_ADDRESS = "sf-3120-24th"

#: A 911 call as one actually arrives: prose, panicked, partly wrong.
CALL = (
    "Caller reports heavy smoke on the third floor of the apartment building. "
    "Two people are still inside. The driveway is blocked by a delivery truck. "
    "There are propane cylinders by the back door. Dispatcher says second alarm."
)


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


async def _open(session: IncidentSession, address: str = DISPUTED_ADDRESS_ID, *, alarm: int = 1):
    return await session.controller.open(address=address, cad_ref="CAD-0001", alarm_level=alarm)


async def _intercept(session: IncidentSession, opened, *, narrative: str = CALL):
    return await session.run_intake(
        opened.incident.incident_id,
        narrative=narrative,
        channel=IntakeChannel.CALL_911,
        source_ref="call/CAD-0001",
        correlation_id="corr_intake",
    )


# ------------------------------------------------------ the instant path


@pytest.mark.invariant
async def test_the_instant_brief_lands_before_the_intake_is_read(
    container: Container, session: IncidentSession
) -> None:
    """Stage one is version 1, model-free, and persisted before anything reads a call.

    The whole reason the intake is allowed to exist is that it cannot get in
    front of this. If it could, the 500 ms budget would be a budget for a Vertex
    round trip, which is not a budget anybody can meet on a fireground.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    instant = await session.emit_instant(opened)

    assert instant.version == 1
    assert instant.stage is BriefStage.INSTANT
    assert instant.model_invoked is False
    assert instant.persisted_at is not None

    result = await _intercept(session, opened)
    # The intake produced a *later* version. Stage one is untouched.
    assert result.emission is not None
    assert result.emission.version > instant.version
    assert result.emission.stage is BriefStage.AMENDMENT
    assert session.emissions_after(opened.incident.incident_id, 0)[0] == instant


@pytest.mark.degraded
async def test_a_model_outage_costs_the_intake_and_never_the_brief(
    container: Container, session: IncidentSession
) -> None:
    """Vertex down is the ordinary case this design is built around.

    The brief still landed, the intake reports plainly that it was not read, and
    the incident log carries that fact rather than an absence somebody has to
    infer.
    """
    from firstdue.adapters.fake.model import FakeModelClient
    from firstdue.incident.intake import IntakeReader
    from firstdue.incident.interceptor import IncidentInterceptor

    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    instant = await session.emit_instant(opened)

    session.interceptor = IncidentInterceptor(
        intake=IntakeReader(model=FakeModelClient(unavailable=True), screen=container.screen),
        registry=container.registry,
        waker=None,
    )
    result = await _intercept(session, opened)

    assert result.accepted is False
    assert result.emission is None
    assert session.latest(opened.incident.incident_id) == instant

    log = await container.incident_log.get_log(opened.incident.incident_id)
    unread = [e for e in log.entries if e.entry_type is LogEntryType.INTAKE_READ]
    assert len(unread) == 1
    assert unread[0].content["accepted"] is False


# --------------------------------------------------- reported vs observed


@pytest.mark.invariant
async def test_a_911_call_never_writes_a_structural_fact(
    container: Container, session: IncidentSession
) -> None:
    """Section 6, at the only place a model reads a citizen's words about a building.

    A caller report has no merge tier because it is never a fact. A low tier
    would not be enough: the merge prefers a known value to an absent one, so on
    a building with nothing on file a caller's guess would become the answer of
    record for that attribute.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)

    before = await container.facts.list_for_address(opened.incident.address_id)
    result = await _intercept(session, opened)
    after = await container.facts.list_for_address(opened.incident.address_id)

    assert result.reading.items, "the fixture call must actually report something"
    assert [f.fact_id for f in after] == [f.fact_id for f in before]


@pytest.mark.invariant
async def test_the_filed_line_and_the_reported_line_stand_side_by_side(
    container: Container, session: IncidentSession
) -> None:
    """One brief, two kinds of knowing, told apart without reading the prose.

    The filed storey count keeps its fact id and its status; the caller's floor
    of origin carries a reported note, no fact id, and never CONFIRMED. An
    officer scanning the brief can see which is which from the line itself.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)
    assert result.emission is not None

    items = [i for s in result.emission.sections for i in s.items]
    filed = [i for i in items if i.canonical_key == Keys.STORIES and i.fact_id]
    reported = [i for i in items if i.reported_note]

    assert filed, "the warmed profile files a storey count"
    assert reported, "the call reported something"
    for line in reported:
        assert line.status is not AssertionStatus.CONFIRMED
        assert line.fact_id is None
        assert line.value_render.startswith("REPORTED")
    # The filed line is exactly as the instant stage rendered it.
    assert all(i.fact_id for i in filed)


@pytest.mark.authorization
async def test_a_caller_reported_alarm_level_does_not_widen_the_incident_grant(
    container: Container, session: IncidentSession
) -> None:
    """The alarm level is an authority boundary, so a caller cannot move it.

    ``alarm_level`` is minted onto the incident grant and checked by the gateway
    on mutual-aid reads. A caller saying "second alarm" that raised it would be a
    member of the public widening what the fleet may read about a building.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session, alarm=1)
    await session.emit_instant(opened)

    result = await _intercept(session, opened)
    assert result.signals.reported_alarm_level == 2

    incident = await container.incidents.get(opened.incident.incident_id)
    assert incident is not None
    assert incident.alarm_level == 1
    grant = await container.grants.get_incident_grant(opened.incident.grant_id)
    assert grant is not None
    assert grant.alarm_level == 1


@pytest.mark.invariant
async def test_a_cold_profile_says_so_even_once_a_caller_has_described_it(
    container: Container, session: IncidentSession
) -> None:
    """A caller's description must not paper over an empty profile.

    Before the intake existed, a cold brief was a column of UNKNOWNs and that
    was honest enough. Now the same brief can carry six things a caller said,
    which reads like knowledge unless the brief states plainly that nothing was
    on file.
    """
    opened = await _open(session, COLD_ADDRESS)
    assert opened.cold_start is True
    instant = await session.emit_instant(opened)
    assert COLD_START_NOTE in {i.value_render for s in instant.sections for i in s.items}

    result = await _intercept(session, opened)
    assert result.emission is not None
    renders = {i.value_render for s in result.emission.sections for i in s.items}
    assert COLD_START_NOTE in renders


# ------------------------------------------------------------- the routing


@pytest.mark.invariant
async def test_the_intake_routes_the_incident_by_declared_capability(
    container: Container, session: IncidentSession
) -> None:
    """Who was woken, why, and what they were handed -- all from the catalog.

    The notifier is woken because it declares notify:agency and the caller
    mentioned entrapment; sensor fusion because it declares read:geometry and
    the caller named a floor. Neither is named anywhere in the rule table.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)

    assert "agency-notifier" in result.woken_agent_ids
    assert "sensor-fusion" in result.woken_agent_ids

    fusion = session.handoff_for(opened.incident.incident_id, "sensor-fusion")
    assert fusion is not None
    assert fusion.intake_keys == (IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,)
    assert fusion.rule_ids == ("reported-floor-of-origin-reaches-the-thermal-scan",)


async def test_every_woken_agent_ran_under_the_incident_grant_and_is_on_the_record(
    container: Container, session: IncidentSession
) -> None:
    """A routed agent runs the only way any agent runs: through the fleet.

    Which means a grant check, a declared deadline, and a durable run record
    naming the pinned version -- the thing a NIOSH investigation reads two years
    later to find out who was told what.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)

    invoked = [ref.split("@")[0] for ref, _ in container.runtime.invocations]  # type: ignore[attr-defined]
    for agent_id in result.woken_agent_ids:
        assert agent_id in invoked


@pytest.mark.authorization
async def test_an_agent_this_grant_cannot_cover_is_withheld_rather_than_denied(
    container: Container, session: IncidentSession
) -> None:
    """A wake that would certainly be denied is not routing, it is noise.

    Narrowing the grant is the obvious least-privilege hardening, and it is
    exactly what turns a working fleet into one that denies on every incident.
    The plan withholds the agent and names the missing scope instead -- stated
    once per incident in the log, rather than buried in a denial an operator
    has to distinguish from a real one.

    The scope set is narrowed *here* rather than relying on a descriptor that
    over-declares. This test was originally written against
    ``incident-recorder``'s spurious ``read:audit``; that was a defect, it has
    been fixed, and a test whose premise is a bug stops testing anything the
    moment somebody fixes it.
    """
    from firstdue.domain.enums import Scope
    from firstdue.incident.handoff import plan_handoffs
    from firstdue.registry.descriptors import ACTIVE_FLEET

    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)

    # Everything the fleet needs, minus the one scope the recorder writes with.
    narrowed = frozenset(s for s in INCIDENT_SCOPES if s is not Scope.WRITE_RMS)
    plan = plan_handoffs(
        result.reading,
        descriptors=ACTIVE_FLEET,
        now=container.clock.now(),
        self_agent_id="incident-interceptor",
        authorised_scopes=narrowed,
    )

    withheld = {w.agent_id: w.missing_scopes for w in plan.withheld}
    assert withheld == {"incident-recorder": ("write:rms",)}
    assert "incident-recorder" not in {h.agent_id for h in plan.handoffs}


async def test_the_intake_and_every_handoff_are_in_the_incident_log(
    container: Container, session: IncidentSession
) -> None:
    """ "Who was told" is the first question after an incident where nobody was.

    The log carries what the narrative was read as -- attribute names, never the
    transcript -- and one entry per agent naming the rule that selected it.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)

    log = await container.incident_log.get_log(opened.incident.incident_id)
    reads = [e for e in log.entries if e.entry_type is LogEntryType.INTAKE_READ]
    handoffs = [e for e in log.entries if e.entry_type is LogEntryType.AGENT_HANDOFF]

    assert len(reads) == 1
    assert reads[0].content["accepted"] is True
    assert set(reads[0].content["reported_keys"]) == set(result.reading.reported_keys)
    assert len(handoffs) == len(result.plan.handoffs) + len(result.plan.withheld)
    assert all(entry.content["rule_ids"] for entry in handoffs)

    # And the transcript itself is not in the department's record.
    assert "delivery truck" not in str(reads[0].content)


async def test_the_draft_report_counts_the_narratives_and_the_handoffs(
    container: Container, session: IncidentSession
) -> None:
    """A NERIS draft should reflect that a call was read and who it reached."""
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    await _intercept(session, opened)

    closed = await session.controller.close(opened.incident.incident_id, closed_by="bc-09")
    assert closed.neris_draft is not None
    assert closed.neris_draft.intake_reads == 1
    assert closed.neris_draft.agent_handoffs >= 1


# ------------------------------------------------- the interceptor's own name


def _analyses(entries, agent_id: str) -> list:
    """Every analysis one agent filed under its own id."""
    return [
        e
        for e in entries
        if e.entry_type is LogEntryType.AGENT_ANALYSIS
        and str((e.content or {}).get("agent_ref", "")).startswith(agent_id)
    ]


async def test_reading_the_narrative_is_recorded_under_the_agent_that_read_it(
    container: Container, session: IncidentSession
) -> None:
    """Screening a transcript and binding six fields off it is the largest thing
    this agent does, and it was filed under whoever wrote the entry.

    ``INTAKE_READ`` names the recorder, because the recorder wrote it. So the
    console's per-agent stream had the interceptor doing nothing between opening
    the incident and the handoffs appearing -- through the model call, the
    screen, and the span binding that is the entire reason a caller's words are
    allowed anywhere near the brief.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)

    entries = (await container.incident_log.get_log(opened.incident.incident_id)).entries
    read = [e for e in _analyses(entries, "incident-interceptor") if "read the" in str(e.content)]
    assert read, "the intake was read and the agent that read it left no entry"
    content = read[0].content or {}
    assert str(content["headline"]).startswith("read the")
    # Counts and screen names. Never the call.
    assert result.reading.screen and result.reading.screen in str(content["detail"])
    assert "delivery truck" not in str(content)
    assert "propane" not in str(content)
    assert set(result.reading.reported_keys) <= set(content["refs"])


async def test_a_rule_that_wakes_nobody_says_so_in_the_incidents_own_record(
    container: Container, session: IncidentSession
) -> None:
    """A stated gap that no incident could see.

    The plan has always carried ``unmatched_rule_ids`` -- a rule that fired and
    matched no catalogued agent -- and nothing wrote it down. A department
    finding out that "reported hazardous material" is checked against the Tier
    II filings by nobody should find it out from the record of the incident
    where it happened, not from reading the rule table afterwards.
    """
    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    result = await _intercept(session, opened)
    assert result.plan.unmatched_rule_ids, "this call is supposed to fire the Tier II rule"

    entries = (await container.incident_log.get_log(opened.incident.incident_id)).entries
    analyses = _analyses(entries, "incident-interceptor")

    routed = [
        e for e in analyses if "routed the incident" in str((e.content or {}).get("headline"))
    ]
    assert len(routed) == 1
    assert str(len(result.plan.fired_rule_ids)) in str(routed[0].content["detail"])
    assert set(result.plan.fired_rule_ids) <= set(routed[0].content["refs"])

    for rule_id in result.plan.unmatched_rule_ids:
        declined = [e for e in analyses if rule_id in str((e.content or {}).get("headline"))]
        assert declined, f"{rule_id} fired, matched nobody, and left no trace"
        # The rule's own sentence, so the record and the table cannot drift.
        assert "read:tier-ii-metadata" in str(declined[0].content["detail"])


async def test_a_briefing_that_could_not_be_written_says_so(
    container: Container, session: IncidentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The focus is optional and its absence is not.

    A deployment that wired a bank and a model has asked its head agent to
    reason about attention on every incident. Losing that is survivable by
    construction -- every other agent falls back to its own rules -- but an
    incident where the thing that points at everything did not run reads on the
    console exactly like an incident where nothing needed pointing at.
    """

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the planner is unreachable")

    monkeypatch.setattr(
        type(session.interceptor), "composes_focus", property(lambda _: True), raising=False
    )
    monkeypatch.setattr(session.interceptor, "compose_focus", _boom, raising=False)

    await run_slow_loop(container, approve=False)
    opened = await _open(session)
    await session.emit_instant(opened)
    # The incident is unaffected: the intake still reads and still routes.
    result = await _intercept(session, opened)
    assert result.woken_agent_ids

    entries = (await container.incident_log.get_log(opened.incident.incident_id)).entries
    assert not [e for e in entries if e.entry_type is LogEntryType.FOCUS_COMPOSED]
    carried = [
        e
        for e in _analyses(entries, "incident-interceptor")
        if "without a focus" in str((e.content or {}).get("headline"))
    ]
    assert carried, "the head agent tried to brief the fleet, failed, and left no trace"
    assert "RuntimeError" in str(carried[0].content["detail"])
