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
#:
#: Bumped from ``1.0.0`` when two descriptors changed content: ``sensor-fusion``
#: took its latency cap from 2 s to 12 s, and ``incident-recorder`` gained
#: ``read:public-records``. A published version is immutable, so changing what a
#: descriptor *says* without changing what it is *called* is refused by the
#: catalog at seed time -- correctly. This is the "publish a new version
#: instead" the guard asks for.
#:
#: Bumped again to ``1.2.0`` for ``incident-interceptor``'s latency cap, 6 s to
#: 12 s. Same rule, and worth stating as a rule rather than a changelog: a
#: descriptor's *content* is what the version names. Editing a comment beside
#: one costs nothing; editing a number inside one costs a version, because
#: somewhere there is a pinned subscription that promised that number.
#:
#: And ``1.3.0`` for the same field again, 12 s to 20 s, because 12 s was
#: measured against a model call when the thing it caps is a whole stage. Two
#: bumps for one number in one afternoon is the catalog doing its job: each one
#: is a promise that changed, and a pin that survived the change would have
#: been a lie about what the agent is allowed to take.
#:
#: ``1.4.0`` cuts ``records-watcher`` from 120 s to 40 s so the serial pass
#: reaches the other four agents while somebody is still looking at the screen.
FLEET_VERSION: Final[str] = "1.4.0"
#: The department that runs the fleet and subscribes to all eight.
HOME_DEPARTMENT: Final[Department] = Department.FIRE
#: Fixed publication timestamp, so seeding is byte-identical on every run.
PUBLISHED_AT: Final[datetime] = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

#: When the four superseded agents stopped being scheduled.
#:
#: They are **not deleted**. Version pinning in this system exists because a
#: NIOSH line-of-duty-death investigation reconstructs what a commander knew
#: two years later, and every brief records the agent versions that produced
#: it. An ``agent_id`` that vanishes from the catalog turns a recorded run into
#: an unresolvable reference -- the replay would say the brief was produced by
#: something this build has never heard of.
#:
#: So they stay published and become **deprecated**: still resolvable, no
#: longer routed, no longer given a worker or a service account.
SUPERSEDED_AT: Final[datetime] = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

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
    deprecated_at: datetime | None = None,
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
        deprecated_at=deprecated_at,
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
    # 40 s, down from 120 s, and the reason is the *shape* of the pass rather
    # than anything this agent does wrong.
    #
    # `_run_one_pass` runs the fleet serially -- records, geometry, hazard,
    # structure-watch, then the clerk -- and this agent is first. It also uses
    # every second it is given: `_RETRIEVAL_SHARE` spends 35 % paging live
    # municipal feeds (measured: `$offset` walking 650, 700 ... 1200 at ~0.5 s
    # a page) and hands the rest to extraction, which is model calls. So a
    # 120 s budget here is 120 s in which *no other slow agent has run*, and a
    # console loaded during it correctly draws four idle agents beside one
    # working -- which reads as a broken fleet and is really a queue.
    #
    # Measured, one live pass: 261 s total, of which this agent held the first
    # ~120 s. The other three emitted 4, 17 and 146 events once they finally
    # got to run.
    #
    # What 40 s costs is records per pass, and that is the cheap side of the
    # trade: the slow loop is cumulative and the choreography starts a pass
    # every 25 s, so the district fills in over several short passes instead of
    # one long one. What it buys is every agent visibly working within about a
    # minute of a page load, which is the thing the loop exists to show.
    #
    # The real fix is running the three independent watchers concurrently --
    # they read disjoint sources and write disjoint keys. That needs
    # `ProfileMaterializer` to stop skipping on a held lock and stop dropping
    # writes on `StaleVersionError` first; both assume a contending pass would
    # compute the same result, which is true of a duplicate pass and false of a
    # different agent. Until then this is the honest lever.
    latency_ms=40_000,
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
    deprecated_at=SUPERSEDED_AT,
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
    deprecated_at=SUPERSEDED_AT,
)

