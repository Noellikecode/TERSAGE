"""The entry package end to end: assessed, solved, synthesised, signed, sent.

Two halves to this file. The first drives the session directly and asserts what
the incident log and the audit log hold afterwards -- which is the complaint
this work answers: the loop looked idle because the agents doing the work left
no trace under their own names. The second drives the HTTP surface a console
will actually build against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.enums import LogEntryType
from firstdue.domain.work import ApprovalStatus
from firstdue.errors import NotFoundError, ValidationError
from firstdue.incident.intake import IntakeChannel
from firstdue.incident.packages import BRIEF_HALF, PATH_HALF, PackageStatus, package_pdf
from firstdue.incident.session import IncidentSession
from firstdue.ports.audit import AuditEventKind
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"

NARRATIVE = "Heavy smoke on the top floor and the side gate is chained shut."


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """The button, with the loop's own hand held away from it.

    ``entry_package_autonomy`` is on by default and this file is not about it:
    ``_incident`` below flies all four faces, which is exactly the point at
    which the loop composes a package for itself, so every count and every id
    list here would be a count of two -- one the fleet staged and one the test
    asked for. Autonomy has its own suite; this one asserts what happens when a
    human asks.
    """
    return Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=True,
        fixtures_dir=REPO_ROOT / "fixtures",
        demo_state_dir=tmp_path / ".demo-state",
        log_json=False,
        entry_package_autonomy=False,
    )


@pytest.fixture
def container(settings: Settings) -> Container:
    return build_container(settings)


@pytest.fixture
def session(container: Container) -> IncidentSession:
    return IncidentSession(container)


async def _incident(container: Container, session: IncidentSession) -> str:
    """A warm district, an open incident, a narrative read, and a flown building."""
    await run_slow_loop(container, approve=False)
    opened = await session.controller.open(
        address=DISPUTED_ADDRESS_ID, cad_ref="CAD-0001", alarm_level=2
    )
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_intake(
        incident_id,
        narrative=NARRATIVE,
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-0001",
        correlation_id="corr-intake",
    )
    for index in range(4):
        await session.run_drone_sweep_step(incident_id, correlation_id=f"corr-sweep-{index}")
    return incident_id


def _analyses(entries: Any, agent: str) -> list[dict[str, Any]]:
    return [
        entry.content
        for entry in entries
        if entry.entry_type is LogEntryType.AGENT_ANALYSIS
        and str(entry.content.get("agent_ref", "")).startswith(agent)
    ]


# ------------------------------------------------------------ the whole flow


@pytest.mark.invariant
async def test_composing_a_package_assesses_solves_synthesises_and_stages_two_cards(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )

    # One document, three artifacts, and both halves waiting on a human.
    assert package.incident_id == incident_id
    assert package.assessment.incident_id == incident_id
    assert package.brief.claims
    assert package.status is PackageStatus.AWAITING_APPROVAL
    assert package.outstanding_halves == (PATH_HALF, BRIEF_HALF)
    assert package.sent_at is None

    # Both approval cards are in the same repository the resource agent stages
    # a shutoff on, so one console list shows every human decision outstanding.
    staged = await container.approvals.list_for_incident(incident_id)
    by_id = {approval.approval_id: approval for approval in staged}
    assert package.path_approval_id in by_id
    assert package.brief_approval_id in by_id
    for approval_id in (package.path_approval_id, package.brief_approval_id):
        assert by_id[approval_id].status is ApprovalStatus.STAGED
        assert by_id[approval_id].prefilled_summary
        # The gateway rule that governs the write is named on the card, rather
        # than a rule id this module invented.
        assert by_id[approval_id].rule_id


@pytest.mark.invariant
async def test_the_route_is_solved_over_the_building_the_slow_loop_measured(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    path = package.path
    assert not path.refused
    assert path.entry is not None
    assert path.algorithm == "A*"

    # Waypoints a map can draw: footprint metres, a storey, and coordinates,
    # because the city adapter can place this address.
    assert path.entry.waypoints[0].node_id == "staging"
    assert path.entry.waypoints[-1].node_id == "core:L0"
    for waypoint in path.entry.waypoints:
        assert waypoint.longitude is not None
        assert waypoint.latitude is not None

    # And every leg says what priced it. A leg with no terms is distance alone,
    # which is itself the reason it was taken.
    for leg in path.entry.legs:
        assert leg.chose_because
        assert leg.cost >= leg.distance_m
    priced = [leg for leg in path.entry.legs if leg.terms]
    assert priced, "the entry leg through a measured wall should carry its terms"
    assert any(term.refs for leg in priced for term in leg.terms)


@pytest.mark.invariant
async def test_both_halves_are_signed_before_anything_is_sent(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )

    with pytest.raises(ValidationError):
        await session.dispatch_package(incident_id, package.package_id, sent_by="bc-9")

    after_path = await session.approve_package_half(
        incident_id, package.package_id, half=PATH_HALF, decided_by="chief-ruiz"
    )
    assert after_path.path_approved
    assert not after_path.brief_approved
    assert after_path.status is PackageStatus.AWAITING_APPROVAL

    # Still refused with one signature. Two halves means two.
    with pytest.raises(ValidationError):
        await session.dispatch_package(incident_id, package.package_id, sent_by="bc-9")

    after_brief = await session.approve_package_half(
        incident_id, package.package_id, half=BRIEF_HALF, decided_by="capt-alvarez"
    )
    assert after_brief.status is PackageStatus.READY_TO_SEND

    sent = await session.dispatch_package(incident_id, package.package_id, sent_by="bc-9")
    assert sent.status is PackageStatus.SENT
    assert sent.sent_by == "bc-9"
    assert sent.dispatch_decision_id
    assert sent.path_approved_by == "chief-ruiz"
    assert sent.brief_approved_by == "capt-alvarez"

    # Sending twice does not send twice.
    again = await session.dispatch_package(incident_id, package.package_id, sent_by="somebody-else")
    assert again.sent_by == "bc-9"
    assert again.sent_at == sent.sent_at


async def test_an_unknown_half_is_refused(container: Container, session: IncidentSession) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    with pytest.raises(ValidationError):
        await session.approve_package_half(
            incident_id, package.package_id, half="whatever", decided_by="bc-9"
        )


async def test_an_unknown_package_is_a_not_found(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    with pytest.raises(NotFoundError):
        await session.get_entry_package(incident_id, "pkg-nobody-composed")


# ---------------------------------------------------------------- the record


@pytest.mark.invariant
async def test_every_step_of_the_flow_leaves_a_trace_under_the_agent_that_did_it(
    container: Container, session: IncidentSession
) -> None:
    """The complaint this answers: an agent that works and leaves no trace is
    indistinguishable from one that did not run."""
    incident_id = await _incident(container, session)
    before = len(
        _analyses(
            (await container.incident_log.get_log(incident_id)).entries, "incident-interceptor"
        )
    )

    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    await session.approve_package_half(
        incident_id, package.package_id, half=PATH_HALF, decided_by="chief-ruiz"
    )
    await session.approve_package_half(
        incident_id, package.package_id, half=BRIEF_HALF, decided_by="capt-alvarez"
    )
    await session.dispatch_package(incident_id, package.package_id, sent_by="bc-9")

    entries = (await container.incident_log.get_log(incident_id)).entries
    headlines = [row["headline"] for row in _analyses(entries, "incident-interceptor")[before:]]

    # One line per criterion, one verdict, one solve, one synthesis, one
    # staging, one per approval, one send. Nothing padded: each is a distinct
    # step against distinct data.
    assert sum(1 for line in headlines if line.startswith("readiness ")) == 6
    assert any(line.startswith(("READY", "NOT READY")) for line in headlines)
    assert any("entry path" in line for line in headlines)
    assert any("synthesised the crew brief" in line for line in headlines)
    assert any("staged entry package" in line for line in headlines)
    assert sum(1 for line in headlines if "approved by" in line) == 2
    assert any("sent entry package" in line for line in headlines)

    # The package itself is in the log, once per state it passed through.
    packaged = [e for e in entries if e.entry_type is LogEntryType.ENTRY_PACKAGE]
    assert len(packaged) == 4
    assert [entry.content["status"] for entry in packaged] == [
        "AWAITING_APPROVAL",
        "AWAITING_APPROVAL",
        "READY_TO_SEND",
        "SENT",
    ]
    # Append-only: the earlier states are still there, unedited.
    assert packaged[0].content["package"]["sent_at"] is None

    # Both approvals and the two gateway decisions are recorded as themselves.
    assert sum(1 for e in entries if e.entry_type is LogEntryType.APPROVAL_GRANTED) == 2
    assert sum(1 for e in entries if e.entry_type is LogEntryType.POLICY_DECISION) >= 2


@pytest.mark.invariant
async def test_the_audit_log_credits_the_interceptor_and_copies_no_document_text(
    container: Container, session: IncidentSession
) -> None:
    """The console's evidence is the audit log, and it carries ids only."""
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    await session.approve_package_half(
        incident_id, package.package_id, half=PATH_HALF, decided_by="chief-ruiz"
    )

    events = await container.audit.list_events(limit=1000)
    steps = [
        event
        for event in events
        if event.kind is AuditEventKind.AGENT_STEP and event.target == incident_id
    ]
    assert any(event.actor == "incident-interceptor" for event in steps)
    assert any(event.detail.get("entry") == "ENTRY_PACKAGE" for event in steps)
    for event in steps:
        for value in event.detail.values():
            assert " " not in value, (event.actor, value)


