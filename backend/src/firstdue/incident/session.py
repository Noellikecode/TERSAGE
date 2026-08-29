"""One running incident, and everything the API needs to drive it.

The session holds the emissions produced so far so the SSE stream can replay
them in order to a reconnecting tablet. It is not the record -- the incident log
is -- but it is the ordered, already-persisted view the stream reads from, which
is what makes a resumed stream show what the original one sent rather than a
fresh render that might differ.

Late data never delays earlier output. Each stage produces a new emission and
appends it; nothing here waits on a source before emitting what it already has.
That is what lets the 911 intake exist at all: it needs a model, so it runs
*after* stage one is persisted and arrives as a marked amendment.

Since the merge there is one incident-loop agent, ``incident-interceptor``, and
one handler registered for it. Which piece of its work a run performs is carried
in the run's ``stage`` parameter rather than in a second agent id, because the
controller, the brief and the intake are three stages of one document and the
catalog now says so.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Final

from firstdue.agents.fleet import FleetRunner
from firstdue.container import Container
from firstdue.domain.briefs import BriefEmission
from firstdue.domain.conflicts import ConflictResolution, ConflictStatus
from firstdue.domain.enums import (
    ApprovalThreshold,
    Classification,
    Department,
    FaceLabel,
    LogEntryType,
    Operation,
    PolicyAction,
    Scope,
    SourceType,
)
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.geometry import GeometrySpec
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import ProfileEvent, ProfileEventType, ProfileSnapshot
from firstdue.domain.values import TextValue
from firstdue.domain.work import ApprovalRequest, ApprovalStatus
from firstdue.errors import NotAuthorizedError, NotFoundError, StaleVersionError, ValidationError
from firstdue.extraction.coercion import coerce_value
from firstdue.gateway.engine import AccessRequest
from firstdue.incident.autonomy import (
    COMPOSE_DEADLINE,
    MAX_ERROR_CHARS,
    AutonomyDiagnostics,
    AutonomyState,
    AutonomyTrigger,
    readiness_signature,
)
from firstdue.incident.autonomy import (
    decide as decide_autonomy,
)
from firstdue.incident.controller import IncidentController, OpenIncidentResult
from firstdue.incident.crewbrief import SECTION_ORDER
from firstdue.incident.crewbrief import compose as compose_crew_brief
from firstdue.incident.drone import (
    SYNTHETIC_SOURCE,
    camera_bearing_for,
    next_face,
    sweep_permitted,
    synthetic_frame,
)
from firstdue.incident.entrypath import EntryPathPlan, GeoOrigin, compute_entry_path
from firstdue.incident.fusion import (
    DEFAULT_FRAME_DEADLINE_MS,
    FrameAnalysis,
    SensorFusion,
    ThermalFrame,
    VoidObservation,
)
from firstdue.incident.handoff import RULES_BY_ID, WAKE_RULES, Handoff, RoutingPlan
from firstdue.incident.intake import (
    CHANNEL_LABEL,
    IntakeChannel,
    IntakeReader,
    IntakeReading,
    reported_sections,
    signals_from,
)
from firstdue.incident.interceptor import AGENT_ID, IncidentInterceptor, InterceptResult
from firstdue.incident.interceptor import AGENT_ID as INTERCEPTOR_AGENT_ID
from firstdue.incident.packages import (
    BRIEF_HALF,
    PATH_HALF,
    EntryPackage,
    PackageStatus,
    approval_id_for,
    get_package,
    list_packages,
    package_content,
)
from firstdue.incident.provenance import (
    authors_of,
    authors_of_geometry,
    credit,
    rules_behind,
    structural_authors,
)
from firstdue.incident.provenance import (
    name as name_agents,
)
from firstdue.incident.readiness import HAZARD_KEYS, ReadinessAssessment
from firstdue.incident.readiness import assess as assess_readiness
from firstdue.incident.reconciler import NarrativeChunk, Reconciler
from firstdue.incident.recorder import IncidentRecorder
from firstdue.incident.resources import ResourceAgent, ResourceOutcome
from firstdue.incident.timer import truss_time_window
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS
from firstdue.ports.runtime import AgentInput, AgentOutcome, Grant
from firstdue.registry.descriptors import FLEET_VERSION
from firstdue.services.grants import GrantService
from firstdue.settings import AppEnv

logger = get_logger(__name__)

#: The merged incident-loop agent, which is also the agent an IC resolution is
#: attributed to: the resolution is a fact the incident loop wrote.
IC_AGENT: Final[str] = AGENT_ID

#: Which piece of the interceptor's work a run performs. Two stages reach the
#: runtime -- the enriched brief and the intake -- and they are told apart by a
#: parameter rather than by two agent ids, because they are two stages of one
#: document produced by one agent.
STAGE_PARAM: Final[str] = "stage"
STAGE_ENRICHED: Final[str] = "enriched"
STAGE_INTAKE: Final[str] = "intake"
#: Assessing readiness, solving the entry path and synthesising the crew brief.
#: One stage rather than three, because they are one decision: the assessment
#: describes the record, the path is solved over exactly that record, and the
#: brief is prose about both. Three runs would let a console approve a path
#: computed against data a later assessment had already contradicted.
STAGE_ENTRY_PACKAGE: Final[str] = "entry-package"

#: How many times one wall is flown before the sweep gives up on it and moves
#: to the next. Two, so a single dropped connection costs a retry and a wall
#: that genuinely cannot be read costs four seconds rather than the incident.
MAX_FACE_ATTEMPTS: Final[int] = 2

#: Left inside a `sensor-fusion` run for everything that happens *after* the
#: vision call returns: resolving the frame, repainting the massing model,
#: recording the analysis and emitting the amendment. Without this reserve the
#: vision client is handed the whole run and the runtime cancels the handler
#: mid-write, which loses the reading and the reason for losing it together.
FRAME_WORK_RESERVE_MS: Final[int] = 400

#: The least a vision call is worth attempting with. Below this the answer is a
#: refusal either way, and a stated one arrives faster.
MIN_FRAME_DEADLINE_MS: Final[int] = 250


def _frame_deadline_ms(deadline: datetime | None, now: datetime) -> int | None:
    """How long the vision call may take, inside the run that is wrapping it.

    ``SensorFusion.analyze_frame`` defaults to 8 s and the ``sensor-fusion``
    descriptor caps the whole run at 2 s, so the vision client was being handed
    four times the budget the runtime would actually allow it. The failure that
    produced was the bad kind: the runtime cancelled the handler at 2 s, the
    ``TimeoutError`` became a ``TIMED_OUT`` run record, and the *frame* -- the
    thing an officer is waiting for -- vanished with no entry in the incident
    log saying which wall had not been read or why.

    Deriving it from the run's own deadline makes the vision client refuse
    inside the run instead, which is a value with a reason: the analysis path
    records "read no coverage off the frame", the face stays UNSCANNED, and the
    sweep is told, in time to fly the next wall.
    """
    if deadline is None:
        return None
    remaining_ms = int((deadline - now).total_seconds() * 1000.0)
    return max(MIN_FRAME_DEADLINE_MS, remaining_ms - FRAME_WORK_RESERVE_MS)


#: Everything a composition still has to do *after* the model has worded the
#: brief: two approval cards staged, the package written to the incident log,
#: and the two analysis entries that explain it. Five record writes, which in
#: fake mode are dictionary inserts and against Firestore are round trips.
#:
#: Reserved rather than hoped for. The composition runs inside a run the
#: runtime cancels at the ``incident-interceptor`` descriptor's six seconds, and
#: the brief's model deadline was a flat 4 s written next to it -- so an
#: assessment and a solve that together cost two seconds left the model with
#: four seconds it was entitled to spend and the staging with none. What that
#: produced is the worst failure shape this system has: the run was cancelled
#: mid-``compose_entry_package``, no package was stored, ``run_entry_package``
#: raised into a shrug, and the incident log said nothing at all. A commander
#: watched a two-minute clock run out against an empty screen three times.
PACKAGE_WORK_RESERVE_MS: Final[int] = 1_500


def _brief_deadline_ms(deadline: datetime | None, now: datetime) -> int | None:
    """How long the crew brief's model call may take, inside the run wrapping it.

    The same argument as :func:`_frame_deadline_ms` and the same shape, because
    it is the same defect: a stage-level deadline written as a constant beside a
    run-level cap enforced by the runtime, with nothing keeping the two honest.
    Derived from the run's own deadline, the model refuses *inside* the run --
    which is a value with a reason, because the deterministic wording is already
    built and the package stages carrying it.

    ``None`` when the run declared no deadline, which leaves
    :data:`~firstdue.incident.crewbrief.CREW_BRIEF_DEADLINE_MS` standing.
    """
    if deadline is None:
        return None
    remaining_ms = int((deadline - now).total_seconds() * 1000.0)
    return remaining_ms - PACKAGE_WORK_RESERVE_MS


def _one(payload: AgentInput, key: str) -> str:
    """Read one identifier from an agent input.

    ``AgentInput.ids`` allows a tuple, because some handoffs carry several fact
    ids. The incident handlers each work on exactly one incident, and reading a
    tuple as though it were a string is the kind of mistake that only shows up
    on a fireground.
    """
    value = payload.ids[key]
    if isinstance(value, tuple):
        if len(value) != 1:
            raise ValidationError(
                "this handler works on exactly one identifier",
                details={"key": key, "count": str(len(value))},
            )
        return value[0]
    return value


class IncidentSession:
    """The incident loop's live state, per process."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self.recorder = IncidentRecorder(
            incident_log=container.incident_log,
            write_actions=container.write_actions,
            audit=container.audit,
            clock=container.clock,
            ids=container.ids,
            rms=container.write_targets.get("department-rms"),
            # Synthesis and loop-closing. The recorder runs after the incident
            # has closed, so it is the one incident agent with room to reason:
            # nothing waits on it and its output is a draft a human reviews.
            memory=container.memory,
            model=container.model,
            use_langgraph=container.settings.langgraph_enabled,
            max_graph_steps=container.settings.agent_graph_max_steps,
            # Left unset, this took the constructor's `"1.0.0"` default, so
            # every entry the recorder wrote was stamped with a version string
            # nobody chose -- and after the fleet moved to 1.1.0 the console
            # attributed the whole incident log to a version that is no longer
            # published. The provenance claim this project rests on is that an
            # emission names the *pinned* version that produced it; a literal
            # in a constructor signature is not that.
            agent_version=FLEET_VERSION,
        )
        self.controller = IncidentController(
            incidents=container.incidents,
            profiles=container.profiles,
            snapshots=container.snapshots,
            grants=GrantService(
                grants=container.grants,
                audit=container.audit,
                clock=container.clock,
                ids=container.ids,
            ),
            recorder=self.recorder,
            city=container.city,
            clock=container.clock,
            ids=container.ids,
            bus=container.bus,
        )
        self.reconciler = Reconciler(
            clock=container.clock, ids=container.ids, model=container.model
        )
        # The intake reads a citizen-authored transcript, so it goes through the
        # same document screen every ingested narrative does. The screen is the
        # container's, not a second one built here: two screens with different
        # configurations is how one of them ends up not running.
        self.interceptor = IncidentInterceptor(
            intake=IntakeReader(model=container.model, screen=container.screen),
            registry=container.registry,
            waker=_FleetWaker(self),
            # The head reads what the slow loop accumulated -- profile, conflicts
            # and the questions it could not settle -- and writes a focus the
            # other incident agents read. It points at ids; it never asserts.
            incident_log=container.incident_log,
            memory=container.memory,
            grounding=container.grounding,
            use_langgraph=container.settings.langgraph_enabled,
            max_graph_steps=container.settings.agent_graph_max_steps,
        )
        self.fusion = SensorFusion(vision=container.vision, ids=container.ids)
        self.resources = ResourceAgent(
            policy=container.policy,
            approvals=container.approvals,
            write_actions=container.write_actions,
            target=container.write_targets["agency-notifications"],
            audit=container.audit,
            clock=container.clock,
            ids=container.ids,
            # ``log`` is the predicate for reasoning at all: it is how the
            # notifier reads the head's focus and what it has already told a
            # partner. Without it the deterministic rule table is what runs.
            log=container.incident_log,
            memory=container.memory,
            model=container.model,
            use_langgraph=container.settings.langgraph_enabled,
            max_graph_steps=container.settings.agent_graph_max_steps,
        )
        #: Persisted emissions, in order, per incident. What the stream replays.
        self._emissions: dict[str, list[BriefEmission]] = {}
        self._grants: dict[str, IncidentGrant] = {}
        # Work staged for a runtime handler, keyed by the correlation id of the
        # run that will pick it up. An AgentInput carries identifiers, never
        # payloads, so a thermal frame cannot travel inside one.
        self._pending_frames: dict[str, ThermalFrame] = {}
        #: Raw imagery staged for the fusion agent's own extraction path.
        self._pending_imagery: dict[str, dict[str, Any]] = {}
        self._pending_requests: dict[str, dict[str, Any]] = {}
        #: A narrative staged for the interceptor's intake run, keyed by the
        #: correlation id of the run that will read it. It travels here rather
        #: than inside the ``AgentInput`` for the same reason a thermal frame
        #: does: an envelope carries identifiers, never record content, and a
        #: 911 transcript is the most sensitive content the loop touches.
        self._pending_narratives: dict[str, tuple[str, IntakeChannel, str]] = {}
        #: What each woken agent was handed, per incident. Held so the agent
        #: that later acts on this incident can read the reported context that
        #: caused it to be woken.
        self._handoffs: dict[str, dict[str, Handoff]] = {}
        self._last_intercept: dict[str, InterceptResult] = {}
        # Where a handler leaves its typed result for the caller. An
        # AgentOutcome carries identifiers; the route still wants the object.
        self._last_thermal: dict[str, dict[str, Any]] = {}
        # Keyed by *correlation id*, not incident id.
        #
        # It was keyed by incident, which is correct only while one resource
        # request is in flight at a time. Two concurrent requests on the same
        # incident overwrote each other here, and each caller then popped
        # whichever outcome landed last -- so a notification to one agency could
        # be reported under another's name. Nothing in the runtime serialises
        # these; the console merely happened to send them one at a time.
        self._last_resource: dict[str, ResourceOutcome] = {}
        # The target storey and the trigger a package run was asked for, keyed
        # by the run's correlation id, and where that run leaves the package it
        # composed. Same shape as every other staged handoff on this session:
        # an ``AgentInput`` carries identifiers and a run's typed result comes
        # back here rather than through the envelope.
        self._pending_packages: dict[str, tuple[int, str]] = {}
        self._last_entry_package: dict[str, EntryPackage] = {}

        #: The storey a caller said was burning, per incident, one-based.
        #:
        #: Kept because the entry path needs it and nothing else carries it that
        #: far: the log records which attributes a narrative bound but not their
        #: values -- the transcript has exactly one home and the log is not it --
        #: and the intake response is returned once, to the console. Without
        #: this the solver had no way to learn the floor the call reported, so
        #: it routed every crew to the ground storey of a building whose graph
        #: had all five.
        self._reported_floor: dict[str, int] = {}

        #: What the loop has already decided about composing a package on its
        #: own, per incident. See :mod:`firstdue.incident.autonomy`.
        self._autonomy: dict[str, AutonomyState] = {}
        #: One sleeping task per incident, holding the fallback deadline. Not a
        #: poll: it wakes once, asks the same question every other hook asks,
        #: and is cancelled the moment a package exists.
        self._deadline_timers: dict[str, asyncio.Task[None]] = {}
        #: Faces the sweep tried and could not read, and how many attempts each
        #: has cost. A wall in ``_abandoned_faces`` stays UNSCANNED -- it is
        #: skipped, never assumed clear.
        self._face_attempts: dict[str, dict[FaceLabel, int]] = {}
        self._abandoned_faces: dict[str, set[FaceLabel]] = {}
        #: The last verdict *and reason* every readiness criterion produced, per
        #: incident. Readiness is re-evaluated at every point an input to it
        #: changes -- a frame, an intake, a resolution, the sweep stopping --
        #: and that evaluation was silent, so a criterion going from outstanding
        #: to met left no trace and the loop looked idle between the intake and
        #: the package. This is what a later evaluation is compared against, so
        #: only movement is written down. See :meth:`_record_criteria_movement`.
        self._criteria_seen: dict[str, dict[str, tuple[bool, str]]] = {}
        #: Coverage voids already written down, as ``face:region``. A void is
        #: re-derived from the whole record on every frame, so without this the
        #: same two on Alpha would be re-reported after every later pass.
        self._voids_seen: dict[str, set[str]] = {}

        # The incident loop runs through the same runtime the slow loop does.
        # Its agents are *not* given standing grants -- incident authority is
        # bound to one incident, one address, and one responding agency, and it
        # is revoked at close -- so every run here passes the incident grant
        # explicitly and the runner refuses without one.
        self.fleet = FleetRunner(
            runtime=container.runtime,
            registry=container.registry,
            grants=GrantService(
                grants=container.grants,
                clock=container.clock,
                ids=container.ids,
                audit=container.audit,
            ),
            runs=container.runs,
            clock=container.clock,
            ids=container.ids,
            only_agent=container.settings.firstdue_agent,
        )
        self.fleet.register_all(
            {
                AGENT_ID: self._interceptor_handler,
                "sensor-fusion": self._fusion_handler,
                "agency-notifier": self._notifier_handler,
                "incident-recorder": self._recorder_handler,
            }
        )

    # ------------------------------------------------------- runtime handlers
    #
    # Each is registered against the descriptor it implements. They are thin on
    # purpose: the work already lives in the agent objects, and what the
    # runtime adds is the grant check, the deadline, the terminal state, and
    # the durable run record naming the pinned version that produced it.

    async def _interceptor_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """The one handler for the merged incident-loop agent.

        Two stages reach the runtime. The instant stage does not: it has no
        model on its path and is emitted synchronously as the incident opens,
        which is what its 500 ms budget describes.
        """
        incident_id = _one(payload, "incident_id")
        stage = payload.parameters.get(STAGE_PARAM, STAGE_ENRICHED)
        if stage == STAGE_ENTRY_PACKAGE:
            target_level, trigger = self._pending_packages.pop(payload.correlation_id, (0, ""))
            package = await self.compose_entry_package(
                incident_id,
                target_level=target_level,
                trigger=trigger,
                # The run's own deadline, for the same reason the frame handler
                # passes it: the synthesis is the only unbounded thing on this
                # path, and a synthesis that overruns has to become a stated
                # refusal on a staged package rather than a cancelled handler
                # that leaves no package and no reason.
                deadline=payload.deadline,
            )
            self._last_entry_package[incident_id] = package
            return AgentOutcome(emitted_event_ids=(package.package_id,))
        if stage == STAGE_INTAKE:
            staged = self._pending_narratives.pop(payload.correlation_id, None)
            if staged is None:  # pragma: no cover - the caller always stages one
                return AgentOutcome()
            narrative, channel, source_ref = staged
            result = await self.intercept(
                incident_id,
                narrative=narrative,
                channel=channel,
                source_ref=source_ref,
                correlation_id=payload.correlation_id,
            )
            self._last_intercept[incident_id] = result
            return AgentOutcome(
                emitted_event_ids=tuple(e.emission_id for e in (result.emission,) if e is not None)
            )

        emission = await self.emit_enriched(incident_id)
        return AgentOutcome(emitted_event_ids=(emission.emission_id,))

    async def _fusion_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Registering a thermal frame amends the brief and appends to the log.

        Two entry points converge here: a pre-extracted frame from a ground
        station, and raw imagery the agent extracts itself. The second is the
        autonomous path, and it produces the first.
        """
        incident_id = _one(payload, "incident_id")

        staged = self._pending_imagery.pop(payload.correlation_id, None)
        if staged is not None:
            result = await self.analyze_imagery(
                incident_id,
                # The run's own deadline, not the vision client's default. See
                # ``_frame_deadline_ms``: a frame that overruns has to come back
                # as a stated refusal inside the run rather than as a cancelled
                # handler that leaves no reason anywhere.
                deadline_ms=_frame_deadline_ms(payload.deadline, self._container.clock.now()),
                **staged,
            )
            self._last_thermal[incident_id] = result
            return AgentOutcome()

        frame = self._pending_frames.pop(payload.correlation_id, None)
        if frame is None:  # pragma: no cover - the caller always stages one
            return AgentOutcome()
        result = await self.register_thermal(incident_id, frame)
        self._last_thermal[incident_id] = result
        return AgentOutcome()

    async def _notifier_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Telling an agency is autonomous; cutting their gas needs a chief."""
        request = self._pending_requests.pop(payload.correlation_id, None)
        if request is None:  # pragma: no cover - the caller always stages one
            return AgentOutcome()
        incident_id = _one(payload, "incident_id")
        outcome = await self.request_resource(incident_id, **request)
        self._last_resource[payload.correlation_id] = outcome
        return AgentOutcome(
            write_action_ids=tuple(ref for ref in (outcome.external_ref,) if ref),
            policy_decision_ids=(outcome.decision_id,),
        )

    async def _recorder_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Drain buffered log entries into the records system."""
        result = await self.recorder.flush_to_rms(incident_id=_one(payload, "incident_id"))
        # The count is what the recorder reports; the entry ids stay in the log
        # where they belong. A run record naming them would duplicate the log.
        return AgentOutcome(
            write_action_ids=(f"rms-flush:{result.flushed}",) if result.flushed else ()
        )

    # ------------------------------------------------------------ emissions

    async def emit_instant(self, opened: OpenIncidentResult) -> BriefEmission:
        """Build and persist the instant brief. No model call on this path."""
        truss = None
        truss_fact = opened.snapshot.facts.get(Keys.LIGHTWEIGHT_TRUSS)
        if truss_fact is not None and truss_fact.value.is_known:
            truss = truss_time_window(
                dispatched_at=opened.incident.dispatched_at,
                now=self._container.clock.now(),
                fact_id=truss_fact.fact_id,
            )

        emission = self.reconciler.instant(
            opened.snapshot,
            incident_id=opened.incident.incident_id,
            collapse_zone_m=(
                opened.snapshot.geometry.collapse_zone_radius_m
                if opened.snapshot.geometry
                else None
            ),
            truss_window=truss,
        )
        incident_id = opened.incident.incident_id
        self._grants[incident_id] = opened.grant
        # Where the 90 s budget starts counting, and the only place it can:
        # this is the moment the incident exists, on the same clock every entry
        # in its record is stamped with. Arming it here rather than at the
        # first intake means an incident nobody ever sends a narrative for
        # still reaches a staged package.
        self._autonomy[incident_id] = AutonomyState(opened_at=self._container.clock.now())
        self._arm_deadline(incident_id)
        return await self._persist(emission)

    async def run_enrichment(self, incident_id: str, *, correlation_id: str) -> BriefEmission:
        """Enrich through the runtime, under the incident's own grant.

        The work is unchanged. What the runtime adds is the grant check before
        it, the descriptor's latency target around it, and a durable run record
        naming the pinned version that produced the emission -- which is what a
        NIOSH investigation asks for and what a direct call never wrote.
        """
        grant = await self._require_grant(incident_id)
        await self.fleet.run(
            AGENT_ID,
            correlation_id=correlation_id,
            parameters={STAGE_PARAM: STAGE_ENRICHED},
            ids={"incident_id": incident_id},
            grant=grant,
        )
        emission = self.latest(incident_id)
        if emission is None:  # pragma: no cover - enrichment always emits one
            raise NotFoundError("enrichment produced no emission", details={"id": incident_id})
        return emission

    async def run_intake(
        self,
        incident_id: str,
        *,
        narrative: str,
        channel: IntakeChannel,
        source_ref: str,
        correlation_id: str,
    ) -> InterceptResult:
        """Read the intake through the runtime, under the incident's own grant.

        Deliberately a separate run from the one that opened the incident. The
        instant brief is already persisted and streamed when this starts, so a
        model that is slow, refusing, or down costs an amendment that never
        arrives -- never a brief that never arrives.
        """
        grant = await self._require_grant(incident_id)
        self._pending_narratives[correlation_id] = (narrative, channel, source_ref)
        try:
            run = await self.fleet.run(
                AGENT_ID,
                correlation_id=correlation_id,
                parameters={STAGE_PARAM: STAGE_INTAKE},
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_narratives.pop(correlation_id, None)
        result = self._last_intercept.pop(incident_id, None)
        if result is None:
            # The same lesson `run_entry_package` already learned, and this
            # path had not: "the handler always sets one" is true in fake mode
            # and false against a real model. A run the runtime cancelled on
            # its deadline leaves this slot empty, and the bare message named
            # neither the deadline nor the run -- so a cancelled intake
            # surfaced as a 404 on `POST /incidents` and the incident did not
            # open at all. The run record knows why; say what it says.
            raise NotFoundError(
                "the intake produced no result",
                details={
                    "id": incident_id,
                    "run_status": str(run.result.status),
                    "run_error_code": run.result.error_code or "",
                    "run_id": run.record.run_id,
                },
            )
        # Outside the run that just finished, never inside it. A composition
        # started from within ``intercept`` would inherit the intake run's
        # remaining budget and be cancelled by it, which is the same mistake
        # the vision deadline above fixes -- and it would nest a run inside a
        # run, so the package's own run record would be a child of a read.
        await self._consider_entry_package(incident_id)
        return result

    async def run_thermal_registration(
        self, incident_id: str, frame: ThermalFrame, *, correlation_id: str
    ) -> dict[str, Any]:
        """Register a thermal frame through the runtime."""
        grant = await self._require_grant(incident_id)
        self._pending_frames[correlation_id] = frame
        try:
            await self.fleet.run(
                "sensor-fusion",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_frames.pop(correlation_id, None)
        result = self._last_thermal.pop(incident_id, {})
        await self._consider_entry_package(incident_id)
        return result

    async def run_frame_analysis(
        self,
        incident_id: str,
        *,
        image: bytes,
        mime_type: str,
        camera_bearing_deg: float,
        source: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Run the imagery path through the runtime, like any other agent work."""
        grant = await self._require_grant(incident_id)
        self._pending_imagery[correlation_id] = {
            "image": image,
            "mime_type": mime_type,
            "camera_bearing_deg": camera_bearing_deg,
            "source": source,
        }
        try:
            await self.fleet.run(
                "sensor-fusion",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_imagery.pop(correlation_id, None)
        result = self._last_thermal.pop(incident_id, {})
        # A frame is the input that moves thermal coverage, which is the last
        # readiness criterion to go green on an ordinary incident. This is
        # therefore the hook that fires on the happy path, on the final wall.
        await self._consider_entry_package(incident_id)
        return result

    async def run_drone_sweep_step(
        self, incident_id: str, *, correlation_id: str
    ) -> dict[str, Any]:
        """Fly one face of a synthetic drone sweep through **Sensor Fusion**.

        One face per call rather than all four, so the console can advance the
        sweep on a cadence and an officer watches the thermal arrive wall by
        wall. It is also the honest shape: four faces flown in one request
        would report a whole building scanned at a single instant, which is not
        what a drone does.

        Nothing here reads a frame or decides a temperature. It picks the next
        unflown wall, works out where a camera must point to see it, and hands
        the bytes to the same path a ground station uses. Every refusal is a
        value with a reason -- a live vision model, an address the slow loop
        never profiled, a face the footprint does not have.
        """
        refusal = sweep_permitted(
            vision_model_ref=self._container.vision.model_ref,
            simulation_declared=self._container.settings.demo_synthetic_sweep,
        )
        if refusal:
            # Recorded, not merely returned.
            #
            # This agent declining to read a generated building with a live
            # model *is* the agent working, and it was the one outcome that left
            # no trace anywhere: the reason went back to the caller as a string,
            # the console printed it once in a corner, and `sensor-fusion` --
            # which had been asked to do the thing it exists for and had given a
            # considered answer -- read as an agent that had done nothing at all
            # for the length of the incident.
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline="declined to fly the synthetic sweep",
                detail=refusal,
                refs=[self._container.vision.model_ref],
            )
            # The sweep is over before it began, and that is a terminal state:
            # no further frame is coming, so whatever thermal coverage this
            # incident has is all it will ever have. The loop composes on it
            # rather than waiting out a deadline for an aircraft that was
            # refused takeoff.
            await self._consider_entry_package(incident_id, sweep_terminated=True)
            return {"flown": False, "complete": False, "reason": refusal}

        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        spec = snapshot.geometry if snapshot is not None else None
        if spec is None:
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline="cannot resolve a frame to a wall",
                detail=(
                    "no pre-incident geometry for this address: the slow loop has to "
                    "measure a footprint before a camera bearing means anything"
                ),
                refs=[incident.address_id],
            )
            await self._consider_entry_package(incident_id, sweep_terminated=True)
            return {
                "flown": False,
                "complete": False,
                "reason": (
                    "no geometry was profiled for this address before the incident, so "
                    "a frame cannot be resolved to a wall"
                ),
            }

        now = self._container.clock.now()
        coverage = self.fusion.coverage(incident_id, now=now)
        scanned = frozenset(entry.face for entry in coverage if entry.scanned)
        abandoned = frozenset(self._abandoned_faces.get(incident_id, ()))
        face = next_face(scanned, spec, abandoned=abandoned)
        if face is None:
            # Deciding not to fly is a decision. The agent recomputed coverage,
            # found every wall inside the currency window, and stopped -- which
            # is the correct end of a sweep and read on the console as the sweep
            # having quietly stalled, because the last three faces each produced
            # a card and finishing produced nothing.
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline=(
                    "the SIMULATED sweep is complete; nothing left to fly"
                    if not abandoned
                    else f"the SIMULATED sweep is over; {len(abandoned)} face(s) unreadable"
                ),
                detail=(
                    f"all {len(coverage) - len(abandoned)} of {len(coverage)} face(s) have "
                    "coverage inside the currency window; a face whose frame ages out goes "
                    "back to UNSCANNED and is flown again"
                    + (
                        f"; {len(abandoned)} face(s) were given up on and stay UNSCANNED"
                        if abandoned
                        else ""
                    )
                ),
                refs=[
                    SYNTHETIC_SOURCE,
                    *(str(entry.face) for entry in coverage),
                    *(str(entry) for entry in sorted(abandoned)),
                ],
            )
            await self._consider_entry_package(incident_id, sweep_terminated=True)
            return {
                "flown": False,
                "complete": True,
                "abandoned": sorted(str(entry) for entry in abandoned),
                "reason": (
                    "every face has current coverage"
                    if not abandoned
                    else "every face is either covered or has been given up on"
                ),
            }

        bearing = camera_bearing_for(face, spec)
        if bearing is None:  # pragma: no cover - next_face only returns faces that exist
            await self._consider_entry_package(incident_id, sweep_terminated=True)
            return {
                "flown": False,
                "complete": False,
                "reason": f"the footprint has no {face} face to photograph",
            }

        # The choice, before the frame that answers it.
        #
        # Picking the next unflown wall off current coverage and working out
        # where a camera has to point to see it is the whole of this method's
        # reasoning, and it left no trace: the only entry the sweep produced was
        # the reading that came back, so the agent appeared to react to frames
        # rather than to decide which wall was flown next and why.
        pending = [str(entry.face) for entry in coverage if not entry.scanned]
        await self.recorder.record_analysis(
            incident_id,
            agent_id="sensor-fusion",
            # SIMULATED in the headline, on the same terms the reading below
            # carries it: every frame this method flies is generated, so an
            # entry announcing a pass over a wall is announcing an aircraft that
            # does not exist and has to say so where it is read.
            headline=f"flying a SIMULATED pass of the {face} face, camera bearing {bearing:.0f}",
            detail=(
                f"{len(pending)} face(s) UNSCANNED before this pass; the bearing comes off the "
                "footprint the slow loop measured, not off the caller"
            ),
            refs=[str(face), SYNTHETIC_SOURCE, incident.address_id, *pending],
        )

        result = await self.run_frame_analysis(
            incident_id,
            image=synthetic_frame(address_id=incident.address_id, face=face),
            mime_type="image/png",
            camera_bearing_deg=bearing,
            source=SYNTHETIC_SOURCE,
            correlation_id=correlation_id,
        )
        after = self.fusion.coverage(incident_id, now=self._container.clock.now())
        read = any(entry.face is face and entry.scanned for entry in after)
        if not read:
            abandoned = await self._face_unread(incident_id, face)
        remaining = [
            str(entry.face) for entry in after if not entry.scanned and entry.face not in abandoned
        ]
        complete = not remaining

        # Coverage recomputed over the whole building, after the pass.
        #
        # This is a second, distinct read from the one that chose the face: the
        # frame has landed, currency has been re-evaluated against the clock for
        # every wall -- including ones flown earlier, which lapse back to
        # UNSCANNED when their frame ages out -- and *that* is what decides
        # whether the sweep continues. It is the step that ends the sweep, and
        # it left no entry at all: the log jumped from a frame being read to the
        # sweep being over, with the arithmetic between them nowhere.
        scanned_now = [entry for entry in after if entry.scanned]
        hottest_now = max(
            (entry.peak_c for entry in scanned_now if entry.peak_c is not None), default=None
        )
        await self.recorder.record_analysis(
            incident_id,
            agent_id="sensor-fusion",
            headline=(
                # SIMULATED, on the same terms every other entry this method
                # produces carries it: the coverage being recomputed here counts
                # a generated frame as a wall that has been seen, and a card
                # saying "3 of 4 faces current" without the mark would be
                # claiming a building had been flown.
                f"recomputed coverage after the SIMULATED {face} pass: "
                f"{len(scanned_now)} of {len(after)} face(s) current"
            ),
            detail=(
                (
                    f"peak {hottest_now:.0f} C across the faces that carry a frame; "
                    if hottest_now is not None
                    else ""
                )
                + (
                    f"{len(remaining)} face(s) still UNSCANNED and still unknown"
                    if remaining
                    else "no wall is left unflown"
                )
                + (
                    f"; {len(abandoned)} face(s) given up on and left UNSCANNED"
                    if abandoned
                    else ""
                )
                + (
                    "; currency is re-checked every pass, so a wall whose frame ages out "
                    "goes back to UNSCANNED and is flown again"
                )
            ),
            refs=[
                str(face),
                SYNTHETIC_SOURCE,
                *remaining,
                *sorted(str(entry) for entry in abandoned),
            ],
        )

        if complete:
            # The last wall, or the last wall anybody is going to get. Either
            # way the record has stopped changing and the loop decides on it.
            await self._consider_entry_package(incident_id, sweep_terminated=True)
        return {
            "flown": True,
            "complete": complete,
            "face": str(face),
            "camera_bearing_deg": round(bearing, 1),
            "source": SYNTHETIC_SOURCE,
            "remaining": remaining,
            "abandoned": sorted(str(entry) for entry in abandoned),
            **result,
        }

    async def _face_unread(self, incident_id: str, face: FaceLabel) -> frozenset[FaceLabel]:
        """Count a failed pass, and give up on the wall once it has cost enough.

        The sweep picks the first UNSCANNED face in a fixed order, so a wall
        whose frame never registers is picked again on the very next call and
        for ever after -- one slow Alpha and Bravo, Charlie and Delta are never
        flown at all. Under a 90 s budget that is the difference between three
        walls read and none.

        Giving up is not a verdict about the wall. The face stays UNSCANNED,
        the readiness criterion still fails on it, the route still prices a
        traverse across it as unknown, and the abandonment is recorded under
        `sensor-fusion`'s own name so an officer reads "we could not see that
        side" rather than inferring it from a card that never appeared.
        """
        attempts = self._face_attempts.setdefault(incident_id, {})
        attempts[face] = attempts.get(face, 0) + 1
        if attempts[face] < MAX_FACE_ATTEMPTS:
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline=f"read nothing off the {face} face; it will be flown again",
                detail=(
                    f"attempt {attempts[face]} of {MAX_FACE_ATTEMPTS}; the face stays "
                    "UNSCANNED, which is unknown and never clear"
                ),
                refs=[str(face), SYNTHETIC_SOURCE],
            )
            return frozenset(self._abandoned_faces.get(incident_id, ()))
        given_up = self._abandoned_faces.setdefault(incident_id, set())
        given_up.add(face)
        await self.recorder.record_analysis(
            incident_id,
            agent_id="sensor-fusion",
            headline=f"gave up on the {face} face after {attempts[face]} attempt(s)",
            detail=(
                "the sweep moves to the walls it can still read rather than spending the "
                "incident on this one; the face stays UNSCANNED and every downstream "
                "reader prices it as unknown"
            ),
            refs=[str(face), SYNTHETIC_SOURCE],
        )
        return frozenset(given_up)

    async def run_resource_request(
        self,
        incident_id: str,
        *,
        correlation_id: str,
        kind_id: str,
        detail: str,
        approval_id: str | None,
    ) -> ResourceOutcome:
        """Ask for a resource through the runtime."""
        grant = await self._require_grant(incident_id)
        self._pending_requests[correlation_id] = {
            "kind_id": kind_id,
            "detail": detail,
            "approval_id": approval_id,
        }
        try:
            await self.fleet.run(
                "agency-notifier",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_requests.pop(correlation_id, None)
        outcome = self._last_resource.pop(correlation_id, None)
        if outcome is None:  # pragma: no cover - the handler always sets one
            raise NotFoundError("resource request produced no outcome", details={"id": incident_id})
        return outcome

    async def emit_enriched(self, incident_id: str) -> BriefEmission:
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to enrich", details={"incident_id": incident_id})
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})
        emission = await self.reconciler.enriched(previous, snapshot)
        stored = await self._persist(emission)
        await self._record_composition(stored)
        return stored

    async def _record_composition(self, emission: BriefEmission) -> None:
        """What the reconciler made of the record, in the interceptor's name.

        ``BRIEF_EMITTED`` records that a version landed and carries the two
        booleans as fields, which is the right shape for a record and the wrong
        shape for a card: nothing in it reads as an agent having done something.
        Stage two is the one place in this loop where a model writes prose an
        officer reads, and the outcome worth seeing is the unhappy one -- a
        brief with no narrative because none was ever wanted and a brief with no
        narrative because the composition was refused look identical on screen
        and are entirely different facts.
        """
        if emission.narrative_available:
            composed = "prose composed and accepted"
        elif emission.model_invoked:
            composed = "the composed prose was refused or never arrived; the brief lands without it"
        else:
            composed = "no narrative model is wired, so this stage carries none"
        await self.recorder.record_analysis(
            emission.incident_id,
            agent_id=INTERCEPTOR_AGENT_ID,
            headline=f"composed the enriched brief, version {emission.version}",
            detail=(
                f"{composed}; {len(emission.sections)} section(s), "
                f"{len(emission.unknowns)} unknown attribute(s), "
                f"{len(emission.conflict_ids)} open conflict(s), "
                f"{len(emission.unavailable)} source(s) unavailable"
            ),
            refs=[emission.emission_id, emission.profile_snapshot_id],
        )

    async def require_enrichable(self, incident_id: str) -> tuple[BriefEmission, ProfileSnapshot]:
        """Resolve everything enrichment needs, or raise before anything streams.

        Called by the route *before* the response begins. An error raised
        inside a streaming generator has already had ``200 OK`` and the SSE
        content type written to the socket, so it cannot become an error
        envelope -- the connection just breaks. Prerequisites are resolved here
        so an unknown incident is an ordinary 404.
        """
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to enrich", details={"incident_id": incident_id})
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})
        return previous, snapshot

    async def emit_enriched_streaming(
        self, incident_id: str, prepared: tuple[BriefEmission, ProfileSnapshot] | None = None
    ) -> AsyncIterator[NarrativeChunk | BriefEmission]:
        """Enrich, streaming the prose as it composes.

        The chunks pass straight through -- they are provisional prose and the
        log does not store them. The emission at the end goes through the same
        ``_persist`` every other emission does, so what the record holds is
        unchanged by the fact that the prose was watched being written.
        """
        previous, snapshot = prepared or await self.require_enrichable(incident_id)

        async for item in self.reconciler.enriched_streaming(previous, snapshot):
            if isinstance(item, BriefEmission):
                stored = await self._persist(item)
                # Recorded on this path too, and it is the path the console
                # actually takes. Recording only on the non-streaming one would
                # have left the enrichment invisible in exactly the deployment
                # the complaint came from.
                await self._record_composition(stored)
                yield stored
            else:
                yield item

    async def emit_amendment(self, incident_id: str, **kwargs: Any) -> BriefEmission:
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to amend", details={"incident_id": incident_id})
        return await self._persist(self.reconciler.amendment(previous, **kwargs))

    # --------------------------------------------------------------- intake

    async def intercept(
        self,
        incident_id: str,
        *,
        narrative: str,
        channel: IntakeChannel,
        source_ref: str,
        correlation_id: str,
    ) -> InterceptResult:
        """Read the narrative, amend the brief, route the incident, wake them.

        Four steps, and the order is the safety argument:

        1. **Read**, bounded and rejectable. A failure here is a value.
        2. **Record** what was read -- including that nothing was, because an
           unread intake is the thing an investigation most needs to see.
        3. **Amend**, marked, and only when something was actually reported. An
           amendment carrying nothing would still bump the version and make a
           commander re-read a brief that did not change.
        4. **Route and wake**, deterministically, from the declared capabilities
           of the catalogued incident agents.

        Nothing in steps 1-4 can reach the instant brief: it was persisted and
        transmitted before this method had an incident to be called with.
        """
        incident = await self._require_incident(incident_id)
        reading = await self.interceptor.read_intake(
            narrative, incident_id=incident_id, channel=channel, source_ref=source_ref
        )
        await self.recorder.record_intake(
            incident_id,
            channel=str(reading.channel),
            source_ref=reading.source_ref,
            accepted=reading.accepted,
            reported_keys=reading.reported_keys,
            unknowns=reading.unknowns,
            model_ref=reading.model_ref,
            screen=reading.screen,
            screen_findings=reading.screen_findings,
            dropped_values=reading.dropped_values,
            rejection_reason=reading.rejection_reason,
        )
        await self._record_read(incident_id, reading)

        signals = signals_from(reading)
        # Remembered before anything is routed, so a path solved later in this
        # incident goes to the storey the caller named. A later call that names
        # a different floor replaces it: the most recent report is the one a
        # commander is acting on.
        if signals.reported_floor_of_origin is not None:
            self._reported_floor[incident_id] = signals.reported_floor_of_origin
        emission = await self._intake_amendment(incident, reading)

        now = self._container.clock.now()
        # Planned against this incident's own authority, so a rule that matches
        # an agent the grant cannot cover is a stated gap rather than a denied
        # run recorded on every incident.
        grant = await self.grant_for(incident_id)
        plan = await self.interceptor.route(
            reading, now=now, authorised_scopes=frozenset(grant.scopes)
        )
        await self._record_routing(incident_id, plan)
        self._stage_handoffs(incident_id, plan)
        started = await self.interceptor.wake_all(
            plan, incident_id=incident_id, correlation_id=correlation_id
        )
        for handoff in plan.handoffs:
            await self.recorder.record_handoff(
                incident_id,
                agent_ref=handoff.agent_ref,
                rule_ids=handoff.rule_ids,
                intake_keys=handoff.intake_keys,
                note=handoff.note,
                started=handoff.agent_id in started,
            )
        for entry in plan.withheld:
            await self.recorder.record_handoff(
                incident_id,
                agent_ref=entry.agent_ref,
                rule_ids=entry.rule_ids,
                intake_keys=(),
                note=(
                    f"Withheld from {entry.agent_id}: this incident's grant does not "
                    f"carry {', '.join(entry.missing_scopes)}."
                ),
                started=False,
                missing_scopes=entry.missing_scopes,
            )

        # Composed last, on purpose. The instant brief has already emitted and
        # every routed agent has already been woken, so a graph that stalls or
        # fails here costs a commander nothing -- ADR 0004 makes stage one
        # model-free by construction, and the focus is guidance layered on top
        # of a fleet that is already moving, never a gate in front of it.
        await self._compose_focus(
            incident_id, reading=reading, grant=grant, now=now, correlation_id=correlation_id
        )

        logger.info(
            "intake_intercepted",
            extra={
                "incident_id": incident_id,
                "accepted": reading.accepted,
                "reported": len(reading.items),
                "woken": ",".join(started),
                "unmatched_rules": ",".join(plan.unmatched_rule_ids),
                "withheld": ",".join(plan.withheld_agent_ids),
            },
        )
        return InterceptResult(
            incident_id=incident_id,
            reading=reading,
            signals=signals,
            plan=plan,
            emission=emission,
            woken_agent_ids=started,
        )

    async def _record_read(self, incident_id: str, reading: IntakeReading) -> None:
        """What the interceptor made of the narrative, under its own id.

        ``record_intake`` files the reading, and files it under the recorder
        that wrote the entry -- so the agent that screened a citizen's words,
        called a model on them and bound attributes back to their spans left
        nothing in the log carrying its own name. Screening a transcript and
        getting six typed fields out of it is the single largest piece of work
        this agent does on an incident, and the console drew it as idle through
        all of it.

        Counts, screen names and canonical keys only. The transcript itself has
        exactly one home in this record and this is not it.
        """
        parts: list[str] = []
        if reading.screen:
            parts.append(
                f"{reading.screen} screened it"
                + (
                    f" and removed {len(reading.screen_findings)} instruction-like passage(s)"
                    if reading.screened
                    else " and found nothing to remove"
                )
            )
        else:
            parts.append("no document screen is wired to inspect it")
        if reading.accepted:
            parts.append(f"{len(reading.reported_keys)} attribute(s) bound to spans in the text")
            if reading.unknowns:
                parts.append(f"{len(reading.unknowns)} left unknown")
            if reading.dropped_values:
                # A value the model returned that is not in the text it claims
                # to come from is the failure mode the span binding exists for,
                # so the count belongs where somebody will see it.
                parts.append(
                    f"{reading.dropped_values} value(s) dropped for not matching the narrative"
                )
        else:
            parts.append("nothing was extracted, so no attribute off this call reaches the brief")
        channel = CHANNEL_LABEL[reading.channel]
        await self.recorder.record_analysis(
            incident_id,
            agent_id=INTERCEPTOR_AGENT_ID,
            headline=(
                f"read the {channel} narrative"
                if reading.accepted
                else f"could not read the {channel} narrative"
            ),
            detail="; ".join(parts),
            refs=[*reading.reported_keys, *reading.unknowns, reading.source_ref],
        )

    async def _record_routing(self, incident_id: str, plan: RoutingPlan) -> None:
        """The routing decision itself, and every rule it could not place.

        The plan produced handoff entries and nothing else, so the decision --
        seven rules evaluated against the catalogue this department publishes,
        against this incident's own authority -- appeared in the log only as its
        successful half. A rule that fired and matched no catalogued agent
        produced no entry at all, which is the exact case the plan carries
        ``unmatched_rule_ids`` to make visible: a department finding out that
        "reported hazardous material" wakes nobody should find it out here.
        """
        await self.recorder.record_analysis(
            incident_id,
            agent_id=INTERCEPTOR_AGENT_ID,
            headline=f"routed the incident to {len(plan.handoffs)} agent(s)",
            detail=(
                f"{len(plan.fired_rule_ids)} of {len(WAKE_RULES)} wake rules fired against the "
                f"catalogue; {len(plan.withheld)} agent(s) withheld for scope this incident's "
                f"grant does not carry; {len(plan.unmatched_rule_ids)} rule(s) matched nobody"
            ),
            refs=[*plan.fired_rule_ids, *plan.woken_agent_ids],
        )
        for rule_id in plan.unmatched_rule_ids:
            rule = RULES_BY_ID.get(rule_id)
            await self.recorder.record_analysis(
                incident_id,
                agent_id=INTERCEPTOR_AGENT_ID,
                headline=f"declined to route {rule_id}: no catalogued agent declares the authority",
                # The rule's own sentence about why it exists, not a second one
                # written here that could drift away from the table.
                detail=(rule.why if rule is not None else "this rule fired and matched nobody"),
                refs=[rule_id],
            )

    async def _compose_focus(
        self,
        incident_id: str,
        *,
        reading: IntakeReading,
        grant: Any,
        now: datetime,
        correlation_id: str,
    ) -> None:
        """Write the head agent's briefing, or carry on without one.

        Every failure path here is a shrug. A focus is *guidance* -- it points
        the other incident agents at ids the head judged material -- and each of
        them already degrades to its own deterministic behaviour when
        ``read_focus`` returns nothing. An exception escaping this method would
        turn an optional improvement into an incident-loop outage, which is the
        opposite trade from the one this system makes everywhere else.
        """
        if not self.interceptor.composes_focus:
            return
        try:
            incident = await self._container.incidents.get(incident_id)
            snapshot = (
                await self._container.snapshots.get(incident.profile_snapshot_id)
                if incident is not None
                else None
            )
            if snapshot is None:
                await self._record_no_focus(
                    incident_id,
                    "this incident opened against no profile snapshot, so there is no "
                    "pre-incident record to point anybody at",
                )
                return
            focus = await self.interceptor.compose_focus(
                incident_id=incident_id,
                snapshot=snapshot,
                now=now,
                reading=reading,
                authorised_scopes=frozenset(grant.scopes),
                correlation_id=correlation_id,
            )
            if focus is not None:
                await self.interceptor.record_focus(focus, now=now)
                return
            await self._record_no_focus(  # pragma: no cover - composes_focus gates this
                incident_id,
                "the composer returned no focus; every agent falls back to its own rules",
            )
        except Exception as exc:  # pragma: no cover - defensive, see docstring
            logger.warning(
                "focus_not_composed",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )
            await self._record_no_focus(
                incident_id,
                f"the composition came apart ({type(exc).__name__}); every agent falls back "
                "to its own rules and the incident is unaffected",
            )

    async def _record_no_focus(self, incident_id: str, reason: str) -> None:
        """A briefing that was attempted and not written, said out loud.

        A deployment that wired a bank and a model has asked its head agent to
        reason about attention on every incident, and an incident where that
        produced nothing is the one an officer most wants to know about -- it is
        the difference between "nothing needed pointing at" and "the thing that
        points at everything did not run". Losing the focus is survivable by
        construction; losing the fact that it was lost is not.

        Swallows its own failure for the reason the caller does: this is the
        optional half of the loop and nothing here may take an incident down.
        """
        try:
            await self.recorder.record_analysis(
                incident_id,
                agent_id=INTERCEPTOR_AGENT_ID,
                headline="carried on without a focus",
                detail=reason,
                refs=[incident_id],
            )
        except Exception as exc:  # pragma: no cover - the log is the last thing left
            logger.warning(
                "focus_failure_not_recorded",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )

    async def _intake_amendment(
        self, incident: Any, reading: IntakeReading
    ) -> BriefEmission | None:
        """Hang the reported lines off a marked amendment, or nothing at all."""
        if not reading.items:
            return None
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:  # pragma: no cover - opening always stores one
            raise NotFoundError(
                "profile snapshot is missing", details={"incident_id": incident.incident_id}
            )
        sections = reported_sections(
            reading,
            signals_from(reading),
            snapshot=snapshot,
            # CAD's alarm level, never the reported one. The reported level is
            # printed beside it and applied to nothing.
            cad_alarm_level=incident.alarm_level,
        )
        if not sections:
            return None
        return await self.emit_amendment(incident.incident_id, reported=sections)

    def _stage_handoffs(self, incident_id: str, plan: RoutingPlan) -> None:
        staged = self._handoffs.setdefault(incident_id, {})
        for handoff in plan.handoffs:
            staged[handoff.agent_id] = handoff

    def handoff_for(self, incident_id: str, agent_id: str) -> Handoff | None:
        """What one agent was handed on this incident, if it was woken."""
        return self._handoffs.get(incident_id, {}).get(agent_id)

    async def _persist(self, emission: BriefEmission) -> BriefEmission:
        """Write to the log, then hold the persisted copy. Never the reverse."""
        stored = await self.recorder.record_emission(emission)
        self._emissions.setdefault(stored.incident_id, []).append(stored)
        return stored

    def latest(self, incident_id: str) -> BriefEmission | None:
        versions = self._emissions.get(incident_id)
        return versions[-1] if versions else None

    def emissions_after(self, incident_id: str, version: int) -> Sequence[BriefEmission]:
        """Everything after a version, in order. What a reconnect replays."""
        return [e for e in self._emissions.get(incident_id, []) if e.version > version]

    def forget(self, incident_id: str) -> None:
        self._emissions.pop(incident_id, None)
        self._grants.pop(incident_id, None)
        self._handoffs.pop(incident_id, None)
        self._last_intercept.pop(incident_id, None)
        # The packages themselves are in the sealed log, which is where a
        # closed incident's record belongs; this only drops the in-flight
        # handoff slot a composing run would have used.
        self._last_entry_package.pop(incident_id, None)
        # The deadline first, and unconditionally. A closed incident whose
        # timer is still sleeping wakes up 45 s later and composes a plan for a
        # fire that is out -- which is the same class of mistake as the thermal
        # left painted on the massing model, and worse, because this one stages
        # two approval cards for it.
        self._cancel_deadline(incident_id)
        self._autonomy.pop(incident_id, None)
        self._face_attempts.pop(incident_id, None)
        self._abandoned_faces.pop(incident_id, None)
        self._criteria_seen.pop(incident_id, None)
        self._voids_seen.pop(incident_id, None)

    # -------------------------------------------------------------- the 360

    async def resolve(
        self,
        incident_id: str,
        *,
        conflict_id: str,
        observed_value: str,
        resolved_by: str,
        note: str,
    ) -> dict[str, Any]:
        """An IC settled a disagreement by looking at the building.

        Writes a live-observation fact, closes the conflict, bumps
        ``profile_version``, records it in the log, and emits a marked
        amendment. All five, because the resolution is worth nothing if the
        brief still shows the disagreement.
        """
        incident = await self._require_incident(incident_id)
        profile = await self._container.profiles.get(incident.address_id)
        if profile is None:
            raise NotFoundError(
                "no profile for this incident", details={"incident_id": incident_id}
            )

        conflict = next((c for c in profile.conflicts if c.conflict_id == conflict_id), None)
        if conflict is None:
            raise NotFoundError("conflict not found", details={"conflict_id": conflict_id})
        if conflict.status is not ConflictStatus.OPEN:
            raise NotFoundError(
                "conflict is already resolved", details={"conflict_id": conflict_id}
            )

        now = self._container.clock.now()
        # Named, because whether the officer's words parsed as the type this
        # attribute is measured in is itself part of the record below.
        parsed = coerce_value(conflict.canonical_key, observed_value)
        value = parsed or TextValue(text=observed_value[:2000])
        fact = StructuralFact(
            fact_id=natural_fact_id(
                address_id=profile.address_id,
                canonical_key=conflict.canonical_key,
                source_ref=f"ic-resolution/{incident_id}/{conflict_id}",
                observed_at=now,
                rendered_value=value.render(),
            ),
            address_id=profile.address_id,
            canonical_key=conflict.canonical_key,
            value=value,
            # A live observation outranks every filed record, by tier.
            source_type=SourceType.IC_RESOLUTION,
            source_ref=f"ic-resolution/{incident_id}/{conflict_id}",
            source_snapshot_id=f"ic:{incident_id}:{conflict_id}",
            observed_at=now,
            ingested_at=now,
            confidence=0.97,
            classification=Classification.PUBLIC,
            produced_by_agent=IC_AGENT,
        )
        await self._container.facts.append(fact)

        resolution = ConflictResolution(
            resolved_at=now,
            resolving_record_id=incident_id,
            resolving_fact_id=fact.fact_id,
            resolved_by=resolved_by,
            note=note or f"Settled on scene during the 360 at incident {incident_id}.",
        )
        await self._container.conflicts.resolve(conflict_id, resolution)

        updated = profile.with_fact(
            fact,
            event=ProfileEvent(
                event_id=f"pevt_ic_{conflict_id.removeprefix('conflict_')}",
                sequence=profile.next_sequence,
                occurred_at=now,
                type=ProfileEventType.CONFLICT_RESOLVED,
                actor=resolved_by,
                summary=f"IC resolution: {conflict.canonical_key} observed as {value.render()}",
                canonical_keys=(conflict.canonical_key,),
                fact_ids=(fact.fact_id,),
                conflict_id=conflict_id,
            ),
        )
        updated = updated.model_copy(
            update={
                "conflicts": tuple(
                    c.resolve(resolution) if c.conflict_id == conflict_id else c
                    for c in updated.conflicts
                )
            }
        )
        durable = True
        try:
            saved = await self._container.profiles.save(
                updated, expected_version=profile.profile_version
            )
        except StaleVersionError:
            logger.info("ic_resolution_contended", extra={"incident_id": incident_id})
            saved = updated
            durable = False

        # Two things happened here that the resolution entry cannot say.
        #
        # The first is whether the observed value parsed as the type its
        # canonical key is measured in or was kept as the officer's own words:
        # a resolution recorded as free text is still authoritative and is not
        # comparable to anything, and a reader has no way to tell the two apart
        # afterwards. The second is whether the write reached the durable
        # profile at all -- losing the race with a slow-loop pass is a correct
        # outcome and a silent one, and it means the next incident at this
        # address opens against the disagreement this one settled.
        await self.recorder.record_analysis(
            incident_id,
            agent_id=IC_AGENT,
            headline=(
                f"settled {conflict.canonical_key} on the 360"
                if durable
                else f"settled {conflict.canonical_key}, but the profile write lost the race"
            ),
            detail=(
                (
                    f"parsed as a {conflict.canonical_key} value"
                    if parsed is not None
                    else "kept as free text: the observation did not parse as this attribute"
                )
                + "; a live observation outranks every filed record and both originals stay"
                + (
                    f"; profile now at version {saved.profile_version}"
                    if durable
                    else "; the durable profile still carries the disagreement"
                )
            ),
            refs=[conflict_id, conflict.canonical_key, fact.fact_id],
        )

        await self.recorder.record_resolution(
            incident_id,
            conflict_id=conflict_id,
            resolved_by=resolved_by,
            note=note,
            fact_id=fact.fact_id,
        )
        await self.recorder.record_observed_fact(
            incident_id,
            fact_id=fact.fact_id,
            canonical_key=conflict.canonical_key,
            source=str(SourceType.IC_RESOLUTION),
        )

        emission = await self.emit_amendment(
            incident_id,
            resolutions=(
                f"{conflict.canonical_key} observed as {value.render()} by {resolved_by}. "
                f"Both original records are retained.",
            ),
        )
        # A fact landing is one of the moments readiness is re-asked at, and
        # this is the only one an incident produces. What it will *not* do
        # today is flip the hazard or conflict criteria: both read the profile
        # snapshot, and that is frozen at dispatch by design, so a resolution
        # written during the incident is not in the record those two evaluate.
        # It stays a hook because it is still a live moment at which a deadline
        # that has quietly passed can be honoured, and because the day the
        # snapshot policy changes is the day this has to already be here.
        await self._consider_entry_package(incident_id)

        return {
            "conflict_id": conflict_id,
            "fact_id": fact.fact_id,
            "profile_version": saved.profile_version,
            "brief_version": emission.version,
            "resolved_by": resolved_by,
        }

    async def register_thermal(self, incident_id: str, frame: ThermalFrame) -> dict[str, Any]:
        """Register a frame and amend the brief with current coverage."""
        self.fusion.register(frame)
        now = self._container.clock.now()
        coverage = self.fusion.coverage(incident_id, now=now)
        voids = self.fusion.voids(incident_id, now=now)

        # In `sensor-fusion`'s own name.
        #
        # The amendment below is emitted by whoever emits briefs, so before
        # this the agent that actually read the frame and resolved it to a wall
        # left no trace under its own id -- it registered four faces and read
        # as idle. The face is the subject: which wall, how hot, and how much
        # of the building is still unscanned.
        unscanned = [str(c.face) for c in coverage if not c.scanned]
        hottest = max(
            (c.peak_c for c in coverage if c.scanned and c.peak_c is not None),
            default=None,
        )
        await self.recorder.record_analysis(
            incident_id,
            agent_id="sensor-fusion",
            headline=f"registered {frame.face} from the drone sweep",
            detail=(
                (f"peak {hottest:.0f} C across scanned faces; " if hottest is not None else "")
                + (
                    f"{len(unscanned)} face(s) still UNSCANNED"
                    if unscanned
                    else "every face scanned"
                )
                + (f"; {len(voids)} coverage void(s)" if voids else "")
            ),
            refs=[frame.frame_id, str(frame.face)],
        )

        await self._record_new_voids(incident_id, voids, source=frame.source)

        emission = await self.emit_amendment(incident_id, thermal=coverage, voids=voids)
        return {
            "frame_id": frame.frame_id,
            "face": str(frame.face),
            "brief_version": emission.version,
            "unscanned_faces": [str(c.face) for c in coverage if not c.scanned],
            "voids": len(voids),
        }

    async def clear_painted_thermal(self, incident_id: str) -> bool:
        """Strip the incident's thermal off the stored massing model.

        The fusion module's rule is that **coverage lapses**: a frame older than
        the window stops counting and the face goes back to UNSCANNED, because
        yesterday's warm wall is not today's warm wall. Painting readings into
        the durable profile quietly broke that -- the cells stayed on the model
        after the incident closed, so a structure opened days later showed a
        heat map from a fire that was out. A stale overlay is the most
        convincing wrong thing that can be on a screen.

        So the paint is undone when the incident is. The readings are not lost:
        they are in the sealed incident log, which is where a reading from a
        closed incident belongs.
        """
        incident = await self._container.incidents.get(incident_id)
        if incident is None:
            return False
        profile = await self._container.profiles.get(incident.address_id)
        if profile is None or profile.geometry is None:
            return False
        cleared = self.fusion.unscanned(profile.geometry)
        if cleared == profile.geometry:
            return False
        try:
            await self._container.profiles.save(
                profile.model_copy(
                    update={
                        "geometry": cleared,
                        "profile_version": profile.profile_version + 1,
                    }
                ),
                expected_version=profile.profile_version,
            )
        except StaleVersionError:
            logger.info("thermal_clear_contended", extra={"incident_id": incident_id})
            return False
        return True

    async def _paint_geometry(
        self,
        # `incident` follows `_require_incident`, which the incident store types
        # as Any; the analysis has a real type and carries it.
        incident: Any,
        analysis: FrameAnalysis,
        spec: GeometrySpec | None,
        *,
        now: datetime,
    ) -> bool:
        """Fold this frame's thermal and structure into the stored massing model.

        Without this the reading exists only in the fusion object and in the
        brief, and the model on screen stays cold -- which is what an officer
        reads as "nobody has flown that wall". The console fetches geometry from
        the profile, so the profile is where a heat map has to land.

        Contention loses deliberately. A slow-loop pass that saved between the
        read and the write means the geometry moved under us; the coverage is
        still in the fusion object and the next frame repaints from the newer
        spec, so the cost of losing is one frame of overlay rather than a
        thermal reading painted over somebody else's measurement.
        """
        if spec is None:
            return False
        profile = await self._container.profiles.get(incident.address_id)
        if profile is None:  # pragma: no cover - an incident implies a profile
            return False
        painted = self.fusion.apply_analysis_to_geometry(
            profile.geometry if profile.geometry is not None else spec,
            analysis,
            incident_id=incident.incident_id,
            now=now,
        )
        try:
            await self._container.profiles.save(
                # The version has to move: the store rejects a write that is not
                # strictly newer than what it holds, which is what stops a slow
                # replayed write from overwriting a fresh one.
                profile.model_copy(
                    update={
                        "geometry": painted,
                        "profile_version": profile.profile_version + 1,
                    }
                ),
                expected_version=profile.profile_version,
            )
        except StaleVersionError:
            logger.info(
                "thermal_paint_contended",
                extra={"incident_id": incident.incident_id},
            )
            return False
        return True

    async def _record_new_voids(
        self, incident_id: str, voids: Sequence[VoidObservation], *, source: str
    ) -> None:
        """One entry per coverage void, the first time it is measured.

        A void is a measured temperature difference between two adjacent regions
        of one wall -- an observation, never a conclusion about what is behind
        it, which is why :class:`~firstdue.incident.fusion.VoidObservation`
        carries its own caveat. It is a finding somebody has to look at, and it
        reached the log only as a number in a sentence: "3 coverage void(s)".
        Which wall, which region, and how big a difference were in the brief and
        in the return value and nowhere an officer reads per-agent work.

        **Only new ones.** Voids are re-derived from the whole record on every
        frame, so Alpha's two would be re-reported after Bravo, Charlie and
        Delta as well -- four times the entries and three times the same
        finding. Keyed by face and region, which is what identifies a void.

        **A void measured off a generated frame says so in its headline**, on
        exactly the terms every other reading the synthetic sweep produces
        carries: the permission to read an imaginary building with a real model
        is granted only because the record names what it is everywhere it
        appears, and a card reading "measured a coverage void on ALPHA" with no
        such mark is the one line that would make that untrue.
        """
        synthetic = source == SYNTHETIC_SOURCE
        seen = self._voids_seen.setdefault(incident_id, set())
        for void in voids:
            key = f"{void.face}:{void.region_index}"
            if key in seen:
                continue
            seen.add(key)
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline=(
                    f"measured a coverage void on {void.face}, region {void.region_index}"
                    f"{' from a SIMULATED frame' if synthetic else ''}"
                ),
                detail=(
                    f"{void.delta_c:.0f} C warmer than the adjacent region against a "
                    f"{void.threshold_c:.0f} C threshold, peak {void.peak_c:.0f} C. "
                    "An observation, not a conclusion: a thermal camera cannot say what "
                    "is behind a wall"
                ),
                refs=[str(void.face), f"region-{void.region_index}", source],
            )

    async def analyze_imagery(
        self,
        incident_id: str,
        *,
        image: bytes,
        mime_type: str,
        camera_bearing_deg: float,
        source: str,
        deadline_ms: int | None = None,
    ) -> dict[str, Any]:
        """Imagery in, amended brief and massing model out.

        The frame is resolved to a face using the footprint the **Geometry
        Watcher** measured during the slow loop, read off the profile snapshot
        this incident opened against. A refusal -- no profile, or a corner shot
        that resolves to no single wall -- is returned as a stated reason and
        amends nothing, because a frame on no wall is not coverage of any wall.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        spec = snapshot.geometry if snapshot is not None else None
        now = self._container.clock.now()

        analysis = await self.fusion.analyze_frame(
            incident_id=incident_id,
            image=image,
            mime_type=mime_type,
            camera_bearing_deg=camera_bearing_deg,
            observed_at=now,
            spec=spec,
            source=source,
            # The fusion module's own default when a caller reached this method
            # outside a run -- a test, a script. Inside a run it is the run's
            # remaining budget, so the vision call refuses in time for this
            # method to record why rather than being cancelled mid-write.
            deadline_ms=DEFAULT_FRAME_DEADLINE_MS if deadline_ms is None else deadline_ms,
        )
        if analysis.rejected is not None:
            # The same argument as the refused sweep above, one layer in. A
            # frame that could not be attributed to a wall means that wall is
            # still UNSCANNED and somebody has to fly it again -- an operational
            # fact, and it went back to the caller as a string and nowhere else.
            # A cold start is the sharpest case: the reason this agent cannot
            # work is that the slow loop never profiled the address, and that
            # belongs in the incident's record, not in a return value.
            await self.recorder.record_analysis(
                incident_id,
                agent_id="sensor-fusion",
                headline=(
                    "cannot read this address at all: the slow loop never profiled it"
                    if analysis.rejected.cold_start
                    else "read no coverage off the frame"
                ),
                # The clearest single statement in this system of why the slow
                # loop matters, so it is stated rather than left to be inferred
                # from a missing footprint. It names no agent because none is
                # recorded: what the snapshot carries is the *absence* of
                # geometry, and "geometry-watcher never ran" is a claim about a
                # process this incident cannot see.
                detail=(
                    analysis.rejected.reason
                    + (
                        "; the profile snapshot this incident opened against carries no "
                        "footprint, and without one there are no face bearings to resolve "
                        "a camera against. The slow loop is what supplies it"
                        if analysis.rejected.cold_start
                        else ""
                    )
                ),
                refs=[
                    r
                    for r in (
                        incident.address_id,
                        incident.profile_snapshot_id,
                        source,
                        analysis.model_ref,
                    )
                    if r
                ],
            )
            return {
                "registered": False,
                "reason": analysis.rejected.reason,
                "cold_start": analysis.rejected.cold_start,
                "model_ref": analysis.model_ref,
            }

        coverage = self.fusion.coverage(incident_id, now=now)
        voids = self.fusion.voids(incident_id, now=now)
        painted = await self._paint_geometry(incident, analysis, spec, now=now)

        # The autonomous path, in `sensor-fusion`'s own name.
        #
        # This is the branch the drone sweep takes -- raw imagery the agent
        # reads itself -- and it is the one that was invisible: four walls
        # flown, the massing model repainted, and nothing in the log carrying
        # the agent's id. The face is the subject; the storey count and the
        # obstructions are what it made of the frame.
        face = str(analysis.frame.face) if analysis.frame else "an unresolved face"
        unscanned = [str(c.face) for c in coverage if not c.scanned]
        detail_parts: list[str] = []
        if analysis.observed_storeys is not None:
            detail_parts.append(f"{analysis.observed_storeys} storey bands observed")
        if analysis.obstructions:
            detail_parts.append(f"{len(analysis.obstructions)} obstruction(s)")
        detail_parts.append(
            f"{len(unscanned)} face(s) still UNSCANNED" if unscanned else "every face scanned"
        )
        if analysis.unknowns:
            # What the model declined to answer travels with what it did.
            detail_parts.append(f"{len(analysis.unknowns)} unknown(s)")
        # Whether the heat actually reached the model on screen. A frame that
        # registered and did not repaint -- geometry that moved under the write,
        # or an address with no footprint -- leaves an officer looking at a cold
        # wall the agent believes it has read, and nothing said so.
        detail_parts.append(
            "massing model repainted" if painted else "the massing model was not repainted"
        )
        synthetic = source == SYNTHETIC_SOURCE
        if synthetic:
            # First, and in the headline, not tucked into the tail of a detail
            # line. A generated frame read by a real model is only permitted at
            # all because the record says so everywhere it appears -- see
            # `sweep_permitted` -- and "everywhere" has to include the one line
            # an officer actually reads on the card.
            detail_parts.insert(0, "SIMULATED frame, not a real aircraft")
        # Whose work made the resolution possible, read off the snapshot rather
        # than assumed from the shape of the data. The footprint itself carries
        # no author -- it has no fact id -- so the headline credits the *loop*
        # and this credits the agents that filed the attributes the geometry is
        # a function of. The wording is exact on purpose: they filed the facts,
        # they did not draw the polygon, and a card that blurred the two would
        # put a name on work nobody recorded doing.
        storey_authors = structural_authors(snapshot) if snapshot is not None else ()
        detail_parts.append(
            credit(
                storey_authors,
                work="the structural facts it is a function of were filed by",
                otherwise="no structural fact on this snapshot names its author",
            )
        )
        await self.recorder.record_analysis(
            incident_id,
            agent_id="sensor-fusion",
            headline=(
                f"read a {'SIMULATED' if synthetic else 'drone'} frame and resolved it to {face} "
                "on the footprint the slow loop measured"
            ),
            detail="; ".join(detail_parts),
            refs=[
                r
                for r in (
                    analysis.frame.frame_id if analysis.frame else None,
                    face,
                    source,
                    *storey_authors,
                )
                if r
            ],
        )

        await self._record_new_voids(incident_id, voids, source=source)

        emission = await self.emit_amendment(incident_id, thermal=coverage, voids=voids)
        return {
            "registered": analysis.registered,
            "geometry_painted": painted,
            "frame_id": analysis.frame.frame_id if analysis.frame else None,
            "face": str(analysis.frame.face) if analysis.frame else None,
            "observed_storeys": analysis.observed_storeys,
            "obstructions": [str(o) for o in analysis.obstructions],
            "unknowns": list(analysis.unknowns),
            "brief_version": emission.version,
            "unscanned_faces": [str(c.face) for c in coverage if not c.scanned],
            "voids": len(voids),
            "model_ref": analysis.model_ref,
        }

    # ------------------------------------------------------ the entry package
    #
    # Four verbs, and the order between them is the safety argument, exactly as
    # it is for the intake: assess what the record holds, solve a route over
    # that record, write prose about both, and only then let two humans sign it
    # and one human send it. Nothing in the first three does anything -- they
    # produce documents -- and nothing in the last two composes anything.

    #: What the entry-package work is filed under. The interceptor's: it is the
    #: agent that owns the incident loop and reads the whole record. The thermal
    #: readings it prices are `sensor-fusion`'s and are cited as such on every
    #: leg, but the assessment, the solve and the synthesis are this agent's.
    PACKAGE_AGENT: Final[str] = INTERCEPTOR_AGENT_ID

    # -------------------------------------------------- composing unprompted
    #
    # The interceptor had the judgement and no moment to use it: every package
    # in this system existed because somebody pressed a button. What follows is
    # that moment. It is a decision taken at the points where the answer can
    # have changed -- see :mod:`firstdue.incident.autonomy` for why that is
    # events rather than a poll, and for the arithmetic behind the deadline.

    async def _consider_entry_package(
        self,
        incident_id: str,
        *,
        sweep_terminated: bool = False,
        deadline_elapsed: bool = False,
    ) -> EntryPackage | None:
        """Ask whether the record now supports a package, and compose if it does.

        Called from every point an input to readiness changes, and from the
        deadline timer. Cheap when the answer is no: one silent assessment over
        a snapshot already in memory and a string comparison.

        **Every failure here is a shrug**, on exactly the argument
        :meth:`_compose_focus` makes. A package the loop composed for itself is
        an improvement layered on a fleet that is already working; a commander
        can still press the button, both approvals are still human, and nothing
        downstream of this method is waiting on it. An exception escaping into
        the sweep would turn an optional composition into a frame that never
        registered, which is the opposite trade this system makes everywhere.

        **A shrug is not an excuse to know nothing.** That policy is unchanged
        and the record it leaves is not. This used to write one line naming an
        exception class and nothing else, which was enough to prove a failure
        had happened and not enough to say which of the four completely
        different failures it was -- so the same live incident failed three
        times and was diagnosed none of them. The exception is still swallowed
        whole; it is now swallowed *in front of a witness*: the type and its
        message, the trigger the attempt was taken under, and the criteria that
        were outstanding, both to the log and onto
        :class:`~firstdue.incident.autonomy.AutonomyState`, where
        :meth:`describe_autonomy` can report it without re-running anything.
        """
        if not self._container.settings.entry_package_autonomy:
            return None
        state = self._autonomy.get(incident_id)
        if state is None:
            # An incident this process did not open -- a replay, a worker that
            # came up mid-incident. It has no start instant, so it has no
            # deadline, and inventing one from "now" would make the fallback
            # fire two minutes after a restart on an incident that was already
            # an hour old. The console's own button still composes.
            return None
        # Bound before the try so the handler below can report *how far it got*.
        # A failure with no assessment is a probe that never ran; one with an
        # assessment and no trigger cannot happen; one with both is a
        # composition that died, which is the only one of the three that means
        # the loop is broken rather than merely blocked.
        assessment: ReadinessAssessment | None = None
        trigger: AutonomyTrigger | None = None
        try:
            assessment = await self._build_assessment(incident_id)
            # The probe is no longer silent about the one thing it learns.
            #
            # It stays silent about everything else: see
            # :meth:`_record_criteria_movement` for why a criterion that has
            # not moved writes nothing at all.
            await self._record_criteria_movement(incident_id, assessment)
            trigger = decide_autonomy(
                state=state,
                assessment=assessment,
                now=self._container.clock.now(),
                sweep_terminated=sweep_terminated,
                deadline_elapsed=deadline_elapsed,
            )
            if trigger is None:
                return None
            state.attempts += 1
            return await self._compose_unprompted(incident_id, state, assessment, trigger)
        except Exception as exc:
            outstanding = assessment.failed_ids if assessment is not None else ()
            state.record_failure(
                trigger=str(trigger) if trigger is not None else "",
                error_type=type(exc).__name__,
                # The message, not just the class. Every error in this codebase
                # carries stable prose and puts its identifiers in ``details``;
                # none of them carries a document, a narrative or a fact value,
                # which is what makes this safe to write down and what the cap
                # on ``AutonomyState.failed_error_message`` enforces anyway.
                message=str(exc),
                failed_ids=outstanding,
                at=self._container.clock.now(),
            )
            logger.warning(
                "entry_package_not_composed",
                extra={
                    "incident_id": incident_id,
                    "trigger": state.failed_trigger,
                    "error_type": state.failed_error_type,
                    "error_message": state.failed_error_message,
                    # Canonical criterion ids, which are keys and not readings.
                    "failed_criteria": ",".join(outstanding),
                    "probed": assessment is not None,
                    "attempts": state.attempts,
                    "failures": state.failures,
                },
            )
            return None

    async def describe_autonomy(self, incident_id: str) -> AutonomyDiagnostics:
        """What the loop has decided about this incident, and what it has not.

        The read behind ``GET /entry-packages/diagnostics``, and it exists
        because the three triggers can all decline silently and correctly. From
        outside, "autonomy is switched off", "this process never opened this
        incident", "the deadline is still forty seconds away" and "the
        composition raised and was shrugged at" are one observation: no card.
        Distinguishing them by reading logs off a Cloud Run instance is not a
        thing a commander can do at two in the morning, and it is not a thing
        that should have to be done at all -- the loop already knows.

        Silent, like :meth:`_build_assessment` and for the same reason: a
        question about the loop is not a finding, and a diagnostic that appended
        to the incident log would put a running commentary on somebody's
        console every three seconds. The one probe it does make -- the criteria
        outstanding *right now* -- is the silent assessment, and if that raises
        it is reported as an error rather than as an empty list, because an
        empty list of outstanding criteria is a claim that the record is ready.
        """
        settings = self._container.settings
        now = self._container.clock.now()
        state = self._autonomy.get(incident_id)
        timer = self._deadline_timers.get(incident_id)

        outstanding: tuple[str, ...] = ()
        assessment_error = ""
        try:
            outstanding = (await self._build_assessment(incident_id)).failed_ids
        except Exception as exc:
            assessment_error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]

        packages = await self.list_entry_packages(incident_id)
        deadline_at = state.opened_at + COMPOSE_DEADLINE if state is not None else None
        return AutonomyDiagnostics(
            incident_id=incident_id,
            autonomy_enabled=settings.entry_package_autonomy,
            tracked=state is not None,
            opened_at=state.opened_at if state is not None else None,
            age_s=(now - state.opened_at).total_seconds() if state is not None else None,
            deadline_armed=timer is not None and not timer.done(),
            deadline_at=deadline_at,
            deadline_in_s=(deadline_at - now).total_seconds() if deadline_at is not None else None,
            composing=state.composing if state is not None else False,
            attempts=state.attempts if state is not None else 0,
            failures=state.failures if state is not None else 0,
            composed_package_id=state.composed_package_id if state is not None else "",
            composed_trigger=state.composed_trigger if state is not None else "",
            failed_at=state.failed_at if state is not None else None,
            failed_trigger=state.failed_trigger if state is not None else "",
            failed_error_type=state.failed_error_type if state is not None else "",
            failed_error_message=state.failed_error_message if state is not None else "",
            failed_criteria=state.failed_criteria if state is not None else (),
            outstanding_criteria=outstanding,
            assessment_error=assessment_error,
            packages=len(packages),
        )

    async def _compose_unprompted(
        self,
        incident_id: str,
        state: AutonomyState,
        assessment: ReadinessAssessment,
        trigger: AutonomyTrigger,
    ) -> EntryPackage:
        """Record the decision, compose, and remember what it was made against.

        The decision entry goes in *before* the composition, and it names the
        criteria that had not passed. That ordering is the point: a fallback
        composition is a judgement call about time, and an officer reading down
        the log has to meet the reason before the document. A package that
        appeared with no line above it saying why the loop stopped waiting
        would read as one somebody asked for.
        """
        state.composing = True
        try:
            await self.recorder.record_analysis(
                incident_id,
                agent_id=self.PACKAGE_AGENT,
                headline=(
                    "the record supports an entry plan; composing one unprompted"
                    if trigger is AutonomyTrigger.READY
                    else f"composing an entry package unprompted: {trigger}"
                ),
                detail=(
                    (
                        "all six readiness criteria pass and nobody has asked for a package; "
                        "the fleet composes one now so a commander arrives to a staged plan "
                        "rather than a button"
                    )
                    if trigger is AutonomyTrigger.READY
                    else (
                        (
                            "the sweep has stopped, so the record has stopped changing"
                            if trigger is AutonomyTrigger.SWEEP_TERMINATED
                            else "the incident is out of budget and nothing has terminated"
                        )
                        + f"; {len(assessment.failed_ids)} criterion(a) did not pass and travel "
                        "on the package as outstanding. Nothing is filled in, nothing is "
                        "assumed, and both approvals are still a human's"
                    )
                ),
                refs=[str(trigger), *assessment.failed_ids],
            )
            # No storey named here on purpose: the loop composing a package for
            # itself should route to the floor the call reported, exactly as a
            # human asking would. Pinning the ground here was what sent an
            # autonomous package to the lobby of a building whose caller had
            # said the third floor was alight.
            package = await self.run_entry_package(
                incident_id,
                correlation_id=self._container.ids.new_id("corr"),
                trigger=str(trigger),
            )
        finally:
            state.composing = False

        # Recorded off the package's own assessment, not off the probe above.
        # The composition re-assesses, and it is that verdict the document
        # carries -- guarding against the probe would let a package composed on
        # one verdict be remembered as another.
        state.composed_package_id = package.package_id
        state.composed_signature = readiness_signature(package.assessment)
        state.composed_trigger = str(trigger)
        # Nothing left for the deadline to protect against.
        self._cancel_deadline(incident_id)
        return package

    def _arm_deadline(self, incident_id: str) -> None:
        """Schedule the one wake-up that guarantees a package exists.

        Every other trigger depends on the console doing something. This one
        does not, which is the whole reason it exists: a tablet that loses
        signal after the dispatch, a sweep nobody advances, an intake that
        never arrives -- and the fallback still stages a plan with its gaps
        stated. One sleep, one question, then the task is done; it is not a
        poll and it never runs twice.

        The sleep is in real seconds while the *decision* is taken on the
        incident's own clock, and that is deliberate. A demo runs a
        deterministic stepping clock in which 45 s of record time is nine
        hundred clock reads that may never happen, so the timer would never
        fire; scheduling in real time and passing ``deadline_elapsed`` keeps
        the two honest about what each is for.

        Not armed under ``AppEnv.TEST``. A test process must not schedule
        three-quarters of a minute of wall-clock work it will never wait for,
        and the decision this task takes is directly testable through
        :meth:`_consider_entry_package` without it.
        """
        settings = self._container.settings
        if not settings.entry_package_autonomy or settings.app_env is AppEnv.TEST:
            return
        if incident_id in self._deadline_timers:  # pragma: no cover - opened once
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - a script opening outside a loop
            return
        task = loop.create_task(
            self._compose_at_deadline(incident_id),
            name=f"entry-package-deadline:{incident_id}",
        )
        self._deadline_timers[incident_id] = task
        task.add_done_callback(lambda _: self._deadline_timers.pop(incident_id, None))

    def _cancel_deadline(self, incident_id: str) -> None:
        task = self._deadline_timers.pop(incident_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _compose_at_deadline(self, incident_id: str) -> None:
        """Sleep out the budget, then ask the same question every hook asks."""
        try:
            await asyncio.sleep(COMPOSE_DEADLINE.total_seconds())
        except asyncio.CancelledError:  # pragma: no cover - the ordinary end
            return
        await self._consider_entry_package(incident_id, deadline_elapsed=True)

    async def run_entry_package(
        self,
        incident_id: str,
        *,
        target_level: int | None = None,
        correlation_id: str,
        trigger: str = "",
    ) -> EntryPackage:
        """Compose a package through the runtime, under the incident's grant.

        ``target_level`` follows :meth:`solve_entry_path`: omitted, it is the
        storey the call reported, so a package composed for a fire on the third
        floor carries a route that climbs to it. Resolved here rather than at
        the solve so the storey the package was *asked* for is the one recorded
        against the run, whether a human or the loop asked for it.

        A separate run from anything the brief does, and deliberately late: the
        instant brief has been persisted and streamed long before this is asked
        for, so a solve that refuses or a synthesis that is slow costs a package
        and never a brief.

        ``trigger`` is empty when a human asked and one of
        :class:`~firstdue.incident.autonomy.AutonomyTrigger` when the loop asked
        itself. The same run, the same grant, the same run record either way --
        autonomy changes who decided, not what the work is or what may do it.
        """
        grant = await self._require_grant(incident_id)
        storey = self.target_level_for(incident_id) if target_level is None else target_level
        self._pending_packages[correlation_id] = (storey, trigger)
        try:
            run = await self.fleet.run(
                AGENT_ID,
                correlation_id=correlation_id,
                parameters={STAGE_PARAM: STAGE_ENTRY_PACKAGE},
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_packages.pop(correlation_id, None)
        package = self._last_entry_package.pop(incident_id, None)
        if package is None:
            # This was a ``pragma: no cover`` reading "the handler always sets
            # one", and in fake mode it does. Against a real model it does not:
            # a run the runtime cancelled on its deadline leaves this slot empty
            # and the only honest thing to say is which terminal state the run
            # actually reached. The old message said none of that, so three live
            # failures in a row surfaced as one word -- ``NotFoundError`` -- in
            # a log line, and the run record that knew the answer was never
            # read by the thing that reported the problem.
            raise NotFoundError(
                "the composing run ended without staging an entry package",
                details={
                    "id": incident_id,
                    "run_status": str(run.result.status),
                    "run_error_code": run.result.error_code or "",
                    "run_id": run.record.run_id,
                },
            )
        # Deliberately out here, on the far side of the run's six-second cap.
        #
        # These entries describe a document that already exists and is already
        # in the record. Written inside the run they would sit in
        # :data:`PACKAGE_WORK_RESERVE_MS` -- the head-room that exists so the
        # staging after the model call cannot be cancelled half-done -- and that
        # reserve is sized for the five writes already there. Six more would be
        # spending, on narration, the margin that stops a commander watching a
        # two-minute clock run out against an empty screen.
        await self._record_brief_sections(incident_id, package)
        return package

    async def _record_brief_sections(self, incident_id: str, package: EntryPackage) -> None:
        """What went into each part of the crew brief, section by section.

        The brief is assembled from separate readers of separate records -- the
        readiness verdict, the structural facts, the thermal coverage, the
        solved route, the attributes nobody could settle, and the caveats that
        travel with all of it -- and every claim in every section cites the ids
        it rests on. That whole assembly reached the log as one number: "from N
        recorded claim(s)".

        A section with no claims is written down too. A crew brief whose THERMAL
        section is empty is a different document from one whose THERMAL section
        is full, and the empty one is the one worth noticing.

        Swallows its own failure. The package is staged, recorded and on a
        commander's screen by the time this runs; an explanation of it may not
        be the thing that takes it away.
        """
        brief = package.brief
        try:
            for section in SECTION_ORDER:
                claims = brief.section(section)
                refs = [ref for claim in claims for ref in claim.refs]
                await self.recorder.record_analysis(
                    incident_id,
                    agent_id=self.PACKAGE_AGENT,
                    headline=(
                        f"assembled the {section} section from {len(claims)} recorded claim(s)"
                        if claims
                        else f"the {section} section is empty: the record carries nothing for it"
                    ),
                    detail=(
                        f"{len(set(refs))} distinct reference(s) cited; every claim names the "
                        "fact ids, canonical keys or node ids it rests on, and none of them "
                        "carries a value that is not already sourced"
                    ),
                    refs=[section, *dict.fromkeys(refs)][:12],
                )
        except Exception as exc:  # pragma: no cover - defensive, see docstring
            logger.warning(
                "brief_sections_not_recorded",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )

    async def _build_assessment(self, incident_id: str) -> ReadinessAssessment:
        """The six criteria, evaluated and nothing else.

        Split out from :meth:`assess_entry_readiness` because the autonomous
        composer asks this question at every point an input changes -- a frame,
        an intake, a resolution -- and the recording half writes seven log
        entries. Asking seven times per sweep would spend the budget on the
        log and bury the entries an officer is actually reading under a running
        commentary on a verdict that has not moved. So the *probe* is silent
        and the *composition* records, which is also the honest division: an
        assessment nobody acted on is not a finding.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})

        now = self._container.clock.now()
        reported_keys, narratives = await self._intake_history(incident_id)
        return assess_readiness(
            incident_id=incident_id,
            snapshot=snapshot,
            coverage=self.fusion.coverage(incident_id, now=now),
            now=now,
            reported_keys=reported_keys,
            narratives_read=narratives,
            assessed_by=self.PACKAGE_AGENT,
        )

    async def _record_criteria_movement(
        self, incident_id: str, assessment: ReadinessAssessment
    ) -> None:
        """Write down the readiness criteria that *moved*, and nothing else.

        The probe above runs at every point an input to readiness changes -- an
        intake read, each wall the drone flies, an IC settling a conflict, the
        sweep stopping -- and it was entirely silent. That was the right call
        against the alternative it was weighed against, which was writing all
        seven entries every time: on a four-face sweep that is fifty lines
        restating a verdict that has not moved, and it would bury the entries
        an officer is actually reading.

        But the two options were never "seven every time" and "nothing". A
        criterion going from outstanding to met is a real event with a real
        cause -- the wall that was just flown, the narrative that was just read
        -- and it is the one thing this evaluation *learns*. So the first
        evaluation states where the incident starts, and every evaluation after
        it writes only what changed since the last one. An incident where
        nothing moves produces nothing here, which is the correct amount.

        The reason is compared, not just the verdict. ``thermal.coverage``
        fails identically at four faces UNSCANNED and at one, and an officer
        watching a sweep wants the count coming down; that is the criterion
        genuinely reading a different record, not a restatement.

        Swallows its own failure, on the argument
        :meth:`_consider_entry_package` makes for the whole probe: this is a
        record of an optional evaluation, and nothing about the incident may
        turn on it. It is called *inside* that method's try block, so an
        exception escaping here would be filed as a failed composition -- a
        different and much more alarming claim than the one that happened.
        """
        try:
            previous = self._criteria_seen.get(incident_id)
            current = {c.criterion_id: (c.passed, c.reason) for c in assessment.criteria}
            if previous is None:
                # First evaluation. Nothing has "changed", but nothing had been
                # asked yet either, and where the incident starts is a finding:
                # on a cold-start address four of the six fail on the opening
                # snapshot, which is the clearest early statement this loop can
                # make about what the slow loop did and did not leave behind.
                self._criteria_seen[incident_id] = current
                for criterion in assessment.criteria:
                    await self.recorder.record_analysis(
                        incident_id,
                        agent_id=self.PACKAGE_AGENT,
                        headline=(
                            f"readiness {criterion.criterion_id} opens "
                            f"{'met' if criterion.passed else 'NOT met'}"
                        ),
                        detail=criterion.reason,
                        refs=[criterion.criterion_id, *criterion.refs],
                    )
                return

            self._criteria_seen[incident_id] = current
            for criterion in assessment.criteria:
                was = previous.get(criterion.criterion_id)
                if was is None or was == (criterion.passed, criterion.reason):
                    continue
                flipped = was[0] is not criterion.passed
                await self.recorder.record_analysis(
                    incident_id,
                    agent_id=self.PACKAGE_AGENT,
                    headline=(
                        (
                            f"readiness {criterion.criterion_id} is now "
                            f"{'met' if criterion.passed else 'NOT met'}"
                        )
                        if flipped
                        # Still outstanding, but against a different record.
                        # Said differently from a flip on purpose: an officer
                        # must not read a sweep's progress as a criterion having
                        # passed.
                        else f"readiness {criterion.criterion_id} re-read, still "
                        f"{'met' if criterion.passed else 'NOT met'}"
                    ),
                    detail=criterion.reason,
                    refs=[criterion.criterion_id, *criterion.refs],
                )
        except Exception as exc:  # pragma: no cover - defensive, see docstring
            logger.warning(
                "readiness_movement_not_recorded",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )

    async def assess_entry_readiness(self, incident_id: str) -> ReadinessAssessment:
        """Evaluate the six criteria and record each one under its own name.

        Every criterion leaves a line, pass or fail. That is not padding: a
        criterion is a distinct check against distinct data, and the complaint
        this answers is that the loop looked idle while the fleet was working.
        The verdict entry goes last so the per-criterion lines are already in
        the log above it when a console reads down.
        """
        assessment = await self._build_assessment(incident_id)
        cited = await self._criterion_provenance(incident_id)

        for criterion in assessment.criteria:
            citation = cited.get(criterion.criterion_id, ("", ()))
            await self.recorder.record_analysis(
                incident_id,
                agent_id=self.PACKAGE_AGENT,
                headline=(
                    f"readiness {criterion.criterion_id}: "
                    f"{'met' if criterion.passed else 'NOT met'}"
                ),
                # The criterion says what it checked; this says whose work it
                # checked. Four of the six read nothing but slow-loop output,
                # and read down the stream they looked like the interceptor
                # deciding things on its own.
                detail=criterion.reason + (f" -- {citation[0]}" if citation[0] else ""),
                refs=[criterion.criterion_id, *criterion.refs, *citation[1]],
            )
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            # The verdict in the headline, both ways round. A not-ready
            # assessment that read as an ordinary line would be the one outcome
            # this whole evaluation exists to surface, made invisible.
            headline=assessment.summary[:200],
            detail=(
                "every criterion was evaluated against recorded data and cites what it "
                "checked; not ready is a stated outcome and does not stop a commander "
                "sending a package, it stops one being sent with its gaps unstated"
            ),
            refs=[assessment.profile_snapshot_id, *assessment.failed_ids],
        )
        return assessment

    async def _criterion_provenance(
        self, incident_id: str
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Whose slow-loop work each criterion is a check on, where it is recorded.

        Only the four that read the profile appear here. ``thermal.coverage``
        and ``intake.access-bound`` check this incident's own agents, and
        crediting the slow loop for them would be the fabrication this whole
        exercise is guarding against -- so they are simply absent, and the
        caller writes the criterion's own reason unchanged.

        ``snapshot.fresh`` is the interesting absence. It is a check on
        slow-loop output and the output has no author: what it reads is the
        instant the snapshot was taken, which belongs to the read rather than
        to any agent. So it cites the snapshot and names nobody.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:  # pragma: no cover - the assessment already required one
            return {}

        # Exact first, weaker second. A storey that still points at a fact the
        # snapshot carries is a direct derivation and says so; when none does --
        # which is the ordinary case for a spec extruded across an open
        # disagreement -- the claim drops to the attributes the geometry is a
        # function of, and the sentence drops with it.
        storeys = authors_of_geometry(snapshot)
        structural = structural_authors(snapshot)
        hazards = authors_of(snapshot.facts, HAZARD_KEYS)
        rules = rules_behind(snapshot.conflicts)
        return {
            "geometry.present": (
                credit(
                    storeys,
                    work="the storeys were derived from facts filed by",
                    otherwise="",
                )
                or credit(
                    structural,
                    work=(
                        "the storeys trace to facts this snapshot no longer carries; the "
                        "attributes the geometry is a function of were filed by"
                    ),
                    otherwise="the geometry comes from the slow loop and names no author",
                ),
                storeys or structural,
            ),
            "hazard.resolved": (
                credit(
                    hazards,
                    work="these attributes were filed by",
                    otherwise="no hazard fact on this snapshot names its author",
                ),
                hazards,
            ),
            "conflicts.load-bearing": (
                (
                    f"detected by rule {name_agents(rules)}, not by anything on this incident"
                    if rules
                    else "no conflict rule has fired against this address"
                ),
                rules,
            ),
            "snapshot.fresh": (
                "what this checks is the slow loop's own output, and the freshness belongs "
                "to the read rather than to any agent",
                (),
            ),
        }

    async def _intake_history(self, incident_id: str) -> tuple[tuple[str, ...], int]:
        """What the narratives on this incident bound, and how many were read.

        Read off the log rather than off the session, because the log is the
        record and a process that restarted mid-incident still has it. Counts
        and canonical keys only -- the transcript has exactly one home and this
        is not it.
        """
        stored = await self._container.incident_log.get_log(incident_id)
        keys: list[str] = []
        reads = 0
        for entry in stored.entries:
            if entry.entry_type is not LogEntryType.INTAKE_READ:
                continue
            reads += 1
            reported = entry.content.get("reported_keys")
            if isinstance(reported, list):
                keys.extend(str(key) for key in reported)
        return tuple(keys), reads

    def target_level_for(self, incident_id: str) -> int:
        """The storey a route should climb to when nobody has named one.

        The caller's reported floor of origin, if this incident had one, and the
        ground storey otherwise. Counting is the call's and the fire service's:
        the ground storey is the first floor, so a reported third floor is two
        levels above it.

        Nothing is inferred when no floor was reported. A call that never named
        a storey leaves this at the ground because that is the documented
        default, not because a number was read out of a sentence without one.
        """
        reported = self._reported_floor.get(incident_id)
        if reported is None:
            return 0
        return max(0, reported - 1)

    async def solve_entry_path(
        self, incident_id: str, *, target_level: int | None = None
    ) -> EntryPathPlan:
        """Solve the entry and egress route, or record the refusal.

        ``target_level`` is zero-based. Left out, it comes from the floor the
        call reported -- a default, not an override: a commander who names a
        storey outranks a caller who reported one, because the caller is
        reporting and the commander is deciding.

        The solver is pure and lives in :mod:`firstdue.incident.entrypath`; this
        method is the part that reads. Everything it hands over already exists
        with a provenance -- the footprint and the storeys the slow loop
        measured, the coverage `sensor-fusion` registered, the facts and
        conflicts on the profile snapshot -- and nothing is defaulted on the way
        in. A refusal is recorded as loudly as a route, because "the data does
        not support a path" is the finding.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})

        storey = self.target_level_for(incident_id) if target_level is None else target_level

        now = self._container.clock.now()
        resolved = self._container.city.get_address(incident.address_id)
        plan = compute_entry_path(
            incident_id=incident_id,
            spec=snapshot.geometry,
            coverage=self.fusion.coverage(incident_id, now=now),
            voids=self.fusion.voids(incident_id, now=now),
            facts=snapshot.facts,
            conflicts=snapshot.conflicts,
            # Absent when the city cannot place the address. The waypoints then
            # carry footprint-local metres and no coordinates at all, rather
            # than coordinates derived from an origin nobody published.
            origin=(
                GeoOrigin(latitude=resolved.latitude, longitude=resolved.longitude)
                if resolved is not None
                else None
            ),
            target_level=storey,
        )

        # What the solve was priced against, and who filed it. The graph is
        # built from the slow loop's spec and the cost terms are its facts and
        # its disagreements -- so a card that described only the A* was
        # describing the cheapest part of the work. Conflicts are cited by rule
        # rather than by agent because a conflict records a rule and no actor;
        # see :mod:`firstdue.incident.provenance`.
        hazard_authors = authors_of(snapshot.facts, HAZARD_KEYS)
        open_rules = rules_behind(
            [c for c in snapshot.conflicts if c.status is ConflictStatus.OPEN]
        )
        priced_on = credit(
            hazard_authors,
            work="hazard costs from facts filed by",
            otherwise="no hazard fact on this snapshot names its author",
        ) + (
            f"; {len(open_rules)} open disagreement(s) found by {name_agents(open_rules)}"
            if open_rules
            else ""
        )

        if plan.refused:
            await self.recorder.record_analysis(
                incident_id,
                agent_id=self.PACKAGE_AGENT,
                headline="refused to compute an entry path",
                # The refusal names the input it did not have. A cold start
                # refuses here for the same reason the frame does one layer
                # out, and saying so is the point: this agent cannot work
                # unless the slow loop already did.
                detail=(
                    plan.refusal_reason
                    + (
                        "; the graph is built from the footprint and storeys on the profile "
                        "snapshot, which the slow loop supplies"
                        if snapshot.geometry is None
                        else f"; {priced_on}"
                    )
                ),
                refs=[
                    incident.address_id,
                    incident.profile_snapshot_id,
                    *plan.refusal_refs,
                    *hazard_authors,
                ],
            )
            return plan

        entry = plan.entry
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=(
                f"solved the entry path to storey {plan.target_level + 1} "
                f"through {plan.entry_face or 'an unlabelled face'}, "
                "over the geometry the slow loop measured"
            ),
            detail=(
                (
                    f"{plan.algorithm} over {plan.node_count} node(s) and "
                    f"{plan.edge_count} edge(s); "
                    f"{entry.total_distance_m:g} m at weighted cost "
                    f"{entry.total_cost:g} over {len(entry.legs)} leg(s); "
                    if entry is not None
                    else ""
                )
                + (f"{len(plan.barriers)} leg(s) refused as barriers; " if plan.barriers else "")
                + (
                    f"{len(plan.unscanned_faces)} face(s) UNSCANNED and priced as unknown"
                    if plan.unscanned_faces
                    else "every face carried current coverage"
                )
                + ("; no second way out" if plan.egress is None else "; a second way out exists")
                + f"; {priced_on}"
            ),
            refs=[
                *([w.node_id for w in entry.waypoints] if entry is not None else []),
                *plan.unscanned_faces,
                *hazard_authors,
                *open_rules,
            ],
        )
        await self._record_route_pricing(incident_id, plan)
        return plan

    #: How many legs of a solved route are written down individually.
    #:
    #: A route into a low-rise runs to five or six legs -- staging, the
    #: perimeter, the approach, the door, the interior, the stair -- and the
    #: cap is above that so an ordinary solve records all of them. It exists
    #: for the pathological one: a target storey high enough that the stairwell
    #: alone is a dozen legs, where the entries stop being the route and start
    #: being a stair count. The rest are still on the plan, on the package, and
    #: on the printed artifact; what is capped is the per-leg *narration*.
    MAX_PRICED_LEGS: Final[int] = 8

    async def _record_route_pricing(self, incident_id: str, plan: EntryPathPlan) -> None:
        """What the solve priced, leg by leg, and what it refused to build.

        The entry above states the totals: an algorithm, a node count, a
        distance and a weighted cost. That is the answer and not the work. The
        work is in the legs -- each one carries its own cost terms, a
        multiplier, what the search priced it against and rejected, and a
        ``chose_because`` sentence composed from those terms rather than
        authored -- and in the barriers, which are legs the cost model refused
        to build at all and which reached the log only as a count.

        A barrier is the more important of the two. It is a wall the route will
        not use because a measured surface temperature crossed the barrier
        threshold, and "three legs refused as barriers" tells an officer nothing
        about *which* three.

        Nothing here recomputes anything. Every value written is already on the
        plan the solver returned; this is the same solve, stated at the
        resolution it was actually performed at.
        """
        for barrier in plan.barriers[: self.MAX_PRICED_LEGS]:
            await self.recorder.record_analysis(
                incident_id,
                agent_id=self.PACKAGE_AGENT,
                headline=f"refused a leg as a barrier: {barrier.from_id} to {barrier.to_id}",
                detail=barrier.reason,
                refs=[barrier.from_id, barrier.to_id],
            )

        entry = plan.entry
        if entry is not None:
            for index, leg in enumerate(entry.legs[: self.MAX_PRICED_LEGS], start=1):
                await self.recorder.record_analysis(
                    incident_id,
                    agent_id=self.PACKAGE_AGENT,
                    headline=(
                        f"priced leg {index} of {len(entry.legs)}: " f"{leg.from_id} to {leg.to_id}"
                    ),
                    detail=(
                        f"{leg.distance_m:g} m at cost {leg.cost:g} "
                        f"(x{leg.multiplier:g}); {leg.chose_because}"
                    ),
                    refs=[
                        leg.from_id,
                        leg.to_id,
                        *(term.term_id for term in leg.terms),
                        *leg.avoided,
                    ],
                )

        # The second way out, or the search that did not find one. Stated on
        # its own because it is a second solve over the same graph -- one A*
        # per candidate approach face -- and it reached the log as five words
        # at the tail of the entry above.
        egress = plan.egress
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=(
                f"found a second way out over {len(egress.legs)} leg(s)"
                if egress is not None
                else "found no second way out"
            ),
            detail=(
                (
                    f"{egress.total_distance_m:g} m at weighted cost {egress.total_cost:g}; "
                    f"{egress.expanded_nodes} node(s) expanded searching for it"
                )
                if egress is not None
                else plan.egress_note
                or "no candidate face offered a route the cost model would build"
            ),
            refs=[w.node_id for w in egress.waypoints] if egress is not None else [],
        )

    async def compose_entry_package(
        self,
        incident_id: str,
        *,
        target_level: int = 0,
        trigger: str = "",
        deadline: datetime | None = None,
    ) -> EntryPackage:
        """Assess, solve, synthesise, stage two approvals, and record all of it.

        The gateway is asked *here*, before anything is staged, whether this
        grant may write a package into the incident's record at all -- so a
        grant that has expired or lost its scope produces a refusal rather than
        two approval cards for a write that could never happen. What the gateway
        does not decide is that a package needs two human taps: its approval
        table covers writes that commit another agency, and handing a crew an
        entry plan commits this one. That requirement is stated here, and the
        rule the gateway *did* apply is recorded on both cards beside it.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})

        decision = await self._package_decision(incident_id, incident.address_id, approval_id=None)
        if decision.action is PolicyAction.DENY:
            raise NotAuthorizedError(
                "this incident's grant does not permit writing an entry package",
                details={"incident_id": incident_id, "rule_id": decision.rule_id},
            )

        assessment = await self.assess_entry_readiness(incident_id)
        plan = await self.solve_entry_path(incident_id, target_level=target_level)

        now = self._container.clock.now()
        brief = await compose_crew_brief(
            brief_id=self._container.ids.new_id("crewbrief"),
            incident_id=incident_id,
            snapshot=snapshot,
            coverage=self.fusion.coverage(incident_id, now=now),
            assessment=assessment,
            plan=plan,
            now=now,
            composed_by=self.PACKAGE_AGENT,
            model=self._container.model,
            # Computed here rather than at the top of the method: the assessment
            # and the solve above have already been spent, and what the wording
            # may have is what is *left* minus the staging that follows it. See
            # ``_brief_deadline_ms`` and ``PACKAGE_WORK_RESERVE_MS``.
            deadline_ms=_brief_deadline_ms(deadline, now),
        )
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=f"synthesised the crew brief from {len(brief.claims)} recorded claim(s)",
            detail=(
                (
                    "wording composed by the model and accepted: every number in it appears "
                    "in the claims it was composed from"
                    if brief.prose_source == "model"
                    else (
                        f"the composed wording was refused ({brief.prose_rejection}); the "
                        "deterministic rendering stands"
                        if brief.prose_rejection
                        else "no narrative model is wired, so the deterministic rendering stands"
                    )
                )
                + f"; {len(brief.unknowns)} attribute(s) stated as unknown"
            ),
            refs=[brief.brief_id, *brief.claim_refs[:10]],
        )
        package_id = self._container.ids.new_id("pkg")
        package = EntryPackage(
            package_id=package_id,
            incident_id=incident_id,
            address_id=incident.address_id,
            created_at=now,
            created_by=self.PACKAGE_AGENT,
            assessment=assessment,
            path=plan,
            brief=brief,
            path_approval_id=approval_id_for(package_id, PATH_HALF),
            brief_approval_id=approval_id_for(package_id, BRIEF_HALF),
        )

        await self._stage_half(
            package,
            half=PATH_HALF,
            approval_id=package.path_approval_id,
            # A route puts a crew inside a building. The higher of the two
            # thresholds unless the gateway named one itself.
            threshold=decision.approval_threshold or ApprovalThreshold.CHIEF,
            rule_id=decision.rule_id,
            summary=(
                f"Release the computed entry path for {incident.address_id} to the crew: "
                + (
                    f"refused -- {plan.refusal_reason}"
                    if plan.refused or plan.entry is None
                    else (
                        f"{plan.entry.total_distance_m:g} m through "
                        f"{plan.entry_face or 'an unlabelled face'} to storey "
                        f"{plan.target_level + 1}, weighted cost {plan.entry.total_cost:g}"
                    )
                )
            ),
        )
        await self._stage_half(
            package,
            half=BRIEF_HALF,
            approval_id=package.brief_approval_id,
            threshold=decision.approval_threshold or ApprovalThreshold.SUPERVISOR,
            rule_id=decision.rule_id,
            summary=(
                f"Release the synthesised crew brief for {incident.address_id}: "
                f"{len(brief.claims)} cited claim(s), {len(brief.unknowns)} attribute(s) "
                f"stated unknown, {assessment.summary}"
            ),
        )

        await self._record_package(
            package,
            note=(
                "staged for two human approvals"
                if not trigger
                else f"composed unprompted ({trigger}) and staged for two human approvals"
            ),
            trigger=trigger,
        )
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=f"staged entry package {package_id} for two approvals",
            detail=(
                "the path and the brief are signed separately; the package is not sent to "
                "anybody until both are granted, and the readiness verdict travels on it "
                "whichever way it went"
            ),
            refs=[package_id, package.path_approval_id, package.brief_approval_id],
        )
        logger.info(
            "entry_package_staged",
            extra={
                "incident_id": incident_id,
                "package_id": package_id,
                "ready": assessment.ready,
                "path_refused": plan.refused,
                "prose_source": brief.prose_source,
                "trigger": trigger,
            },
        )
        return package

    async def _package_decision(
        self, incident_id: str, address_id: str, *, approval_id: str | None
    ) -> Any:
        """Ask the gateway whether this grant may write a package, and record it.

        ``WRITE_RMS`` because that is what a package *is*: a document written
        into the incident's own record and flushed to the records system with
        every other entry. It is not a notification and it commits no other
        agency, which is why the gateway's approval table has nothing to say
        about it and why the two-tap requirement is stated in this module rather
        than borrowed from a rule that did not fire.
        """
        grant = await self._require_grant(incident_id)
        decision = self._container.policy.decide(
            AccessRequest(
                agent_id=self.PACKAGE_AGENT,
                agent_version=FLEET_VERSION,
                grant=grant,
                target="crew-entry-package",
                operation=Operation.WRITE,
                # The department's own record about one building. Not public,
                # and not person-level: no PHI reaches a package.
                classification=Classification.RESTRICTED,
                scope=Scope.WRITE_RMS,
                now=self._container.clock.now(),
                incident_id=incident_id,
                address_id=address_id,
                responding_agency_id=grant.responding_agency_id,
                approval_id=approval_id,
            )
        )
        await self.recorder.record_decision(decision)
        return decision

    async def _stage_half(
        self,
        package: EntryPackage,
        *,
        half: str,
        approval_id: str,
        threshold: ApprovalThreshold,
        rule_id: str,
        summary: str,
    ) -> ApprovalRequest:
        """One approval card, through the repository every other card uses.

        Idempotent on the id, like ``ResourceAgent._stage``: re-composing a
        package that already staged this half returns the stored record rather
        than resetting a decision somebody already made.
        """
        existing = await self._container.approvals.get(approval_id)
        if existing is not None:  # pragma: no cover - package ids are minted fresh
            return existing
        return await self._container.approvals.stage(
            ApprovalRequest(
                approval_id=approval_id,
                action_id=f"act_{package.package_id}_{half}",
                incident_id=package.incident_id,
                address_id=package.address_id,
                threshold=threshold,
                receiving_department=Department.FIRE,
                prefilled_summary=summary[:500],
                rule_id=rule_id,
                status=ApprovalStatus.STAGED,
                staged_at=package.created_at,
            )
        )

    async def _record_package(self, package: EntryPackage, *, note: str, trigger: str = "") -> None:
        await self.recorder.record_entry_package(
            package_content(package, note=note, trigger=trigger),
            incident_id=package.incident_id,
            agent_id=package.created_by,
            agent_version=package.created_by_version,
        )

    async def approve_package_half(
        self, incident_id: str, package_id: str, *, half: str, decided_by: str
    ) -> EntryPackage:
        """Grant one half. Two of these, by a human each, before anything is sent."""
        if half not in (PATH_HALF, BRIEF_HALF):
            raise ValidationError(
                "an entry package has exactly two halves",
                details={"half": half[:40], "expected": f"{PATH_HALF} or {BRIEF_HALF}"},
            )
        package = await get_package(self._container.incident_log, incident_id, package_id)
        if package is None:
            raise NotFoundError("entry package not found", details={"package_id": package_id})

        approval_id = package.path_approval_id if half == PATH_HALF else package.brief_approval_id
        approval = await self._container.approvals.get(approval_id)
        if approval is None:  # pragma: no cover - composing always stages both
            raise NotFoundError("approval not found", details={"approval_id": approval_id})

        now = self._container.clock.now()
        await self._container.approvals.save(
            approval.model_copy(
                update={
                    "status": ApprovalStatus.GRANTED,
                    "decided_at": now,
                    "decided_by": decided_by,
                }
            )
        )
        await self.recorder.record_approval(
            incident_id,
            approval_id=approval_id,
            decided_by=decided_by,
            threshold=str(approval.threshold),
        )

        updated = package.with_approval(half, decided_by=decided_by, at=now)
        await self._record_package(updated, note=f"{half} approved")
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=f"{half} approved by {decided_by}",
            detail=(
                "both halves are granted; the package may be sent"
                if updated.status is PackageStatus.READY_TO_SEND
                else "still outstanding: " + ", ".join(updated.outstanding_halves)
            ),
            refs=[package_id, approval_id, *updated.outstanding_halves],
        )
        return updated

    async def dispatch_package(
        self, incident_id: str, package_id: str, *, sent_by: str
    ) -> EntryPackage:
        """Mark the package sent to the crew. Refuses unless both halves signed.

        The only method that sets ``sent_at``, so there is one place to look for
        "was this handed to anybody". The gateway is asked again with the path
        approval on the request, which is what turns two stored decisions into
        one recorded permission rather than a check this method remembered.
        """
        package = await get_package(self._container.incident_log, incident_id, package_id)
        if package is None:
            raise NotFoundError("entry package not found", details={"package_id": package_id})
        if package.status is PackageStatus.SENT:
            return package
        if package.outstanding_halves:
            raise ValidationError(
                "a package is sent only once both halves are approved",
                details={
                    "package_id": package_id,
                    "outstanding": ", ".join(package.outstanding_halves),
                },
            )

        decision = await self._package_decision(
            incident_id, package.address_id, approval_id=package.path_approval_id
        )
        if decision.action is not PolicyAction.ALLOW:
            raise NotAuthorizedError(
                "the gateway did not permit sending this package",
                details={"package_id": package_id, "rule_id": decision.rule_id},
            )

        sent = package.sent(
            by=sent_by, at=self._container.clock.now(), decision_id=decision.decision_id
        )
        await self._record_package(sent, note="sent to the crew")
        await self.recorder.record_analysis(
            incident_id,
            agent_id=self.PACKAGE_AGENT,
            headline=f"sent entry package {package_id} to the crew",
            detail=(
                f"{decision.action} under policy rule {decision.rule_id}; both halves were "
                f"approved, and the package carries its own readiness verdict: "
                f"{sent.assessment.summary}"
            ),
            refs=[
                package_id,
                decision.decision_id,
                decision.rule_id,
                sent.path_approval_id,
                sent.brief_approval_id,
            ],
        )
        return sent

    async def list_entry_packages(self, incident_id: str) -> tuple[EntryPackage, ...]:
        """Every package this incident produced, latest state of each."""
        return await list_packages(self._container.incident_log, incident_id)

    async def get_entry_package(self, incident_id: str, package_id: str) -> EntryPackage:
        package = await get_package(self._container.incident_log, incident_id, package_id)
        if package is None:
            raise NotFoundError("entry package not found", details={"package_id": package_id})
        return package

    # ----------------------------------------------------------- resources

    async def request_resource(
        self, incident_id: str, *, kind_id: str, detail: str, approval_id: str | None
    ) -> ResourceOutcome:
        grant = await self._require_grant(incident_id)
        incident = await self._require_incident(incident_id)
        outcome = await self.resources.request(
            kind_id,
            grant=grant,
            incident_id=incident_id,
            address_id=incident.address_id,
            detail=detail,
            approval_id=approval_id,
        )
        # Delivery rate, counted at the point of truth: a request the gateway
        # allowed either reached its target or did not. A commitment awaiting
        # approval is neither -- it was never sent, so it is not a delivery
        # failure and is not counted as one.
        if outcome.action is not PolicyAction.REQUIRE_APPROVAL:
            METRICS.record_notification(delivered=bool(outcome.sent and outcome.external_ref))

        # The gateway's answer, in the notifier's own name.
        #
        # Only a *sent* notification was recorded, so the two outcomes that are
        # not a send left nothing in the log at all: a commitment staged on a
        # chief's card and waiting, and a request the gateway refused. Both are
        # this agent working, and the second is the governance model doing the
        # one thing it exists to do -- an approval gate whose firing is
        # invisible is indistinguishable from an agent that never asked.
        #
        # It is a separate entry from the notification rather than a field on
        # it, because they answer different questions: which rule permitted this
        # and what it permitted, against what actually reached a partner.
        if outcome.action is PolicyAction.ALLOW:
            headline = f"gateway cleared the {kind_id} request"
            settlement = (
                "sent autonomously; the agency stays free to do nothing"
                if outcome.sent
                else "cleared, but the target returned no reference"
            )
        elif outcome.action is PolicyAction.REQUIRE_APPROVAL:
            headline = f"{kind_id} is staged and waiting on a chief"
            settlement = "this request spends another agency's resources, so nobody has been told"
        else:
            headline = f"gateway refused the {kind_id} request"
            settlement = "nothing was sent and nothing was staged"
        await self.recorder.record_analysis(
            incident_id,
            agent_id="agency-notifier",
            headline=headline,
            detail=f"{outcome.action} under policy rule {outcome.rule_id}; {settlement}"
            + ("; replayed an identical request already made" if outcome.replayed else ""),
            refs=[
                r
                for r in (
                    kind_id,
                    outcome.rule_id,
                    outcome.decision_id,
                    outcome.external_ref,
                    outcome.approval_id,
                )
                if r
            ],
        )

        if outcome.sent and outcome.external_ref:
            await self.recorder.record_notification(
                incident_id,
                target=kind_id,
                external_ref=outcome.external_ref,
                autonomous=approval_id is None,
            )
        return outcome

    async def approve(
        self, incident_id: str, approval_id: str, *, decided_by: str
    ) -> dict[str, Any]:
        """Grant a staged approval and execute the request it was holding."""
        from firstdue.domain.work import ApprovalStatus

        approval = await self._container.approvals.get(approval_id)
        if approval is None:
            raise NotFoundError("approval not found", details={"approval_id": approval_id})
        now = self._container.clock.now()
        granted = await self._container.approvals.save(
            approval.model_copy(
                update={
                    "status": ApprovalStatus.GRANTED,
                    "decided_at": now,
                    "decided_by": decided_by,
                }
            )
        )
        await self.recorder.record_approval(
            incident_id,
            approval_id=approval_id,
            decided_by=decided_by,
            threshold=str(approval.threshold),
        )

        kind_id = approval_id.rsplit("_", 1)[-1]
        outcome = await self.request_resource(
            incident_id, kind_id=kind_id, detail="", approval_id=approval_id
        )
        return {
            "approval_id": granted.approval_id,
            "decided_by": decided_by,
            "executed": outcome.sent,
            "external_ref": outcome.external_ref,
            "action": str(outcome.action),
        }

    # ------------------------------------------------------------ internals

    async def _require_incident(self, incident_id: str) -> Any:
        incident = await self._container.incidents.get(incident_id)
        if incident is None:
            raise NotFoundError("incident not found", details={"incident_id": incident_id})
        return incident

    async def grant_for(self, incident_id: str) -> IncidentGrant:
        """This incident's grant, reloaded from the store if the process lost it.

        Public because waking a routed agent needs it: every incident-loop run
        happens under the incident's own bounded authority, never a standing one.
        """
        return await self._require_grant(incident_id)

    async def _require_grant(self, incident_id: str) -> IncidentGrant:
        grant = self._grants.get(incident_id)
        if grant is not None:
            return grant
        incident = await self._require_incident(incident_id)
        stored = await self._container.grants.get_incident_grant(incident.grant_id)
        if stored is None:
            raise NotFoundError("incident grant not found", details={"incident_id": incident_id})
        self._grants[incident_id] = stored
        return stored


