"""Agent runs: terminal states, resumable checkpoints, compensating actions.

Three rules, each of which exists because of a specific failure:

* **Every run reaches a terminal state.** A run that is neither finished nor
  failed is a run nobody will investigate, because nothing shows it as broken.
  :data:`TERMINAL_STATUSES` is the closed set, and a record in one of them can
  never transition again.
* **Long work checkpoints.** Polling eleven sources across 3,800 structures does
  not restart from zero because instance seven was preempted at minute nine.
  Checkpoints are append-only and gapless, and each carries the cursor needed to
  resume exactly where the last one stopped.
* **Executed writes name their undo.** A run that filed three referrals and then
  failed leaves three compensating-action records, so withdrawal is a recorded
  obligation rather than an operator's memory.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import AgentRunStatus
from firstdue.errors import AppendOnlyViolationError, ValidationError

#: Statuses from which there is no transition. Checked, not assumed.
TERMINAL_STATUSES: Final[frozenset[AgentRunStatus]] = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.TIMED_OUT,
        AgentRunStatus.DENIED,
        AgentRunStatus.CANCELLED,
    }
)

#: Statuses a run may still leave.
ACTIVE_STATUSES: Final[frozenset[AgentRunStatus]] = frozenset(
    {AgentRunStatus.PENDING, AgentRunStatus.RUNNING}
)


def is_terminal(status: AgentRunStatus) -> bool:
    return status in TERMINAL_STATUSES


class RunCheckpoint(BaseModel):
    """One resumable position inside a long run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=0, description="gapless, monotonic within a run")
    taken_at: datetime

    #: Which part of the run this position belongs to, e.g. ``poll:sf-permits``.
    stage: str = Field(min_length=1, max_length=120)
    #: Opaque resume token from the source -- a page cursor, a watermark.
    cursor: str | None = Field(default=None, max_length=400)
    #: Identifiers already processed at this stage, so resume skips them.
    processed_ids: tuple[str, ...] = ()
    #: Count of items handled so far, for progress reporting.
    items_done: int = Field(default=0, ge=0)


