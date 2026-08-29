"""The agent descriptors: nine scheduled, four superseded but still catalogued.

A descriptor is a contract the gateway and the console read, so these tests are
about the contract being complete and consistent -- not about the prose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.container import WRITE_TARGET_IDS
from firstdue.domain.enums import Capability, Loop
from firstdue.domain.identity import WRITE_SCOPES
from firstdue.errors import NotFoundError
from firstdue.registry.descriptors import (
    ACTIVE_FLEET,
    FLEET,
    FLEET_VERSION,
    descriptor_for,
    fleet_descriptors,
)

#: The agents that are scheduled and given a worker.
EXPECTED_ACTIVE = {
    "records-watcher",
    "hazard-watcher",
    "geometry-watcher",
    "structure-watch",
    "referral-clerk",
    "incident-interceptor",
    "sensor-fusion",
    "agency-notifier",
    "incident-recorder",
}

#: Superseded, still catalogued. Four agents merged into two.
EXPECTED_DEPRECATED = {
    "conflict-detector",
    "survey-ranker",
    "incident-controller",
    "brief-reconciler",
}


def test_the_fleet_is_the_declared_agents() -> None:
    assert {d.agent_id for d in ACTIVE_FLEET} == EXPECTED_ACTIVE
    assert len(ACTIVE_FLEET) == 9
    assert {d.agent_id for d in FLEET} == EXPECTED_ACTIVE | EXPECTED_DEPRECATED


def test_a_superseded_agent_stays_resolvable_for_replay() -> None:
    """Version pinning exists for NIOSH, so a retired id must still resolve.

    Every brief records the agent versions that produced it. An ``agent_id``
    deleted from the catalog turns a two-year-old recorded run into a reference
    to something this build has never heard of -- the replay could not say what
    produced the brief a commander acted on.
    """
    for agent_id in EXPECTED_DEPRECATED:
        descriptor = descriptor_for(agent_id)
        assert descriptor.deprecated_at is not None
        assert descriptor.is_deprecated(datetime(2026, 8, 22, tzinfo=UTC))


def test_nothing_deprecated_is_routed_or_given_a_worker() -> None:
    """Catalogued is not scheduled. A retired agent gets no subscription.

    A push subscription pointed at an agent nobody runs dead-letters forever
    while every dashboard looks healthy -- phase 2 flagged exactly this.
    """
    from firstdue.registry.routing import CONSUMES

    for agent_id in EXPECTED_DEPRECATED:
        assert agent_id not in CONSUMES


def test_tier_two_is_read_by_the_county_agent_the_department_pins() -> None:
    """The subscription is the authorization boundary for confidential filings."""
    from firstdue.domain.enums import Classification, Department

    hazard = descriptor_for("hazard-watcher")
    assert hazard.publisher_department is Department.COUNTY_OEM
    assert Classification.TIER_II_CONFIDENTIAL in hazard.classifications_accessed
    # The fire department's own records watcher cannot reach them.
    records = descriptor_for("records-watcher")
    assert Classification.TIER_II_CONFIDENTIAL not in records.classifications_accessed


def test_every_descriptor_declares_the_full_contract() -> None:
    for descriptor in FLEET:
        assert descriptor.version == FLEET_VERSION
        assert descriptor.publisher_department
        assert descriptor.capabilities
        assert descriptor.required_scopes
        assert descriptor.classifications_accessed
        assert descriptor.latency_target_ms > 0
        assert descriptor.role_summary
        assert descriptor.input_schema_ref.startswith("firstdue.schemas.")
        assert descriptor.output_schema_ref.startswith("firstdue.schemas.")


@pytest.mark.invariant
def test_a_writer_names_its_targets_and_carries_a_write_scope() -> None:
    """Enforced by the model, asserted here so the fleet cannot drift past it."""
    for descriptor in FLEET:
        writes = Capability.WRITE in descriptor.capabilities
        assert writes == bool(descriptor.write_targets)
        if writes:
            assert descriptor.required_scopes & WRITE_SCOPES


def test_every_external_write_target_has_an_owning_agent() -> None:
    """A system nothing writes to is a system nobody is accountable for."""
    declared = {target for d in FLEET for target in d.write_targets}
    configured = {target_id for target_id, _department, _prefix in WRITE_TARGET_IDS}
    assert declared == configured


def test_both_loops_are_represented() -> None:
    loops = {d.loop for d in FLEET}
    assert loops == {Loop.SLOW, Loop.INCIDENT}


def test_the_incident_controller_is_budgeted_for_the_instant_brief() -> None:
    """The instant brief contains no model call, so 500 ms is a read and a render."""
    controller = descriptor_for("incident-controller")
    assert controller.loop is Loop.INCIDENT
    assert controller.latency_target_ms == 500
    # It reads. It does not write anywhere outside the department.
    assert Capability.WRITE not in controller.capabilities
    assert controller.write_targets == ()


def test_resource_committing_agents_require_a_human() -> None:
    from firstdue.domain.enums import ApprovalThreshold

    for agent_id in ("survey-ranker", "referral-clerk", "agency-notifier"):
        assert descriptor_for(agent_id).approval_threshold is not ApprovalThreshold.NONE


# ------------------------------------- the descriptor agrees with the gateway
#
# A descriptor's ``approval_threshold`` is *published metadata*: it is what the
# catalog shows a subscribing department, and what the console renders next to
# an agent. The gateway's ``APPROVAL_THRESHOLDS`` is the *enforcement*: it is
# what actually stages a write for a human.
#
# Nothing connected the two. They agree today, and the way that stops being
# true is quiet: someone adds a write scope to an agent and does not touch the
# descriptor, so the catalog advertises an autonomous agent that the gateway
# gates -- or, far worse, advertises a gated one the gateway waves through. The
# first is confusing. The second is a department believing a human signs
# something that no human ever sees.


#: NONE < SUPERVISOR < CHIEF. Declared here rather than on the enum because the
#: enum is a set of names, and only this comparison needs them ordered.
_STRICTNESS = {"NONE": 0, "SUPERVISOR": 1, "CHIEF": 2}


def _enforced_threshold(descriptor):
    """The strictest threshold the gateway would apply to this agent's scopes.

    Strictest rather than first: an agent holding both a supervisor scope and a
    chief scope is a chief-approval agent, because the catalog has one field
    and it must not under-state what the gateway will demand.
    """
    from firstdue.domain.enums import ApprovalThreshold
    from firstdue.gateway.engine import APPROVAL_THRESHOLDS

    applicable = [
        APPROVAL_THRESHOLDS[scope]
        for scope in descriptor.required_scopes
        if scope in APPROVAL_THRESHOLDS
    ]
    if not applicable:
        return ApprovalThreshold.NONE
    return max(applicable, key=lambda t: _STRICTNESS[str(t)])


@pytest.mark.authorization
def test_every_descriptor_publishes_the_threshold_the_gateway_declares() -> None:
    """The two declarations must agree with each other.

    Scope note, deliberately narrow: this compares two *declarations* -- the
    catalog's published threshold and the gateway's approval table. It does not
    prove either is reached on any given path, and for the slow loop it is not.
    ``PolicyEngine.decide`` has exactly one caller, the incident resource
    request, so the incident thresholds are enforced by the gateway and the
    slow-loop ones are not: the referral gate lives in ``ActionFlow`` and the
    work order has no gate at all, by design and under test.

    Whether the catalog should therefore publish NONE for ``survey-ranker`` is
    an open question and not one a test should settle by fiat -- see
    docs/build-notes.md. What this test does settle is that the two
    declarations cannot drift apart without somebody noticing.
    """
    for descriptor in ACTIVE_FLEET:
        assert descriptor.approval_threshold is _enforced_threshold(descriptor), (
            f"{descriptor.agent_id} publishes {descriptor.approval_threshold} "
            f"but the gateway enforces {_enforced_threshold(descriptor)} "
            f"for scopes {sorted(str(s) for s in descriptor.required_scopes)}"
        )


@pytest.mark.authorization
def test_no_approval_rule_guards_a_scope_no_agent_holds() -> None:
    """A rule nothing can trigger is a rule nobody maintains.

    It reads as coverage in the policy table and protects nothing, and the day
    an agent does take the scope, the stale entry is what everyone trusts
    instead of reading it.
    """
    from firstdue.gateway.engine import APPROVAL_THRESHOLDS

    held = {scope for descriptor in FLEET for scope in descriptor.required_scopes}
    assert set(APPROVAL_THRESHOLDS) <= held, (
        "these approval rules guard scopes no published agent holds: "
        f"{sorted(str(s) for s in set(APPROVAL_THRESHOLDS) - held)}"
    )


def test_at_least_one_agent_is_published_by_another_department() -> None:
    """The registry exists because departments publish for each other."""
    from firstdue.registry.descriptors import HOME_DEPARTMENT

    publishers = {d.publisher_department for d in FLEET}
    assert publishers - {HOME_DEPARTMENT}


def test_descriptors_are_returned_in_deterministic_order() -> None:
    assert [d.ref for d in fleet_descriptors()] == sorted(d.ref for d in FLEET)


def test_an_unknown_agent_is_not_guessed_at() -> None:
    with pytest.raises(NotFoundError):
        descriptor_for("thermal-oracle")


def test_no_live_code_path_stamps_a_superseded_agent_id() -> None:
    """A retired id must not be written onto a record being produced today.

    Found for real during the merge: `services/surveys.py` still stamped
    ``survey-ranker`` as `produced_by_agent` on the fact a physical survey
    produces -- a **human-verified** value, the most authoritative thing this
    system holds, with its provenance pointing at an agent nothing runs any
    more. `services/materialization.py` had the same problem with
    ``conflict-detector`` on every conflict timeline event.

    Nothing failed, which is why it survived: a deprecated descriptor still
    resolves, so the id was valid and merely untrue. This test reads the source
    rather than the behaviour, because the behaviour is indistinguishable.
    """
    import re
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[2] / "backend" / "src" / "firstdue"
    # String literals only: prose in docstrings and comments explaining the
    # supersession is exactly what this file wants people to write.
    literal = re.compile(r"""["']([a-z-]+)["']""")
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if path.name == "descriptors.py":
            continue  # the catalog is where a retired id is supposed to appear
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith(('"""', "'''")):
                continue
            for match in literal.findall(line):
                if match in EXPECTED_DEPRECATED:
                    offenders.append(f"{path.name}:{number} -> {match}")
    assert not offenders, "superseded agent ids on live paths: " + "; ".join(offenders)


def test_no_agent_declares_a_budget_tighter_than_the_work_it_runs() -> None:
    """`latency_target_ms` is a hard cap, so under-declaring it times work out.

    `budget_seconds` takes `min(declared, remaining)`, which makes a
    descriptor's latency target a ceiling on every run of that agent rather
    than an aspiration. `incident-interceptor` shipped at 500 ms for one
    release -- the instant brief's budget -- while the two model-bearing stages
    it owns ask for 4 s and 6 s. Fake adapters answer in microseconds, so the
    entire suite passed and the failure would have arrived on the first live
    Vertex call, as a timeout on every incident with a narrative.

    Equality is not good enough either, which is the second half of this. A
    stage whose model deadline is exactly the run's cap loses the race to the
    runtime by microseconds when the model spends its whole budget -- so the
    *good* failure, a refusal this loop records and routes around, is replaced
    by a cancelled handler that records nothing and wakes nobody. Every stage
    has to leave room for the work that follows its model call.
    """
    from firstdue.incident.crewbrief import CREW_BRIEF_DEADLINE_MS
    from firstdue.incident.intake import INTAKE_DEADLINE_MS
    from firstdue.incident.reconciler import NARRATIVE_DEADLINE_MS

    interceptor = descriptor_for("incident-interceptor")
    slowest = max(INTAKE_DEADLINE_MS, NARRATIVE_DEADLINE_MS, CREW_BRIEF_DEADLINE_MS)
    assert interceptor.latency_target_ms > slowest, (
        f"incident-interceptor declares {interceptor.latency_target_ms} ms but runs a "
        f"{slowest} ms stage; budget_seconds would cap and time it out"
    )


def test_every_incident_agent_can_be_covered_by_an_incident_grant() -> None:
    """A declared scope the grant cannot carry is a run that always denies.

    The runtime checks `descriptor.required_scopes <= grant.scopes`, so an
    incident agent declaring anything outside `INCIDENT_SCOPES` is refused on
    every incident -- correctly, and indistinguishably in a log from a denial
    worth investigating.

    Both directions of this have now bitten: `agency-notifier` under-declared
    and worked by accident until someone narrowed the grant, and
    `incident-recorder` declared `read:audit`, which it never reads, and failed
    the moment routing sent work to it.
    """
    from firstdue.domain.enums import Loop
    from firstdue.services.grants import INCIDENT_SCOPES

    for descriptor in ACTIVE_FLEET:
        if descriptor.loop is not Loop.INCIDENT:
            continue
        uncovered = descriptor.required_scopes - INCIDENT_SCOPES
        assert not uncovered, (
            f"{descriptor.agent_id} declares {sorted(str(s) for s in uncovered)}, "
            "which an incident grant cannot carry"
        )