async def test_a_package_read_back_out_of_the_log_is_the_package_that_went_in(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    reread = await session.get_entry_package(incident_id, package.package_id)
    assert reread == package

    listed = await session.list_entry_packages(incident_id)
    assert [entry.package_id for entry in listed] == [package.package_id]

    # A second package is listed beside the first, in composition order.
    second = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg-2"
    )
    listed = await session.list_entry_packages(incident_id)
    assert [entry.package_id for entry in listed] == [package.package_id, second.package_id]


async def test_the_pdf_carries_the_verdict_the_prose_and_every_citation(
    container: Container, session: IncidentSession
) -> None:
    incident_id = await _incident(container, session)
    package = await session.run_entry_package(
        incident_id, target_level=0, correlation_id="corr-pkg"
    )
    pdf = package_pdf(package)
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    # An unapproved sheet says so on its own face rather than looking signed.
    assert b"not approved" in pdf
    assert b"Not sent." in pdf
    assert b"Decision support." in pdf


# ------------------------------------------------------------------- the API


@pytest.fixture
def incident(app_client: TestClient) -> dict[str, Any]:
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    response = app_client.post(
        f"{PREFIX}/incidents",
        json={
            "address": DISPUTED_ADDRESS_ID,
            "cad_ref": "CAD-0001",
            "alarm_level": 2,
            "intake_narrative": NARRATIVE,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    for _ in range(4):
        app_client.post(f"{PREFIX}/incidents/{body['incident_id']}/drone-sweep")
    return body


def test_the_api_walks_a_package_from_composition_to_a_downloadable_pdf(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]

    readiness = app_client.post(f"{PREFIX}/incidents/{incident_id}/readiness")
    assert readiness.status_code == 200
    verdict = readiness.json()
    assert len(verdict["criteria"]) == 6
    assert isinstance(verdict["ready"], bool)
    assert verdict["summary"]

    solved = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/entry-path", json={"target_level": 0}
    )
    assert solved.status_code == 200
    plan = solved.json()
    assert plan["refused"] is False
    assert plan["entry"]["waypoints"][0]["node_id"] == "staging"
    assert plan["entry_face"]

    composed = app_client.post(f"{PREFIX}/incidents/{incident_id}/entry-packages", json={})
    assert composed.status_code == 201
    package = composed.json()
    package_id = package["package_id"]
    assert package["status"] == "AWAITING_APPROVAL"
    assert package["outstanding_halves"] == ["entry-path", "crew-brief"]
    assert package["path_approved"] is False
    assert package["brief_approved"] is False

    listed = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages").json()
    assert [row["package_id"] for row in listed["packages"]] == [package_id]
    assert listed["packages"][0]["status"] == "AWAITING_APPROVAL"

    # Dispatch before either signature is a 422 with a stated reason.
    early = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/entry-packages/{package_id}/dispatch"
    )
    assert early.status_code == 422
    assert early.json()["error"]["code"] == "VALIDATION_ERROR"

    for half in ("entry-path", "crew-brief"):
        signed = app_client.post(
            f"{PREFIX}/incidents/{incident_id}/entry-packages/{package_id}/approvals/{half}"
        )
        assert signed.status_code == 200

    sent = app_client.post(f"{PREFIX}/incidents/{incident_id}/entry-packages/{package_id}/dispatch")
    assert sent.status_code == 200
    assert sent.json()["status"] == "SENT"
    assert sent.json()["sent_by"]

    fetched = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/{package_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "SENT"

    pdf = app_client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/{package_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert f"crew-brief-{package_id}.pdf" in pdf.headers["content-disposition"]
    assert pdf.content.startswith(b"%PDF-1.4")


def test_the_api_reports_a_refusal_rather_than_erroring_on_a_cold_address(
    app_client: TestClient,
) -> None:
    """No pre-incident geometry is a 200 with ``refused`` set and a reason."""
    opened = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": "sf-3120-24th", "cad_ref": "CAD-0002", "alarm_level": 1},
    )
    assert opened.status_code == 201
    incident_id = opened.json()["incident_id"]

    solved = app_client.post(f"{PREFIX}/incidents/{incident_id}/entry-path", json={})
    assert solved.status_code == 200
    plan = solved.json()
    assert plan["refused"] is True
    assert "no pre-incident geometry" in plan["refusal_reason"]
    assert plan["entry"] is None

    # And a package composed anyway carries the refusal rather than a route.
    composed = app_client.post(f"{PREFIX}/incidents/{incident_id}/entry-packages", json={})
    assert composed.status_code == 201
    body = composed.json()
    assert body["path"]["refused"] is True
    assert body["assessment"]["ready"] is False
    assert "geometry.present" in body["assessment"]["failed_ids"]


def test_an_unknown_package_is_a_404(app_client: TestClient, incident: dict[str, Any]) -> None:
    response = app_client.get(
        f"{PREFIX}/incidents/{incident['incident_id']}/entry-packages/pkg-nope"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------- the floor the caller reported


@pytest.mark.invariant
async def test_the_reported_floor_of_origin_decides_which_storey_the_path_climbs_to(
    container: Container, session: IncidentSession
) -> None:
    """The caller says which floor is burning. The route has to go there.

    Everything for this already existed and none of it was joined up. The
    interceptor reads ``intake.reported_floor_of_origin`` off the call and binds
    it to the span that supports it; the graph carries a vertical core with an
    interior node on every storey the massing model measured; the solver takes a
    target level and the waypoints carry the height to draw it at. But the
    target defaulted to the ground and nothing ever passed anything else, so a
    caller reporting smoke on the third floor got a crew routed to the lobby --
    on a five-storey building, using one storey of a graph that had all five.

    Counting is the fire service's: the ground storey is the first floor, so the
    third floor is two levels above it.
    """
    await run_slow_loop(container, approve=False)
    opened = await session.controller.open(
        address=DISPUTED_ADDRESS_ID, cad_ref="CAD-FLOOR", alarm_level=2
    )
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_intake(
        incident_id,
        narrative="The third floor is full of smoke. There are people still inside.",
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-FLOOR",
        correlation_id="corr-floor",
    )

    plan = await session.solve_entry_path(incident_id)

    assert plan.target_level == 2
    reached = [waypoint.level for waypoint in plan.entry.waypoints if waypoint.level is not None]
    assert max(reached) == 2
    # And it climbed rather than teleporting: every storey between the door and
    # the fire floor is on the route, because that is the walk crews make.
    assert sorted(set(reached)) == [0, 1, 2]


@pytest.mark.invariant
async def test_an_explicit_target_level_still_wins_over_the_reported_floor(
    container: Container, session: IncidentSession
) -> None:
    """A commander asking for a storey outranks what the call said.

    The reported floor is a default, not an override. An IC who asks for the
    ground storey gets the ground storey even on a call that reported the third,
    because the caller is reporting and the IC is deciding.
    """
    await run_slow_loop(container, approve=False)
    opened = await session.controller.open(
        address=DISPUTED_ADDRESS_ID, cad_ref="CAD-FLOOR-2", alarm_level=2
    )
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_intake(
        incident_id,
        narrative="The third floor is full of smoke. There are people still inside.",
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-FLOOR-2",
        correlation_id="corr-floor-2",
    )

    plan = await session.solve_entry_path(incident_id, target_level=0)

    assert plan.target_level == 0


@pytest.mark.invariant
async def test_a_call_that_reports_no_floor_leaves_the_path_on_the_ground(
    container: Container, session: IncidentSession
) -> None:
    """No reported floor is not a reported ground floor, but it routes the same.

    The difference is that nothing was inferred to get there: the solver was
    given no storey and used its documented default, rather than reading a
    number out of a call that never contained one.
    """
    await run_slow_loop(container, approve=False)
    opened = await session.controller.open(
        address=DISPUTED_ADDRESS_ID, cad_ref="CAD-FLOOR-3", alarm_level=2
    )
    incident_id = opened.incident.incident_id
    await session.emit_instant(opened)
    await session.run_intake(
        incident_id,
        narrative="Smoke showing from the rear. The side gate is chained shut.",
        channel=IntakeChannel.CALL_911,
        source_ref="intake/CAD-FLOOR-3",
        correlation_id="corr-floor-3",
    )

    plan = await session.solve_entry_path(incident_id)

    assert plan.target_level == 0
