"""The slow-loop demo, end to end, with no credentials.

One command, one district, and the sequence the whole product is built around:

1. The permit says the building at 450 Hayes has **two** storeys.
2. The lidar DSM measures 9.5 m, which is **three**.
3. Both facts are stored. Neither is corrected, averaged, or dropped.
4. The deterministic conflict engine records the disagreement.
5. Structure Watch puts that building at the top of the district's survey queue,
   citing the conflict as the reason.
6. A work order, a calendar hold, a crew notification, and an NFPA 1620
   pre-incident plan are created autonomously.
7. A referral to the building department is **staged and waits** -- accusing a
   property owner is a captain's decision, not an agent's.
8. On approval, exactly one case number comes back and lands on the profile.

Running it twice changes nothing. Facts, conflicts, queue rows, and external
writes all key on derived identifiers, so the second run re-derives what exists
and writes none of it again.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.actions import ActionFlow, ApprovalResult, DispatchResult
from firstdue.agents.fleet import FleetRun, FleetRunner, outcome
from firstdue.agents.geometry_watcher import GeometryWatcher, GeometryWatchResult
from firstdue.agents.hazard_watcher import HazardWatcher, HazardWatchResult
from firstdue.agents.records_watcher import RecordsWatcher, WatchResult
from firstdue.agents.structure_watch import StructureWatch, StructureWatchResult
from firstdue.container import Container
from firstdue.domain.enums import AgentRunStatus
from firstdue.errors import ConfigurationError
from firstdue.extraction.extractor import FactExtractor
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind
from firstdue.ports.runtime import AgentHandler, AgentInput, AgentOutcome
from firstdue.ports.sources import SourceAdapter
from firstdue.services.grants import GrantService
from firstdue.services.materialization import ProfileMaterializer

logger = get_logger(__name__)

#: The address the whole demo turns on: permit two, measurement three.
DISPUTED_ADDRESS_ID: Final[str] = "sf-0450-hayes"
DEFAULT_COMPANY: Final[str] = "E-05"
DEFAULT_CREW_EMAIL: Final[str] = "e05-crew@sffd.example"
APPROVER: Final[str] = "capt-alvarez"


class AgentRunSummary(BaseModel):
    """One agent run, as the console and the CLI render it.

    Present so the demo can *show* that the fleet ran through its runtime --
    the version that ran, the terminal state it reached, and how long it took
    against the budget its descriptor declares.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    version: str
    status: str
    duration_ms: float = Field(ge=0.0)
    facts_written: int = Field(default=0, ge=0)


class _Pass:
    """One slow-loop pass, addressable by the correlation id that started it.

    The runtime handlers below are module-level and stateless: they are
    registered once per process and take everything they need from the
    ``AgentInput`` they are called with. That is the shape the payload contract
    asks for -- identifiers in, work done, identifiers out -- and it is what
    lets one registration serve every pass the process ever runs.

    A handler still has to leave its own typed result somewhere for the report,
    and this is that somewhere. It lives exactly as long as the pass does.
    """

    __slots__ = (
        "actions",
        "company",
        "crew_email",
        "dispatch",
        "district",
        "geometry",
        "geometry_agent",
        "hazard",
        "hazard_agent",
        "queue",
        "structure_watch",
        "records",
        "sources",
        "watch",
    )

    def __init__(
        self,
        *,
        district: str,
        records: RecordsWatcher,
        geometry_agent: GeometryWatcher,
        hazard_agent: HazardWatcher,
        structure_watch: StructureWatch,
        actions: ActionFlow,
        sources: list[SourceAdapter],
        company: str,
        crew_email: str,
    ) -> None:
        self.district = district
        self.records = records
        self.geometry_agent = geometry_agent
        self.hazard_agent = hazard_agent
        self.structure_watch = structure_watch
        self.actions = actions
        self.sources = sources
        self.company = company
        self.crew_email = crew_email
        self.watch: WatchResult | None = None
        self.geometry: GeometryWatchResult | None = None
        self.hazard: HazardWatchResult | None = None
        self.queue: StructureWatchResult | None = None
        self.dispatch: DispatchResult | None = None


#: Passes in flight, keyed by the correlation id that opened them. A handler
#: finds its pass here; nothing else reads it, and a finished pass is removed.
_PASSES: Final[dict[str, _Pass]] = {}


def _pass_for(payload: AgentInput) -> _Pass:
    current = _PASSES.get(payload.correlation_id)
    if current is None:  # pragma: no cover - a handler cannot run without one
        raise ConfigurationError(
            "no slow-loop pass is open for this correlation id",
            details={"correlation_id": payload.correlation_id},
        )
    return current