#: Supersedes ``conflict-detector`` and ``survey-ranker``.
#:
#: They were split because detection and ranking are different *kinds* of work,
#: and that turned out to be a distinction without a boundary: ranking reads
#: the conflicts detection just wrote, on the same profiles, in the same pass,
#: and neither one was ever useful without the other. Two Cloud Run services
#: were paying to hand a district's profiles to each other.
#:
#: What the merge buys is that a conflict's severity and a structure's rank are
#: computed from one reading of one profile set, so they cannot disagree about
#: what the corpus said.
STRUCTURE_WATCH = _agent(
    "structure-watch",
    publisher=Department.FIRE,
    loop=Loop.SLOW,
    role_summary=(
        "Watches profiles, runs the deterministic conflict rules, and ranks "
        "structures and conflicts by importance into the department's queue."
    ),
    capabilities={Capability.READ, Capability.RANK, Capability.WRITE},
    scopes={Scope.READ_PROFILE, Scope.WRITE_PROFILE, Scope.WRITE_WORK_ORDER},
    classifications={Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL},
    write_targets=("inspection-work-orders",),
    # NONE, and this is a correction rather than a relaxation. `survey-ranker`
    # published SUPERVISOR while nothing on the work-order path ever called the
    # gateway -- the catalog claimed a human approved something no human
    # approved. Work orders are autonomous by design: a work order commits the
    # department's own morning, and the department's own agent may do that.
    # The referral, which accuses a property owner, still needs a captain.
    approval=ApprovalThreshold.NONE,
    latency_ms=60_000,
    input_schema="MaterializeRequest",
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
    deprecated_at=SUPERSEDED_AT,
)

AGENCY_NOTIFIER = _agent(
    "agency-notifier",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Notifies mutual-aid, utility, and OEM partners of incident conditions.",
    capabilities={Capability.READ, Capability.NOTIFY, Capability.WRITE},
    # Both commitment scopes, because this agent exercises both. Five resource
    # kinds are approval-gated and they split across the two: gas and electric
    # shutoff are `write:utility-shutoff`; road closure, a county hazmat team,
    # and collapse rescue are `write:road-closure`.
    #
    # Only the first was declared. It worked, because the runtime checks that
    # the *grant* covers what the descriptor declares -- not that what the
    # agent exercises is declared -- and the incident grant carries both. So
    # the catalog under-stated this agent's authority: a department reading the
    # descriptor would not learn it can ask police to close a street. It would
    # also have broken the day anyone narrowed the incident grant to the
    # declared scopes, which is the obvious least-privilege hardening.
    scopes={
        Scope.READ_PROFILE,
        Scope.NOTIFY_AGENCY,
        Scope.REQUEST_UTILITY_SHUTOFF,
        Scope.REQUEST_ROAD_CLOSURE,
    },
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
    deprecated_at=SUPERSEDED_AT,
)

