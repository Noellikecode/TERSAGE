"""Resumable district backfill.

Polling eleven sources across 3,800 structures is not a request; it is a job
that takes minutes and that Cloud Run will interrupt. So it checkpoints.

Each checkpoint records the source, the page cursor, and the addresses already
handled, appended to the run record. A resumed backfill reads the last
checkpoint and starts from exactly there -- it does not restart the district and
it does not skip the page it was on when the instance went away.

A source that fails does not fail the backfill. It is recorded as unavailable
for this pass and the remaining sources still run: a hazmat feed being down must
never stop the permit feed from updating a profile.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AgentRunStatus
from firstdue.domain.runs import AgentRunRecord, RunCheckpoint
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import AgentRunRepository
from firstdue.ports.sources import SourceAdapter, SourceSnapshot

logger = get_logger(__name__)


class SourcePass(BaseModel):
    """What one source contributed to one backfill pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    pages: int = Field(default=0, ge=0)
    records: int = Field(default=0, ge=0)
    snapshot_ids: tuple[str, ...] = ()
    #: Set when the source could not be reached. The pass continues regardless.
    unavailable_reason: str | None = Field(default=None, max_length=200)

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None


class BackfillResult(BaseModel):
    """The outcome of one backfill run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    district_id: str
    status: AgentRunStatus
    passes: tuple[SourcePass, ...] = ()
    #: Addresses whose records were pulled on this run.
    addresses_covered: tuple[str, ...] = ()
    #: True when this run continued an earlier, interrupted one.
    resumed: bool = False
    checkpoints: int = Field(default=0, ge=0)

    @property
    def total_records(self) -> int:
        return sum(p.records for p in self.passes)

    @property
    def unavailable_sources(self) -> tuple[str, ...]:
        return tuple(p.source_id for p in self.passes if not p.available)


class DistrictBackfill:
    """Pulls every configured source for a district, resumably."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        clock: Clock,
        ids: IdGenerator,
        agent_id: str = "records-watcher",
        agent_version: str = "1.0.0",
        max_pages_per_source: int = 100,
    ) -> None:
        self._runs = runs
        self._clock = clock
        self._ids = ids
        self._agent_id = agent_id
        self._agent_version = agent_version
        self._max_pages = max_pages_per_source

    def idempotency_key(self, district_id: str, watermark: datetime | None) -> str:
        """One backfill per (district, watermark). A re-dispatch finds the run."""
        return self._ids.idempotency_key(
            "backfill", self._agent_id, district_id, watermark.isoformat() if watermark else "*"
        )

    async def run(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        since: datetime | None = None,
        resume_run_id: str | None = None,
        interrupt_after_pages: int | None = None,
    ) -> BackfillResult:
        """Pull every source for a district.

        Args:
            district_id: the district being polled.
            sources: the adapters to pull, in order.
            correlation_id: threads this run through the audit log.
            since: watermark. Only records observed at or after it are pulled.
            resume_run_id: continue an interrupted run from its last checkpoint.
            interrupt_after_pages: stop after this many pages and leave the run
                resumable. This is how the interruption path is tested; nothing
                in production passes it.
        """
        run, resumed = await self._start_or_resume(district_id, since, resume_run_id)
        start_index, start_cursor, done = self._resume_position(run, sources)

        passes: list[SourcePass] = []
        covered: set[str] = set(done)
        pages_this_run = 0
        checkpoints = len(run.checkpoints)

        for index in range(start_index, len(sources)):
            source = sources[index]
            cursor = start_cursor if index == start_index else None
            source_pass, pages, snapshots = await self._pull_source(
                source,
                since=since,
                cursor=cursor,
                budget=(
                    None
                    if interrupt_after_pages is None
                    else max(0, interrupt_after_pages - pages_this_run)
                ),
            )
            passes.append(source_pass)
            pages_this_run += pages

            for snapshot in snapshots:
                covered.update(r.address_id for r in snapshot.records if r.address_id)

            checkpoint_cursor = snapshots[-1].next_cursor if snapshots else None
            run = await self._runs.checkpoint(
                run.run_id,
                RunCheckpoint(
                    checkpoint_id=self._ids.new_id("cp"),
                    sequence=run.next_checkpoint_sequence,
                    taken_at=self._clock.now(),
                    stage=f"poll:{source.source_id}",
                    cursor=checkpoint_cursor,
                    processed_ids=tuple(sorted(covered)),
                    items_done=source_pass.records,
                ),
            )
            checkpoints += 1

            if interrupt_after_pages is not None and pages_this_run >= interrupt_after_pages:
                # Stop where we are and stay resumable. The next run picks up
                # from this checkpoint, not from the top of the district.
                logger.info(
                    "backfill_interrupted",
                    extra={
                        "district_id": district_id,
                        "run_id": run.run_id,
                        "source_id": source.source_id,
                    },
                )
                await self._runs.save(
                    run.finished(
                        AgentRunStatus.TIMED_OUT,
                        at=self._clock.now(),
                        error_code="INTERRUPTED",
                        error_message="backfill stopped at a checkpoint and is resumable",
                    )
                )
                return BackfillResult(
                    run_id=run.run_id,
                    district_id=district_id,
                    status=AgentRunStatus.TIMED_OUT,
                    passes=tuple(passes),
                    addresses_covered=tuple(sorted(covered)),
                    resumed=resumed,
                    checkpoints=checkpoints,
                )

        await self._runs.save(run.finished(AgentRunStatus.COMPLETED, at=self._clock.now()))
        logger.info(
            "backfill_completed",
            extra={
                "district_id": district_id,
                "run_id": run.run_id,
                "records": sum(p.records for p in passes),
                "unavailable": len([p for p in passes if not p.available]),
            },
        )
        return BackfillResult(
            run_id=run.run_id,
            district_id=district_id,
            status=AgentRunStatus.COMPLETED,
            passes=tuple(passes),
            addresses_covered=tuple(sorted(covered)),
            resumed=resumed,
            checkpoints=checkpoints,
        )

    # ------------------------------------------------------------ internals

    async def _start_or_resume(
        self, district_id: str, since: datetime | None, resume_run_id: str | None
    ) -> tuple[AgentRunRecord, bool]:
        if resume_run_id is not None:
            previous = await self._runs.get(resume_run_id)
            if previous is not None and previous.checkpoints:
                # A terminal run cannot transition again, so resuming forks a
                # new run that inherits the position. The failed run stays on
                # the record -- that is the point of a terminal state.
                fresh = AgentRunRecord(
                    run_id=self._ids.new_id("run"),
                    agent_id=self._agent_id,
                    agent_version=self._agent_version,
                    status=AgentRunStatus.RUNNING,
                    correlation_id=previous.correlation_id,
                    causation_id=previous.run_id,
                    idempotency_key=self.idempotency_key(district_id, since),
                    attempt=previous.attempt + 1,
                    started_at=self._clock.now(),
                    checkpoints=previous.checkpoints,
                )
                return await self._runs.start(fresh), True

        run = AgentRunRecord(
            run_id=self._ids.new_id("run"),
            agent_id=self._agent_id,
            agent_version=self._agent_version,
            status=AgentRunStatus.RUNNING,
            correlation_id=self._ids.new_id("corr"),
            idempotency_key=self.idempotency_key(district_id, since),
            started_at=self._clock.now(),
        )
        return await self._runs.start(run), False

    @staticmethod
    def _resume_position(
        run: AgentRunRecord, sources: Sequence[SourceAdapter]
    ) -> tuple[int, str | None, tuple[str, ...]]:
        """Where to start: which source, which cursor, and what is already done."""
        checkpoint = run.resume_point
        if checkpoint is None:
            return 0, None, ()

        stage_source = checkpoint.stage.removeprefix("poll:")
        for index, source in enumerate(sources):
            if source.source_id != stage_source:
                continue
            if checkpoint.cursor is not None:
                # Mid-source: pick up on the very page it stopped on.
                return index, checkpoint.cursor, checkpoint.processed_ids
            # That source finished; continue with the next one.
            return index + 1, None, checkpoint.processed_ids
        return 0, None, checkpoint.processed_ids

    async def _pull_source(
        self,
        source: SourceAdapter,
        *,
        since: datetime | None,
        cursor: str | None,
        budget: int | None,
    ) -> tuple[SourcePass, int, list[SourceSnapshot]]:
        snapshots: list[SourceSnapshot] = []
        records = 0
        pages = 0
        next_cursor = cursor

        while pages < self._max_pages:
            try:
                snapshot = await source.fetch(since=since, cursor=next_cursor)
            except SourceUnavailableError as exc:
                # One source being down never stops the others.
                logger.warning(
                    "backfill_source_unavailable",
                    extra={"source_id": source.source_id, "error_code": str(exc.code)},
                )
                return (
                    SourcePass(
                        source_id=source.source_id,
                        pages=pages,
                        records=records,
                        snapshot_ids=tuple(s.snapshot_id for s in snapshots),
                        unavailable_reason=str(exc.code),
                    ),
                    pages,
                    snapshots,
                )

            snapshots.append(snapshot)
            records += len(snapshot.records)
            pages += 1
            next_cursor = snapshot.next_cursor
            if next_cursor is None:
                break
            if budget is not None and pages >= budget:
                break

        return (
            SourcePass(
                source_id=source.source_id,
                pages=pages,
                records=records,
                snapshot_ids=tuple(s.snapshot_id for s in snapshots),
            ),
            pages,
            snapshots,
        )