async def _run_records(payload: AgentInput, _grant: object) -> AgentOutcome:
    current = _pass_for(payload)
    current.watch = await current.records.poll(
        district_id=current.district,
        sources=current.sources,
        correlation_id=payload.correlation_id,
        # The runtime's deadline, not one the agent guessed. See `AgentInput`.
        deadline=payload.deadline,
    )
    return outcome(facts=current.watch.written_fact_ids)


async def _run_geometry(payload: AgentInput, _grant: object) -> AgentOutcome:
    current = _pass_for(payload)
    current.geometry = await current.geometry_agent.poll(
        district_id=current.district,
        sources=current.sources,
        correlation_id=payload.correlation_id,
        # The runtime's deadline, the same one `_run_records` passes. Without
        # it this agent measured until the runtime killed it and committed
        # nothing -- 0 of 385 live profiles had geometry for that reason alone.
        deadline=payload.deadline,
    )
    return outcome(facts=current.geometry.written_fact_ids)


async def _run_hazards(payload: AgentInput, _grant: object) -> AgentOutcome:
    current = _pass_for(payload)
    current.hazard = await current.hazard_agent.poll(
        district_id=current.district,
        sources=current.sources,
        correlation_id=payload.correlation_id,
        # The runtime's deadline, as its three siblings above and below already
        # pass. This was the one handler that did not, and the omission is not
        # cosmetic: with no deadline the cross-check graph's `graph_budget`
        # falls back to the agent's *whole* 180-second descriptor budget --
        # which is the same number `ADKRuntime` is counting down from outside,
        # with nothing reserved for the apply loop or for `_record_pass`. So a
        # graph that used its budget legally was cancelled before this agent
        # wrote a single audit event, and the console, whose only evidence is
        # that log, drew `hazard-watcher` idle through a pass it had run in
        # full. Fixtures answer in microseconds and fake mode wires no memory
        # bank, so the graph never runs there and fake mode never saw it.
        deadline=payload.deadline,
    )
    return outcome(facts=current.hazard.written_fact_ids)


async def _run_structure_watch(payload: AgentInput, _grant: object) -> AgentOutcome:
    """Detect and rank in one pass, from one reading of the district.

    The conflicts this reports and the queue it produces come out of the same
    ``list_by_district`` call, which is the whole point of the merge: a row that
    cites a severity-4 conflict was scored on a corpus that contained it.
    """
    current = _pass_for(payload)
    current.queue = await current.structure_watch.watch(
        current.district,
        correlation_id=payload.correlation_id,
        # The runtime's deadline. Detection is a profile read, a rule sweep and
        # a versioned write per structure, which over a real district is
        # minutes against a 60-second budget -- so without this the agent was
        # cancelled mid-district, `watch` never returned, and two things
        # followed. It recorded no pass; and `current.queue` stayed `None`, so
        # the caller below substituted an empty queue and never dispatched
        # `referral-clerk` at all. One missing argument drew two agents idle.
        deadline=payload.deadline,
    )
    return outcome(events=current.queue.published_event_ids)


async def _run_referral_clerk(payload: AgentInput, _grant: object) -> AgentOutcome:
    """Stage the work order, the calendar hold, the crew mail, and the referral.

    The referral is staged and stays staged. Approving it is a captain's act,
    and it happens outside this run.
    """
    current = _pass_for(payload)
    queue = current.queue
    if queue is None or not queue.entries:  # pragma: no cover - guarded by caller
        return outcome()
    current.dispatch = await current.actions.dispatch(
        queue.entries[0],
        company=current.company,
        crew_email=current.crew_email,
        correlation_id=payload.correlation_id,
    )
    return outcome(writes=current.dispatch.external_refs)


#: Every slow-loop agent, and the work it does. Registered once per process.
SLOW_LOOP_HANDLERS: Final[dict[str, AgentHandler]] = {
    "records-watcher": _run_records,
    "geometry-watcher": _run_geometry,
    "hazard-watcher": _run_hazards,
    "structure-watch": _run_structure_watch,
    "referral-clerk": _run_referral_clerk,
}