#: Supersedes ``incident-controller`` and ``brief-reconciler``.
#:
#: The controller opened the incident and emitted the instant brief; the
#: reconciler emitted every stage after it. One agent produced stage one and a
#: different agent produced stages two and three of the same document, which
#: made the 500 ms budget and the model boundary land on opposite sides of a
#: service boundary for no reason either of them asked for.
#:
#: The merge also gives the intake somewhere to live. A 911 call or a CAD
#: dispatch arrives as prose, and until now the incident loop took only the
#: fields CAD happened to put in the envelope. This agent reads the narrative,
#: extracts what it says, and **routes the incident to the other incident
#: agents by their declared capabilities**.
#:
#: The routing is deterministic. The model extracts; a rule table matched
#: against :class:`AgentDescriptor` capabilities decides who is woken. A model
#: that could choose which agents run would be making an authorisation
#: decision, which section 6 puts out of its reach.
INCIDENT_INTERCEPTOR = _agent(
    "incident-interceptor",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary=(
        "Reads the 911 or CAD intake, opens the incident on one profile "
        "snapshot, streams the three-stage brief, and routes the incident to "
        "the other incident agents by their declared capabilities."
    ),
    capabilities={Capability.READ, Capability.NOTIFY},
    scopes={Scope.READ_PROFILE, Scope.READ_GEOMETRY, Scope.READ_EMS_DERIVED},
    classifications={
        Classification.PUBLIC,
        Classification.RESTRICTED,
        Classification.TIER_II_CONFIDENTIAL,
        Classification.PHI,
    },
    # The **slowest** stage's budget, not the fastest. This was 500 ms for one
    # release and that was a defect: `budget_seconds` treats
    # `latency_target_ms` as a hard cap on every run of the agent, so a 500 ms
    # target would have timed out all three model-bearing stages -- the
    # enriched prose and the crew brief at 4 s and the intake read at 5 s --
    # against a real Vertex endpoint. Fake mode answers in microseconds, so
    # nothing failed locally and the whole incident loop would have degraded on
    # the first live call.
    #
    # The gap between 5 s and this is not slack, it is the reserve. A stage
    # whose model deadline equalled the cap would be cancelled by the runtime
    # at the same instant the model gave up, so the refusal the loop knows how
    # to record and route around would be replaced by a handler that recorded
    # nothing and woke nobody. What the second buys is everything after the
    # model call: the screen, the log entry, the amendment, the routing and the
    # wakes -- or, on the package stage, the readiness verdict and the solve.
    #
    # The instant brief does not need this number to protect it. It is emitted
    # synchronously outside any runtime run and is already checked against
    # `settings.instant_brief_budget_ms`, where exceeding it is logged as a
    # defect rather than silently truncated.
    #
    # 6 s was still wrong, and wrong in the way the paragraph above warns
    # about: it was set from stage budgets nobody had measured. One live
    # `gemini-3.5-flash` compose on this project costs 5.72-6.97 s, so the cap
    # on the *whole run* was below the mean cost of a single model call inside
    # it. Every entry-package composition was cancelled by the runtime before
    # the crew brief returned, `_last_entry_package` was never set, and the
    # loop reported "the composing run ended without staging an entry package"
    # -- the exact message `run_entry_package` writes for a run the runtime
    # cancelled. No package, therefore no optimal path and no crew brief, on
    # every live incident.
    #
    # 12 s was measured against the wrong thing and failed by half a second.
    # It was set to the slowest stage *deadline* (10 s) plus a 2 s reserve --
    # but a stage is not a model call. Timed on a live intake: the screened
    # extract returned at 3.8 s, the `FocusComposer` graph closed at 11.2 s,
    # and the runtime cancelled the run at 12.0 s -- 0.5 s after the focus was
    # composed and before the handler could hand back its result. The incident
    # then failed to open at all, with "the intake produced no result".
    #
    # The intake stage is the slowest because it is two model-bearing phases,
    # not one: a Gemini extract under `INTAKE_DEADLINE_MS`, then a LangGraph
    # focus composition that recalls from the memory bank and plans. Worst case
    # is the extract taking its full 10 s and the graph its observed 7 s, plus
    # the writes and the event publish -- about 18.5 s.
    #
    # 20 s covers that with the reserve the paragraph above argues for. It is
    # also, deliberately, what `COMPOSITION_CAP` reserves against the two-minute
    # ceiling: the runtime will cancel *any* stage of this agent at this number,
    # so the budget has to assume the worst one.
    #
    # `COMPOSITION_CAP` in `firstdue.incident.autonomy` reads this number off
    # the catalog, and `COMPOSE_DEADLINE` is solved against it, so raising it
    # here moves the fallback earlier rather than pushing the card past the
    # two-minute ceiling.
    latency_ms=20_000,
    input_schema="DispatchEvent",
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
    # It was 2 s, and the sentence beside it read: "a frame that registers
    # slower than this is a frame describing a fire that has moved." That is a
    # true thing about *frames* and it was the wrong thing to enforce here,
    # because this number is not a staleness rule -- it is the hard cap the
    # runtime cancels the run at, and the vision call happens inside it.
    #
    # Measured against live Vertex, one frame at `_FRAME_PX`: 5.2 s, 5.4 s,
    # 5.9 s, 7.1 s, 9.0 s cold. Every one of those is over 2 s, so on a live
    # incident every frame was cancelled, every wall stayed UNSCANNED,
    # `thermal.coverage` could never pass, and the agent that reads walls
    # recorded one line -- that four faces were unscanned -- for a whole fire.
    # Fake mode answers in microseconds, which is why 2 s looked fine for as
    # long as nobody flew a sweep against a real model.
    #
    # 12 s is the cold call plus `FRAME_WORK_RESERVE_MS` and the writes after
    # it, and it is the number the sweep's own arithmetic is now written
    # against: `MAX_FACE_ATTEMPTS` is 2, so a wall that genuinely cannot be
    # read costs up to 24 s of a sweep rather than the 4 s it used to. That is
    # the price of the frames that *can* be read arriving at all.
    #
    # Staleness is still enforced, and by the thing that should enforce it:
    # `DEFAULT_COVERAGE_WINDOW` lapses a face back to UNSCANNED five minutes
    # after the frame that covered it, whatever the run cost.
    latency_ms=12_000,
    input_schema="ThermalFrame",
    output_schema="ThermalObservation",
)

