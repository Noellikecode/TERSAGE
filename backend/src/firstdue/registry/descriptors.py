"""The eight agents, declared.

A descriptor is a contract, not documentation. Before an agent runs, the gateway
reads its ``required_scopes`` and refuses the run if the grant does not carry
them; the console reads ``classifications_accessed`` to show an officer what a
given agent can see; an investigator reads ``version`` to know which code
produced a fact two years ago. Everything here is load-bearing.

Two conventions are worth stating, because both are enforced by the model:

**``Capability.WRITE`` means writing outside the department's own store.** An
agent that only appends facts to a building profile is not a writer in this
sense: it declares the ``write:profile`` scope and no write targets. An agent
that files with the building department, cuts a work order, or writes to the
records system declares ``WRITE`` and names the system it writes into --
:class:`~firstdue.domain.registry.AgentDescriptor` rejects one without the other.

**Approval thresholds sit on the descriptor, not in the agent's code.** Telling
an agency something is autonomous. Committing another agency's resources, or
cutting a utility, requires a human tap -- and which agents need one is a
property of the catalog an officer can read, not a branch buried in a handler.

Latency targets differ by two orders of magnitude between the loops on purpose.
The slow loop is measured in minutes because it has months. ``incident-controller``
is budgeted at 500 ms because the instant brief has no model call in it and
nothing to wait for -- the work already happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from firstdue.domain.enums import (
    ApprovalThreshold,
    Capability,
    Classification,
    Department,
    Loop,
    Scope,
)
from firstdue.domain.registry import AgentDescriptor
from firstdue.errors import NotFoundError

#: The version every agent in this build is published at.
FLEET_VERSION: Final[str] = "1.0.0"
#: The department that runs the fleet and subscribes to all eight.
HOME_DEPARTMENT: Final[Department] = Department.FIRE
#: Fixed publication timestamp, so seeding is byte-identical on every run.
PUBLISHED_AT: Final[datetime] = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

_SCHEMA_ROOT: Final[str] = "firstdue.schemas"


def _agent(
    agent_id: str,
    *,
    publisher: Department,
    loop: Loop,
    role_summary: str,
    capabilities: set[Capability],
    scopes: set[Scope],
    classifications: set[Classification],
    write_targets: tuple[str, ...] = (),
    approval: ApprovalThreshold = ApprovalThreshold.NONE,
    latency_ms: int,
    input_schema: str,
    output_schema: str,
) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        version=FLEET_VERSION,
        publisher_department=publisher,
        loop=loop,
        role_summary=role_summary,
        capabilities=frozenset(capabilities),
        required_scopes=frozenset(scopes),
        classifications_accessed=frozenset(classifications),
        write_targets=write_targets,
        approval_threshold=approval,
        input_schema_ref=f"{_SCHEMA_ROOT}.{input_schema}",
        output_schema_ref=f"{_SCHEMA_ROOT}.{output_schema}",
        latency_target_ms=latency_ms,
        published_at=PUBLISHED_AT,
    )


# ------------------------------------------------------------- the slow loop

RECORDS_WATCHER = _agent(
    "records-watcher",
    # Published by the building department: it owns the permit and violation
    # systems, so it owns the agent that reads them. The fire department
    # subscribes to a pinned version rather than writing its own scraper.
    publisher=Department.BUILDING,
    loop=Loop.SLOW,
    role_summary="Polls permit, assessor, inspection, and violation records into facts.",
    capabilities={Capability.READ},
    scopes={Scope.READ_PUBLIC_RECORDS, Scope.WRITE_PROFILE},
    classifications={Classification.PUBLIC},
    latency_ms=120_000,
    input_schema="SourcePollRequest",
    output_schema="FactBatch",
)

HAZARD_WATCHER = _agent(
    "hazard-watcher",
    # Published by county emergency management, because they hold the Tier II
    # filings. The fire department subscribes to a pinned version rather than
    # being handed the filings themselves -- the subscription *is* the
    # authorization boundary, and it is the reason this agent exists separately
    # from the records watcher.
    publisher=Department.COUNTY_OEM,
    loop=Loop.SLOW,
    role_summary="Reads EPA, PHMSA, NREL, and Tier II registries into classified hazard facts.",
    capabilities={Capability.READ},
    scopes={Scope.READ_PUBLIC_RECORDS, Scope.READ_TIER_II_METADATA, Scope.WRITE_PROFILE},
    classifications={Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL},
    latency_ms=180_000,
    input_schema="HazardPollRequest",
    output_schema="FactBatch",
)

GEOMETRY_WATCHER = _agent(
    "geometry-watcher",
    publisher=Department.FIRE,
    loop=Loop.SLOW,
    role_summary="Derives roof geometry and collapse zones from imagery and lidar.",
    capabilities={Capability.READ, Capability.WRITE},
    scopes={
        Scope.READ_PUBLIC_RECORDS,
        Scope.READ_GEOMETRY,
        Scope.WRITE_PROFILE,
        Scope.WRITE_PREINCIDENT_PLAN,
    },
    classifications={Classification.PUBLIC},
    write_targets=("preincident-plan-store",),
    latency_ms=300_000,
    input_schema="GeometryRequest",
    output_schema="GeometrySpec",
)

CONFLICT_DETECTOR = _agent(
    "conflict-detector",
    publisher=Department.FIRE,
    loop=Loop.SLOW,
    role_summary="Runs the deterministic conflict rules and records disagreements.",
    # No WRITE capability and no model: this agent decides nothing an officer
    # cannot re-derive from the rule id and the fact ids it cites.
    capabilities={Capability.READ},
    scopes={Scope.READ_PROFILE, Scope.WRITE_PROFILE},
    classifications={Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL},
    latency_ms=30_000,
    input_schema="MaterializeRequest",
    output_schema="ConflictBatch",
)

SURVEY_RANKER = _agent(
    "survey-ranker",
    publisher=Department.FIRE,
    loop=Loop.SLOW,
    role_summary="Ranks a district's structures for physical survey and cuts work orders.",
    capabilities={Capability.READ, Capability.RANK, Capability.WRITE},
    scopes={Scope.READ_PROFILE, Scope.WRITE_WORK_ORDER},
    classifications={Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL},
    write_targets=("inspection-work-orders",),
    # Committing a company's morning is a supervisor's call, not an agent's.
    approval=ApprovalThreshold.SUPERVISOR,
    latency_ms=60_000,
    input_schema="RankRequest",
    output_schema="SurveyQueue",
)

REFERRAL_CLERK = _agent(
    "referral-clerk",
    publisher=Department.FIRE,
    loop=Loop.SLOW,
    role_summary="Drafts inter-agency referrals from open conflicts and files approved ones.",
    capabilities={Capability.READ, Capability.WRITE},
    scopes={Scope.READ_PROFILE, Scope.WRITE_REFERRAL},
    classifications={Classification.PUBLIC},
    write_targets=("building-referral-intake",),
    # A referral accuses a property owner of something. A captain signs it.
    approval=ApprovalThreshold.SUPERVISOR,
    latency_ms=60_000,
    input_schema="ReferralRequest",
    output_schema="ReferralRecord",
)

# --------------------------------------------------------- the incident loop

INCIDENT_CONTROLLER = _agent(
    "incident-controller",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Opens the incident, loads one profile snapshot, and streams the brief.",
    capabilities={Capability.READ},
    scopes={Scope.READ_PROFILE, Scope.READ_GEOMETRY, Scope.READ_EMS_DERIVED},
    classifications={
        Classification.PUBLIC,
        Classification.RESTRICTED,
        Classification.TIER_II_CONFIDENTIAL,
        Classification.PHI,
    },
    # The instant brief contains no model call, so this budget is a read, a
    # render, and a write to the log. Exceeding it is a defect, not a slow day.
    latency_ms=500,
    input_schema="DispatchEvent",
    output_schema="BriefEmission",
)

AGENCY_NOTIFIER = _agent(
    "agency-notifier",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Notifies mutual-aid, utility, and OEM partners of incident conditions.",
    capabilities={Capability.READ, Capability.NOTIFY, Capability.WRITE},
    scopes={Scope.READ_PROFILE, Scope.NOTIFY_AGENCY, Scope.REQUEST_UTILITY_SHUTOFF},
    classifications={Classification.PUBLIC, Classification.RESTRICTED},
    write_targets=("agency-notifications",),
    # Telling an agency is autonomous; cutting their gas is not.
    approval=ApprovalThreshold.CHIEF,
    latency_ms=5_000,
    input_schema="NotificationRequest",
    output_schema="NotificationReceipt",
)

BRIEF_RECONCILER = _agent(
    "brief-reconciler",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Assembles the three-stage brief and streams it to the commander.",
    capabilities={Capability.READ},
    # It reads the snapshot and the EMS-derived life-safety fact, and it writes
    # nothing outside the department: an emission goes to the incident log,
    # which the recorder owns.
    scopes={Scope.READ_PROFILE, Scope.READ_GEOMETRY, Scope.READ_EMS_DERIVED},
    classifications={
        Classification.PUBLIC,
        Classification.RESTRICTED,
        Classification.TIER_II_CONFIDENTIAL,
        Classification.PHI,
    },
    # The enriched stage waits on a model. The instant stage that precedes it is
    # the one with the 500 ms budget, and it is the controller's.
    latency_ms=5_000,
    input_schema="ProfileSnapshot",
    output_schema="BriefEmission",
)

SENSOR_FUSION = _agent(
    "sensor-fusion",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Registers thermal frames to building faces and detects voids.",
    # No WRITE capability: that means writing *outside* the department's own
    # store, and a thermal observation goes to the profile and the incident
    # log. The write:profile scope below is what it actually needs.
    capabilities={Capability.READ},
    # Writing a thermal observation amends the brief and appends to the log, so
    # it is a write scope. The authorization matrix test refused to let a
    # viewer do it, which is how that was settled.
    scopes={Scope.READ_PROFILE, Scope.READ_GEOMETRY, Scope.WRITE_PROFILE},
    classifications={Classification.PUBLIC, Classification.RESTRICTED},
    # A frame that registers slower than this is a frame describing a fire that
    # has moved. Void detection is a fixed threshold, not a search.
    latency_ms=2_000,
    input_schema="ThermalFrame",
    output_schema="ThermalObservation",
)

INCIDENT_RECORDER = _agent(
    "incident-recorder",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Writes the append-only incident log through to the records system.",
    capabilities={Capability.READ, Capability.WRITE},
    scopes={Scope.READ_PROFILE, Scope.READ_AUDIT, Scope.WRITE_RMS},
    classifications={Classification.PUBLIC, Classification.RESTRICTED},
    write_targets=("department-rms",),
    latency_ms=15_000,
    input_schema="IncidentLogEntry",
    output_schema="WriteReceipt",
)


#: The fleet, in publication order. Eleven agents, five write targets, two
#: loops, three publishing departments.
FLEET: Final[tuple[AgentDescriptor, ...]] = (
    RECORDS_WATCHER,
    HAZARD_WATCHER,
    GEOMETRY_WATCHER,
    CONFLICT_DETECTOR,
    SURVEY_RANKER,
    REFERRAL_CLERK,
    INCIDENT_CONTROLLER,
    BRIEF_RECONCILER,
    SENSOR_FUSION,
    AGENCY_NOTIFIER,
    INCIDENT_RECORDER,
)


def fleet_descriptors() -> tuple[AgentDescriptor, ...]:
    """Every descriptor this build publishes, sorted for deterministic seeding."""
    return tuple(sorted(FLEET, key=lambda d: (d.agent_id, d.version)))


def descriptor_for(agent_id: str, version: str = FLEET_VERSION) -> AgentDescriptor:
    """Look up a built-in descriptor.

    Raises:
        NotFoundError: for an agent this build does not publish. Guessing at a
            descriptor would mean guessing at its scopes.
    """
    for descriptor in FLEET:
        if descriptor.agent_id == agent_id and descriptor.version == version:
            return descriptor
    raise NotFoundError(
        "no such agent in this build's fleet",
        details={"agent_id": agent_id, "version": version},
    )