class _FleetWaker:
    """Starts a routed agent the only way any agent runs: through the fleet.

    The routing decision and the act of running are separated on purpose. The
    decision is a pure function of the intake signals and the catalog, testable
    without a runtime; this is the part that needs a grant, a deadline, and a
    durable run record, and it gets all three by going through
    :class:`~firstdue.agents.fleet.FleetRunner` rather than calling a handler.

    What the woken agent is handed does **not** travel inside the
    ``AgentInput``: the envelope carries the incident id and nothing else, and
    the reported items sit on the session where the agent can read them. A 911
    transcript inside an event envelope would be record content on the bus.

    In-process, like every other incident handoff in this session. A deployment
    that gives each agent its own Cloud Run service reaches them over the bus
    instead, and ``FleetRunner`` refuses an agent this worker does not serve --
    which :meth:`IncidentInterceptor.wake_all` treats as a wake that did not
    start rather than as a failure of the incident. The plan is still the
    record of what was decided.
    """

    def __init__(self, session: IncidentSession) -> None:
        self._session = session

    async def wake(self, handoff: Handoff, *, incident_id: str, correlation_id: str) -> str | None:
        """Start one routed agent, and announce the wake so the other topology sees it.

        Both, not either. In a single process the run below *is* the wake, and
        publishing as well costs one envelope. Across eleven Cloud Run services
        the agent lives somewhere else entirely: ``FleetRunner`` refuses one this
        worker does not serve, and the announcement is the only thing that
        reaches it.

        The announcement is what closes the gap this method used to leave. An
        agent that subscribed to ``incident.opened`` was started by Pub/Sub
        whatever the plan decided -- so a handoff the plan *withheld* because
        the incident grant lacked the scope ran anyway in the deployed topology,
        while the plan sat in the log recording a refusal that never happened.
        Routed agents now listen on ``agent.wake`` instead, which only this
        emits, and only for a handoff the plan produced.

        The envelope carries ids and nothing else, like every other one: which
        agent, which incident. What that agent should *look at* is the focus,
        already written to the incident log, and what it may do is its grant.
        """
        await self._announce(handoff, incident_id=incident_id, correlation_id=correlation_id)
        grant = await self._session.grant_for(incident_id)
        run = await self._session.fleet.run(
            handoff.agent_id,
            correlation_id=f"{correlation_id}:{handoff.agent_id}"[:120],
            causation_id=correlation_id,
            ids={"incident_id": incident_id},
            grant=grant,
        )
        return run.record.run_id

    async def _announce(self, handoff: Handoff, *, incident_id: str, correlation_id: str) -> None:
        """Publish the wake. Never raises: a bus outage must not lose the run.

        The in-process run below is the authoritative one when this worker
        serves the agent. If the announcement fails and the agent lives
        elsewhere, the wake is lost -- which is what the dead-letter topic and
        the plan in the incident log are for, and it is strictly better than an
        exception here taking down an incident that is otherwise proceeding.
        """
        # The bus is always present on the container -- in-memory or Pub/Sub,
        # never absent -- so there is nothing to guard, only to fail softly.
        container = self._session._container
        try:
            await container.bus.publish(
                EventEnvelope(
                    event_id=container.ids.new_id("evt"),
                    topic=Topic.AGENT_WAKE,
                    occurred_at=container.clock.now(),
                    producer=INTERCEPTOR_AGENT_ID,
                    producer_version=FLEET_VERSION,
                    correlation_id=correlation_id,
                    ids={"incident_id": incident_id, "agent_id": handoff.agent_id},
                    idempotency_key=container.ids.idempotency_key(
                        "agent.wake", f"{incident_id}:{handoff.agent_id}"
                    ),
                )
            )
        except Exception as exc:
            logger.warning(
                "agent_wake_announce_failed",
                extra={
                    "incident_id": incident_id,
                    "agent_id": handoff.agent_id,
                    "error_type": type(exc).__name__,
                },
            )


class SessionRegistry:
    """One session per process. Held on the app, not in a module global."""

    def __init__(self) -> None:
        self._session: IncidentSession | None = None

    def for_container(self, container: Container) -> IncidentSession:
        if self._session is None:
            self._session = IncidentSession(container)
        return self._session

    def forget(self, incident_id: str) -> None:
        if self._session is not None:
            self._session.forget(incident_id)


_REGISTRIES: dict[int, SessionRegistry] = {}


def sessions(container: Container) -> SessionRegistry:
    """The registry for this container. Keyed by identity, not by a global."""
    return _REGISTRIES.setdefault(id(container), SessionRegistry())


def get_session(container: Container) -> IncidentSession:
    return sessions(container).for_container(container)
