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
from firstdue.errors import ConfigurationError
from firstdue.extraction.extractor import FactExtractor
from firstdue.observability.logging import get_logger
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
    )
    return outcome(facts=current.watch.written_fact_ids)


async def _run_geometry(payload: AgentInput, _grant: object) -> AgentOutcome:
    current = _pass_for(payload)
    current.geometry = await current.geometry_agent.poll(
        district_id=current.district,
        sources=current.sources,
        correlation_id=payload.correlation_id,
    )
    return outcome(facts=current.geometry.written_fact_ids)


async def _run_hazards(payload: AgentInput, _grant: object) -> AgentOutcome:
    current = _pass_for(payload)
    current.hazard = await current.hazard_agent.poll(
        district_id=current.district,
        sources=current.sources,
        correlation_id=payload.correlation_id,
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
        current.district, correlation_id=payload.correlation_id
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
    )
    geometry = GeometryWatcher(
        profiles=container.profiles,
        facts=container.facts,
        materializer=materializer,
        clock=container.clock,
    )
    hazards = HazardWatcher(
        profiles=container.profiles,
        facts=container.facts,
        materializer=materializer,
        clock=container.clock,
    )
    structure_watch = StructureWatch(
        profiles=container.profiles,
        conflicts=container.conflicts,
        queue=container.queue,
        clock=container.clock,
        ids=container.ids,
        bus=container.bus,
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
    )
    return records, geometry, hazards, structure_watch, actions


async def run_slow_loop(
    container: Container,
    *,
    district_id: str | None = None,
    approve: bool = True,
    company: str = DEFAULT_COMPANY,
    crew_email: str = DEFAULT_CREW_EMAIL,
) -> SlowLoopReport:
    """Run one complete slow-loop pass over a district.

    Args:
        container: the wired process.
        district_id: which district. Defaults to the configured one.
        approve: whether to grant the staged referral. ``False`` leaves it
            waiting, which is the honest default state -- the demo grants it so
            the case-number write-back is visible in one command.
    """
    district = district_id or container.settings.default_district_id
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
            runs.append(
                await fleet.run(
                    agent_id,
                    correlation_id=correlation_id,
                    parameters={"district_id": district},
                )
            )

        queue: StructureWatchResult = current.queue or StructureWatchResult(district_id=district)
        if queue.entries:
            runs.append(
                await fleet.run(
                    "referral-clerk",
                    correlation_id=correlation_id,
                    parameters={
                        "district_id": district,
                        "entry_id": queue.entries[0].entry_id,
                    },
                )
            )
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
