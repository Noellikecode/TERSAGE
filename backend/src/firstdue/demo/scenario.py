"""The slow-loop demo, end to end, with no credentials.

One command, one district, and the sequence the whole product is built around:

1. The permit says the building at 450 Hayes has **two** storeys.
2. The lidar DSM measures 9.5 m, which is **three**.
3. Both facts are stored. Neither is corrected, averaged, or dropped.
4. The deterministic conflict engine records the disagreement.
5. The ranker puts that building at the top of the district's survey queue,
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
from firstdue.agents.geometry_watcher import GeometryWatcher
from firstdue.agents.hazard_watcher import HazardWatcher
from firstdue.agents.ranker import DeltaRanker, RankedQueue
from firstdue.agents.records_watcher import RecordsWatcher, WatchResult
from firstdue.container import Container
from firstdue.extraction.extractor import FactExtractor
from firstdue.observability.logging import get_logger
from firstdue.services.materialization import ProfileMaterializer

logger = get_logger(__name__)

#: The address the whole demo turns on: permit two, measurement three.
DISPUTED_ADDRESS_ID: Final[str] = "sf-0450-hayes"
DEFAULT_COMPANY: Final[str] = "E-05"
DEFAULT_CREW_EMAIL: Final[str] = "e05-crew@sffd.example"
APPROVER: Final[str] = "capt-alvarez"


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

    @property
    def produced_the_conflict(self) -> bool:
        return bool(self.conflicts)


def build_agents(
    container: Container,
) -> tuple[RecordsWatcher, GeometryWatcher, HazardWatcher, DeltaRanker, ActionFlow]:
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
    extractor = FactExtractor(ids=container.ids, model=container.model)

    records = RecordsWatcher(
        profiles=container.profiles,
        facts=container.facts,
        city=container.city,
        extractor=extractor,
        materializer=materializer,
        clock=container.clock,
        ids=container.ids,
        audit=container.audit,
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
    ranker = DeltaRanker(profiles=container.profiles, queue=container.queue, clock=container.clock)
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
    return records, geometry, hazards, ranker, actions


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
    records, geometry, hazards, ranker, actions = build_agents(container)
    sources = list(container.source_adapters)

    watch: WatchResult = await records.poll(
        district_id=district, sources=sources, correlation_id=correlation_id
    )
    geometry_result = await geometry.poll(
        district_id=district, sources=sources, correlation_id=correlation_id
    )
    hazard_result = await hazards.poll(
        district_id=district, sources=sources, correlation_id=correlation_id
    )

    queue: RankedQueue = await ranker.rank(district)

    dispatch: DispatchResult | None = None
    approval: ApprovalResult | None = None
    if queue.entries:
        dispatch = await actions.dispatch(
            queue.entries[0],
            company=company,
            crew_email=crew_email,
            correlation_id=correlation_id,
        )
        if approve and dispatch.referral_id:
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
    )