async def _record_unfinished(container: Container, run: FleetRun, *, district_id: str) -> None:
    """Say that an agent ran, when the agent itself did not get to say it.

    Every slow-loop agent closes its own pass with an ``AGENT_PASS``, and that
    is the record the console reads. But the runtimes enforce the deadline from
    *outside*, by cancelling the coroutine -- so the one pass that most needs a
    line in the log, the one that ran out of budget, is exactly the pass whose
    own closing line never executes. The guards inside each agent are what keep
    a pass off that path; this is what keeps the console honest about the ones
    that still land on it, along with the denied and the failed.

    Nothing is invented here. The status, the error code and the version are
    read off the run record the fleet runner already wrote and stored; this
    republishes them where the console looks. Only for a run that did *not*
    complete, because a completed run has already written its own.
    """
    if run.result.status is AgentRunStatus.COMPLETED:
        return
    detail = {"status": str(run.result.status), "run_id": run.record.run_id}
    if run.result.error_code:
        detail["error_code"] = run.result.error_code
    try:
        await container.audit.record_event(
            AuditEvent(
                audit_id=container.ids.new_id("audit"),
                kind=AuditEventKind.AGENT_PASS,
                occurred_at=container.clock.now(),
                actor=run.agent_id,
                actor_version=run.version,
                target=district_id,
                correlation_id=run.record.correlation_id,
                detail=detail,
            )
        )
    except Exception:
        # Logged rather than raised, and this is the one place in this file
        # where that is right: the run it describes is already durable in the
        # run repository, so nothing is lost that cannot be read back -- while
        # letting a diagnostic append end the pass would mean a report about an
        # agent that failed taking the four that did not down with it.
        logger.warning(
            "unfinished_run_not_recorded",
            extra={"agent_id": run.agent_id, "status": str(run.result.status)},
        )