class CompensationStatus(StrEnum):
    RECORDED = "RECORDED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class CompensationRecord(BaseModel):
    """The obligation to undo one executed write.

    Created when the write executes, not when the failure happens: a system that
    only records the undo after something goes wrong has no undo for the failure
    that took the process with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    compensation_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=1, max_length=120, description="the write being undone")
    run_id: str | None = Field(default=None, max_length=120)
    target: str = Field(min_length=1, max_length=120)

    #: Copied from ``WriteAction.compensating_action``: what undoing means here.
    compensating_action: str = Field(min_length=1, max_length=200)
    #: Idempotency key for the compensating write itself. Undo runs once too.
    idempotency_key: str = Field(min_length=8, max_length=200)
    reason: str = Field(min_length=1, max_length=400)

    status: CompensationStatus = CompensationStatus.RECORDED
    recorded_at: datetime
    executed_at: datetime | None = None
    external_ref: str | None = Field(default=None, max_length=200)
    correlation_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_execution(self) -> Self:
        if self.status is CompensationStatus.EXECUTED and self.executed_at is None:
            raise ValidationError(
                "an executed compensation must record when it executed",
                details={"compensation_id": self.compensation_id},
            )
        return self

    def executed(self, *, at: datetime, external_ref: str) -> CompensationRecord:
        return self.model_copy(
            update={
                "status": CompensationStatus.EXECUTED,
                "executed_at": at,
                "external_ref": external_ref,
            }
        )

    def failed(self, *, at: datetime, reason: str) -> CompensationRecord:
        return self.model_copy(
            update={
                "status": CompensationStatus.FAILED,
                "executed_at": at,
                "reason": reason,
            }
        )


class AgentRunRecord(BaseModel):
    """The durable record of one agent run, from claim to terminal state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    agent_version: str = Field(min_length=1, max_length=40)
    status: AgentRunStatus = AgentRunStatus.PENDING

    correlation_id: str = Field(min_length=1, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)
    #: The envelope or schedule tick that caused this run. Dedupes re-dispatch.
    idempotency_key: str = Field(min_length=8, max_length=200)
    #: Which delivery attempt produced this run.
    attempt: int = Field(default=1, ge=1)

    started_at: datetime
    finished_at: datetime | None = None
    #: Append-only, gapless. The last one is where a resume begins.
    checkpoints: tuple[RunCheckpoint, ...] = ()
    compensation_ids: tuple[str, ...] = ()

    written_fact_ids: tuple[str, ...] = ()
    emitted_event_ids: tuple[str, ...] = ()
    write_action_ids: tuple[str, ...] = ()

    error_code: str | None = Field(default=None, max_length=80)
    #: Redacted. Never carries source internals or record contents.
    error_message: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        terminal = is_terminal(self.status)
        if terminal and self.finished_at is None:
            raise ValidationError(
                "a terminal agent run must record when it finished",
                details={"run_id": self.run_id, "status": str(self.status)},
            )
        if not terminal and self.finished_at is not None:
            raise ValidationError(
                "an unfinished agent run must not record a finish time",
                details={"run_id": self.run_id, "status": str(self.status)},
            )
        if self.status is AgentRunStatus.FAILED and not self.error_code:
            raise ValidationError(
                "a failed agent run must record a stable error code",
                details={"run_id": self.run_id},
            )
        for index, checkpoint in enumerate(self.checkpoints):
            if checkpoint.sequence != index:
                raise AppendOnlyViolationError(
                    "run checkpoints must be gapless and monotonic",
                    details={"expected": index, "found": checkpoint.sequence},
                )
        return self

    # ------------------------------------------------------------- read side

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @property
    def is_resumable(self) -> bool:
        """A run that stopped short and left a position worth resuming from."""
        return (
            self.status in (AgentRunStatus.FAILED, AgentRunStatus.TIMED_OUT)
            and len(self.checkpoints) > 0
        )

    @property
    def resume_point(self) -> RunCheckpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    @property
    def next_checkpoint_sequence(self) -> int:
        return len(self.checkpoints)

    def duration_ms(self, now: datetime) -> float:
        end = self.finished_at if self.finished_at is not None else now
        return max(0.0, (end - self.started_at).total_seconds() * 1000.0)

    # ------------------------------------------------------------ write side

    def _guard_active(self) -> None:
        if self.is_terminal:
            raise ValidationError(
                "an agent run in a terminal state cannot transition again",
                details={"run_id": self.run_id, "status": str(self.status)},
            )

    def running(self) -> AgentRunRecord:
        self._guard_active()
        return self.model_copy(update={"status": AgentRunStatus.RUNNING})

    def checkpointed(self, checkpoint: RunCheckpoint) -> AgentRunRecord:
        """Append one checkpoint. Checkpoints are never rewritten."""
        self._guard_active()
        if checkpoint.sequence != self.next_checkpoint_sequence:
            raise AppendOnlyViolationError(
                "run checkpoints must be appended in sequence",
                details={"expected": self.next_checkpoint_sequence, "found": checkpoint.sequence},
            )
        return self.model_copy(update={"checkpoints": (*self.checkpoints, checkpoint)})

    def finished(
        self,
        status: AgentRunStatus,
        *,
        at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
        written_fact_ids: tuple[str, ...] = (),
        emitted_event_ids: tuple[str, ...] = (),
        write_action_ids: tuple[str, ...] = (),
    ) -> AgentRunRecord:
        """Move to a terminal state. There is no path back out of one."""
        self._guard_active()
        if not is_terminal(status):
            raise ValidationError(
                "finished() requires a terminal status",
                details={"run_id": self.run_id, "status": str(status)},
            )
        return self.model_copy(
            update={
                "status": status,
                "finished_at": at,
                "error_code": error_code,
                "error_message": error_message,
                "written_fact_ids": written_fact_ids or self.written_fact_ids,
                "emitted_event_ids": emitted_event_ids or self.emitted_event_ids,
                "write_action_ids": write_action_ids or self.write_action_ids,
            }
        )

    def with_compensation(self, compensation_id: str) -> AgentRunRecord:
        if compensation_id in self.compensation_ids:
            return self
        return self.model_copy(
            update={"compensation_ids": (*self.compensation_ids, compensation_id)}
        )
