"""The eight agent descriptors.

A descriptor is a contract the gateway and the console read, so these tests are
about the contract being complete and consistent -- not about the prose.
"""

from __future__ import annotations

import pytest

from firstdue.container import WRITE_TARGET_IDS
from firstdue.domain.enums import Capability, Loop
from firstdue.domain.identity import WRITE_SCOPES
from firstdue.errors import NotFoundError
from firstdue.registry.descriptors import (
    FLEET,
    FLEET_VERSION,
    descriptor_for,
    fleet_descriptors,
)

EXPECTED_AGENTS = {
    "records-watcher",
    "hazard-watcher",
    "geometry-watcher",
    "conflict-detector",
    "survey-ranker",
    "referral-clerk",
    "incident-controller",
    "agency-notifier",
    "incident-recorder",
}


def test_the_fleet_is_the_declared_agents() -> None:
    assert {d.agent_id for d in FLEET} == EXPECTED_AGENTS
    assert len(FLEET) == 9


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