def build_fleet_runner(container: Container) -> FleetRunner:
    """The runner every catalogued agent goes through."""
    return FleetRunner(
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


class SlowLoopReport(BaseModel):
    """Everything one demo pass did. Rendered by the CLI verbatim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    ran_at: datetime
    #: The id every agent in this pass ran under, and every `AGENT_PASS` and
    #: `AGENT_STEP` it wrote carries.
    #:
    #: Here because the report was the one thing a caller got back from a pass
    #: that could not say *which* pass it was. A console scoping a counter to
    #: the pass in flight has to read that id out of the audit log instead --
    #: newest pass-or-step event wins -- which is right for the passes a
    #: scheduler drives and unnecessarily indirect for the one the console
    #: triggered itself. Naming it closes the second case.
    correlation_id: str = ""

    facts_written: int = Field(default=0, ge=0)
    facts_deduped: int = Field(default=0, ge=0)
    conflicts: tuple[str, ...] = ()
    unavailable_sources: tuple[str, ...] = ()
    screen_findings: tuple[str, ...] = ()

    queue_size: int = Field(default=0, ge=0)
    top_address_id: str | None = None
    top_score: float = 0.0
    top_reasons: tuple[str, ...] = ()

    dispatch: DispatchResult | None = None
    approval: ApprovalResult | None = None
    #: Every agent run this pass made, in order, with its terminal state.
    agent_runs: tuple[AgentRunSummary, ...] = ()

    @property
    def produced_the_conflict(self) -> bool:
        return bool(self.conflicts)


def build_agents(
    container: Container,
) -> tuple[RecordsWatcher, GeometryWatcher, HazardWatcher, StructureWatch, ActionFlow]:
    """Wire the slow-loop fleet from a container. One place, so the demo, the
    API, and the tests all run the same agents."""
    materializer = ProfileMaterializer(
        profiles=container.profiles,
        conflicts=container.conflicts,
        locks=container.locks,
        clock=container.clock,
        ids=container.ids,
        bus=container.bus,
    )
    # The configured screen, so a live process actually reaches Model Armor.
    extractor = FactExtractor(ids=container.ids, model=container.model, screen=container.screen)

    records = RecordsWatcher(
        profiles=container.profiles,
        facts=container.facts,
        city=container.city,
        extractor=extractor,
        materializer=materializer,
        clock=container.clock,
        ids=container.ids,
        audit=container.audit,
        vectors=container.vectors,
        # Reasoning is opt-in per process, and it is these two arguments that
        # switch it on. Without a bank there is nowhere to park a question, so
        # the graph does not run and the fixed pass is what executes -- which
        # is exactly what fake mode and the whole test suite exercise today.
        memory=container.memory,
        grounding=container.grounding,
        use_langgraph=container.settings.langgraph_enabled,
        max_graph_steps=container.settings.agent_graph_max_steps,
    )
    geometry = GeometryWatcher(
        profiles=container.profiles,
        facts=container.facts,
        materializer=materializer,
        clock=container.clock,
        # So the pass leaves a trace. Without a sink this agent measured
        # buildings and wrote nothing to the audit log, and the console -- whose
        # only evidence is what an agent recorded -- drew it as idle.
        audit=container.audit,
        ids=container.ids,
    )
    hazards = HazardWatcher(
        profiles=container.profiles,
        facts=container.facts,
        materializer=materializer,
        clock=container.clock,
        memory=container.memory,
        grounding=container.grounding,
        use_langgraph=container.settings.langgraph_enabled,
        max_graph_steps=container.settings.agent_graph_max_steps,
        # Which registries answered and which refused, in this agent's own
        # name. Without it a pass that read nothing because three registries
        # were down looked exactly like a pass that found no hazards.
        audit=container.audit,
        ids=container.ids,
    )
    structure_watch = StructureWatch(
        profiles=container.profiles,
        conflicts=container.conflicts,
        queue=container.queue,
        clock=container.clock,
        ids=container.ids,
        bus=container.bus,
        # The detection and the ranking, in this agent's own name. Its work
        # landed on profiles, in the conflict log, in the queue and on the bus,
        # and the console reads none of those -- so the one agent that decides
        # which building a company is sent to next was the one drawn idle.
        audit=container.audit,
    )
    actions = ActionFlow(
        profiles=container.profiles,
        conflicts=container.conflicts,
        queue=container.queue,
        referrals=container.referrals,
        approvals=container.approvals,
        write_actions=container.write_actions,
        compensations=container.compensations,
        write_targets=container.write_targets,
        calendar=container.calendar,
        mailer=container.mailer,
        plan_store=container.plan_store,
        clock=container.clock,
        ids=container.ids,
        audit=container.audit,
        # The model may polish the referral; it may not author it. A draft that
        # drops a fact id or the no-determination sentence is rejected and the
        # deterministic template ships instead.
        model=container.model,
        # Where an approved referral is emailed. The mailer is separate from
        # ``mailer`` because crew notification and inter-agency filing are
        # different acts with different consequences and different recipients.
        referral_mailer=container.referral_mailer,
        referral_recipients=container.settings.referral_recipients,
    )
    return records, geometry, hazards, structure_watch, actions


#: The pass in flight for a district, so a second caller joins it instead of
#: starting a second one. Keyed by the district and by ``approve``, because a
#: pass that grants the staged referral and one that leaves it waiting are two
#: different passes and a joiner must not be handed the wrong one.
_IN_FLIGHT: Final[dict[tuple[str, bool], asyncio.Task[SlowLoopReport]]] = {}


async def run_slow_loop(
    container: Container,
    *,
    district_id: str | None = None,
    approve: bool = True,
    company: str = DEFAULT_COMPANY,
    crew_email: str = DEFAULT_CREW_EMAIL,
) -> SlowLoopReport:
    """Run one complete slow-loop pass over a district, or join the one running.

    **A district has one pass at a time.** A live pass is minutes long -- the
    records read alone measured 95 s and the hazard cross-check 165 s -- and
    every caller into this module is a timer somebody else set: the console's
    own choreography, a second console tab, a reload that abandoned the request
    and not the work behind it, a scheduler. Each of those used to start its
    own pass, so five ran at once against one district, and everything that
    followed came from that:

    * every agent contended for the same profiles, so ``structure-watch``
      spent its whole 60 s budget losing version checks and timed out on every
      pass, which also meant ``referral-clerk`` was never reached;
    * the incident loop's own writes lost those races too, which is how a
      composing run died with ``STALE_VERSION`` and staged no entry package;
    * and each pass minted its own correlation id, so the console -- which
      scopes the slow-loop column to the pass in flight, found in the audit log
      -- re-anchored on whichever pass had written most recently and drew every
      other agent ``0 recorded`` and idle through work they were doing.

    Joining rather than queueing, and rather than refusing. A queue would run
    the same duplicate passes in series and merely spread the contention out;
    a refusal would make the console report a failure for a pass that is
    running perfectly well. The joiner gets the report of the pass that was
    already in flight, which is the answer it would have computed itself.

    A caller that goes away does not take the pass with it: the pass is a task,
    and cancelling the request that started it cancels the *await*, not the
    work. That is the behaviour the console already relies on.

    Args:
        container: the wired process.
        district_id: which district. Defaults to the configured one.
        approve: whether to grant the staged referral. ``False`` leaves it
            waiting, which is the honest default state -- the demo grants it so
            the case-number write-back is visible in one command.
    """
    district = district_id or container.settings.default_district_id
    key = (district, approve)
    running = _IN_FLIGHT.get(key)
    if running is not None and not running.done():
        logger.info("slow_loop_joined", extra={"district_id": district})
        return await asyncio.shield(running)
    task = asyncio.ensure_future(
        _run_one_pass(
            container,
            district=district,
            approve=approve,
            company=company,
            crew_email=crew_email,
        )
    )
    _IN_FLIGHT[key] = task
    # Cleared when the pass ends rather than when this caller stops waiting.
    # A `finally` here would drop the entry the moment an abandoned request
    # unwound, leaving the next tick free to start a second pass against the
    # first one still running -- which is the bug this function exists to fix.
    task.add_done_callback(
        lambda done: _IN_FLIGHT.pop(key, None) if _IN_FLIGHT.get(key) is done else None
    )
    return await asyncio.shield(task)


async def _run_one_pass(
    container: Container,
    *,
    district: str,
    approve: bool,
    company: str,
    crew_email: str,
) -> SlowLoopReport:
    """The pass itself. Always reached through :func:`run_slow_loop`."""
    correlation_id = container.ids.new_id("corr")
    records, geometry, hazards, structure_watch, actions = build_agents(container)
    sources = list(container.source_adapters)
    fleet = build_fleet_runner(container)

    # Every pass below runs *through the runtime*: the grant is checked before
    # any work, the descriptor's latency target is the deadline, and the run --
    # completed, denied, or timed out -- lands on the record naming the pinned
    # version that produced it. Nothing here calls an agent directly.
    fleet.register_all(SLOW_LOOP_HANDLERS)
    current = _Pass(
        district=district,
        records=records,
        geometry_agent=geometry,
        hazard_agent=hazards,
        structure_watch=structure_watch,
        actions=actions,
        sources=sources,
        company=company,
        crew_email=crew_email,
    )
    _PASSES[correlation_id] = current
    runs: list[FleetRun] = []
    try:
        for agent_id in (
            "records-watcher",
            "geometry-watcher",
            "hazard-watcher",
            "structure-watch",
        ):
            run = await fleet.run(
                agent_id,
                correlation_id=correlation_id,
                parameters={"district_id": district},
            )
            runs.append(run)
            await _record_unfinished(container, run, district_id=district)

        queue: StructureWatchResult = current.queue or StructureWatchResult(district_id=district)
        if queue.entries:
            run = await fleet.run(
                "referral-clerk",
                correlation_id=correlation_id,
                parameters={
                    "district_id": district,
                    "entry_id": queue.entries[0].entry_id,
                },
            )
            runs.append(run)
            await _record_unfinished(container, run, district_id=district)
    finally:
        _PASSES.pop(correlation_id, None)

    watch: WatchResult = current.watch or WatchResult(district_id=district)
    geometry_result = current.geometry or GeometryWatchResult(district_id=district)
    hazard_result = current.hazard or HazardWatchResult(district_id=district)
    dispatch: DispatchResult | None = current.dispatch
    approval: ApprovalResult | None = None
    if approve and dispatch is not None and dispatch.referral_id:
        approval = await actions.approve_referral(
            dispatch.referral_id, approved_by=APPROVER, correlation_id=correlation_id
        )

    conflicts = tuple(
        sorted(
            {
                *watch.conflicts_detected,
                *geometry_result.conflicts_detected,
            }
        )
    )
    top = queue.entries[0] if queue.entries else None

    logger.info(
        "slow_loop_complete",
        extra={
            "district_id": district,
            "facts": watch.facts_written + geometry_result.facts_written,
            "conflicts": len(conflicts),
            "queue": len(queue.entries),
        },
    )
    return SlowLoopReport(
        district_id=district,
        ran_at=container.clock.now(),
        correlation_id=correlation_id,
        facts_written=(
            watch.facts_written + geometry_result.facts_written + hazard_result.facts_written
        ),
        facts_deduped=watch.facts_deduped,
        conflicts=conflicts,
        unavailable_sources=tuple(
            sorted(
                {
                    *watch.unavailable_sources,
                    *geometry_result.unavailable_sources,
                    *hazard_result.unavailable_sources,
                }
            )
        ),
        screen_findings=watch.screen_findings,
        queue_size=len(queue.entries),
        top_address_id=top.address_id if top else None,
        top_score=top.score if top else 0.0,
        top_reasons=tuple(reason.detail for reason in top.reasons) if top else (),
        dispatch=dispatch,
        approval=approval,
        agent_runs=tuple(
            AgentRunSummary(
                agent_id=run.agent_id,
                version=run.version,
                status=str(run.result.status),
                duration_ms=round(run.result.duration_ms, 3),
                facts_written=len(run.result.written_fact_ids),
            )
            for run in runs
        ),
    )