INCIDENT_RECORDER = _agent(
    "incident-recorder",
    publisher=Department.FIRE,
    loop=Loop.INCIDENT,
    role_summary="Writes the append-only incident log through to the records system.",
    capabilities={Capability.READ, Capability.WRITE},
    # No READ_AUDIT. The recorder *writes* to the audit sink -- record_event
    # and record_decision -- and never reads it; that scope belongs to the
    # audit console route a human opens. Declaring it meant the incident grant
    # did not cover what the catalog claimed, so routing the recorder through
    # the runtime produced a DENIED run. The mirror image of the
    # agency-notifier finding: that one under-declared and worked by accident,
    # this one over-declared and failed once anything checked.
    # ``read:public-records`` is what lets this agent close the slow loop's
    # open questions. A question carries the classification of the records
    # that raised it, and the memory bank refuses a recall the caller holds
    # no scope for -- so without this the recorder reads every thread as
    # absent and silently resolves nothing. Fail-closed, and the failure is
    # invisible: the loop looks built and never completes.
    scopes={Scope.READ_PROFILE, Scope.WRITE_RMS, Scope.READ_PUBLIC_RECORDS},
    classifications={Classification.PUBLIC, Classification.RESTRICTED},
    write_targets=("department-rms",),
    latency_ms=15_000,
    input_schema="IncidentLogEntry",
    output_schema="WriteReceipt",
)


#: The fleet, in publication order. Thirteen descriptors -- nine scheduled and
#: four superseded -- across five write targets, two loops, and three
#: publishing departments.
#: Everything this build publishes, live and superseded alike. The catalog is
#: the record; :data:`ACTIVE_FLEET` is what actually runs.
FLEET: Final[tuple[AgentDescriptor, ...]] = (
    RECORDS_WATCHER,
    HAZARD_WATCHER,
    GEOMETRY_WATCHER,
    STRUCTURE_WATCH,
    REFERRAL_CLERK,
    INCIDENT_INTERCEPTOR,
    SENSOR_FUSION,
    AGENCY_NOTIFIER,
    INCIDENT_RECORDER,
    # Superseded. Still resolvable so a recorded run replays; never scheduled.
    CONFLICT_DETECTOR,
    SURVEY_RANKER,
    INCIDENT_CONTROLLER,
    BRIEF_RECONCILER,
)

#: The agents that are scheduled, routed, and given a worker and a service
#: account. Everything else in the catalog is history.
#:
#: Derived rather than listed, so adding a ``deprecated_at`` is the single edit
#: that retires an agent -- a second hand-maintained list would be a way for the
#: catalog and the infrastructure to disagree, which is exactly what
#: ``registry/routing.py`` exists to prevent.
ACTIVE_FLEET: Final[tuple[AgentDescriptor, ...]] = tuple(
    d for d in FLEET if d.deprecated_at is None
)


def fleet_descriptors() -> tuple[AgentDescriptor, ...]:
    """Every descriptor this build publishes, sorted for deterministic seeding."""
    return tuple(sorted(FLEET, key=lambda d: (d.agent_id, d.version)))


def active_descriptors() -> tuple[AgentDescriptor, ...]:
    """The scheduled agents, sorted. What routing and Terraform derive from."""
    return tuple(sorted(ACTIVE_FLEET, key=lambda d: (d.agent_id, d.version)))


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
