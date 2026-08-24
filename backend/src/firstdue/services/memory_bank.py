"""The Memory Bank -- persistent, scope-gated context across extended timelines.

The slow loop's problem is not that it reasons badly; it is that it cannot
finish. ``records-watcher`` finds a 2019 permit citing prior permit
``201804-3321``, cannot find that permit in the published window, and the pass
ends. The next pass starts from nothing and fails the same way, at the same
cost, for as long as the missing record takes to appear -- which for municipal
records is routinely weeks and sometimes months.

The bank turns that dead end into a durable open question. The next pass recalls
it instead of rediscovering it, skips the dead ends the last pass eliminated,
and closes it when the record finally lands. ``incident-recorder`` reads the same
store, so a thread the slow loop opened in March can be closed by what an
officer observed in May.

Two properties are load-bearing and neither is optional:

**Reopening is idempotent.** ``question_id`` is derived from the question's
natural key (see :func:`~firstdue.domain.memory.derive_question_id`), the same
way ``ActionFlow`` derives a referral id from the conflict it refers. Asking the
same question on every pass produces one record with a rising
``examined_count``, not one record per pass. A bank that accumulated duplicates
would cost more to carry than the search it replaced.

**Recall is the security boundary.** :meth:`MemoryBank.recall` takes the
caller's scopes and there is no overload that omits them. A memory derived from
a Tier II filing is not returned to an agent that does not hold
``read:tier-ii-metadata``, and the filtering happens here -- once, above both
adapters -- rather than in each repository, because a boundary implemented twice
is a boundary enforced once.

Nothing here reads the wall clock. ``now`` arrives from a
:class:`~firstdue.ports.clock.Clock`, so a replayed pass re-derives the same ids
and the same timestamps.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

from firstdue.domain.enums import Classification, Scope
from firstdue.domain.memory import (
    MemoryCheckpoint,
    OpenQuestion,
    derive_checkpoint_id,
    derive_question_id,
)
from firstdue.errors import AppendOnlyViolationError, NotFoundError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.memory import CheckpointRepository, OpenQuestionRepository

logger = get_logger(__name__)


class MemoryBank:
    """Opens, recalls, and closes the questions the fleet is still carrying."""

    def __init__(
        self,
        *,
        questions: OpenQuestionRepository,
        checkpoints: CheckpointRepository,
        clock: Clock,
    ) -> None:
        self._questions = questions
        self._checkpoints = checkpoints
        self._clock = clock

    # ----------------------------------------------------------------- open

    async def open(
        self,
        *,
        district_id: str,
        question: str,
        waiting_on: str,
        opened_by: str,
        opened_by_version: str,
        classification: Classification,
        address_id: str | None = None,
        confidence: float = 0.5,
        evidence_fact_ids: tuple[str, ...] = (),
        ruled_out: tuple[str, ...] = (),
        expires_at: datetime | None = None,
    ) -> OpenQuestion:
        """Open a question, or pick the existing one back up.

        Idempotent on the derived id. The second ask returns the stored record
        with ``last_examined_at`` and ``examined_count`` moved on -- including
        everything the earlier passes ruled out, which is the part that makes
        the next attempt cheaper rather than merely repeatable.

        The create is attempted rather than guarded on the read: two instances
        polling one district derive the same id at the same moment, and the one
        that loses the create re-reads and records an examination. Guarding on a
        read alone would let both of them believe they created it.
        """
        now = self._clock.now()
        question_id = derive_question_id(
            district_id=district_id,
            address_id=address_id,
            opened_by=opened_by,
            question=question,
        )

        existing = await self._questions.get(question_id)
        if existing is not None:
            return await self._questions.save(existing.examined(now))

        candidate = OpenQuestion(
            question_id=question_id,
            district_id=district_id,
            address_id=address_id,
            question=question,
            opened_by=opened_by,
            opened_by_version=opened_by_version,
            opened_at=now,
            last_examined_at=now,
            waiting_on=waiting_on,
            ruled_out=ruled_out,
            evidence_fact_ids=evidence_fact_ids,
            classification=classification,
            confidence=confidence,
            expires_at=expires_at,
        )
        try:
            opened = await self._questions.add(candidate)
        except AppendOnlyViolationError:
            stored = await self._questions.get(question_id)
            if stored is None:  # pragma: no cover - the create said it existed
                raise
            return await self._questions.save(stored.examined(now))

        logger.info(
            "memory_question_opened",
            extra={
                "question_id": question_id,
                "district_id": district_id,
                "agent_id": opened_by,
                "classification": str(classification),
            },
        )
        return opened

    # --------------------------------------------------------------- recall

    async def recall(
        self,
        *,
        district_id: str,
        scopes: Collection[Scope],
        address_id: str | None = None,
    ) -> tuple[OpenQuestion, ...]:
        """What this caller is still carrying, and is authorized to see.

        ``scopes`` is required and has no default. A recall that could be called
        without them would be a recall that leaks by omission, and "somebody
        forgot the argument" is not a failure mode worth leaving reachable.

        Expired questions are withheld here as well as swept later by
        :meth:`abandon`. If recall returned them, whether an agent picked up a
        dead thread would depend on when a background sweep last ran, and a
        security-adjacent read whose answer depends on cron timing is one nobody
        can reason about.
        """
        stored = await self._questions.list_open(district_id=district_id, address_id=address_id)
        now = self._clock.now()
        visible = tuple(
            question
            for question in stored
            if question.is_visible_to(scopes) and not question.is_expired(now)
        )
        withheld = len(stored) - len(visible)
        if withheld:
            # Counts only. Which questions were withheld, and what they were
            # about, is exactly what the caller is not authorized to learn.
            logger.info(
                "memory_recall_filtered",
                extra={
                    "district_id": district_id,
                    "returned": len(visible),
                    "withheld": withheld,
                },
            )
        return visible

    async def get(self, question_id: str, *, scopes: Collection[Scope]) -> OpenQuestion | None:
        """One question by id, gated the same way recall is.

        Returns ``None`` rather than raising when the caller is not authorized.
        A refusal distinguishable from an absence would tell an unauthorized
        caller that the question exists, which is half of what it was not
        allowed to know.
        """
        question = await self._questions.get(question_id)
        if question is None or not question.is_visible_to(scopes):
            return None
        return question

    # ---------------------------------------------------------- transitions

    async def rule_out(
        self, question_id: str, *candidates: str, now: datetime | None = None
    ) -> OpenQuestion:
        """Record what was tried and eliminated.

        Not gated on scopes: this is a write by the agent that already holds the
        thread, and the gateway governs whether that agent may act at all. What
        it protects against is the loop paying for the same dead end every pass.
        """
        question = await self._require(question_id)
        return await self._questions.save(
            question.rule_out(*candidates, now=now or self._clock.now())
        )

    async def resolve(
        self,
        question_id: str,
        *,
        resolution: str,
        resolved_by: str,
        now: datetime | None = None,
    ) -> OpenQuestion:
        """Close the thread because something settled it.

        ``now`` is accepted so the incident loop can close a question at the
        instant it recorded the observation rather than at the instant it got
        around to writing it down; omitted, it comes from the clock. Neither
        path reads the wall clock directly.
        """
        question = await self._require(question_id)
        closed = question.resolved(
            resolution=resolution, resolved_by=resolved_by, now=now or self._clock.now()
        )
        saved = await self._questions.save(closed)
        logger.info(
            "memory_question_resolved",
            extra={
                "question_id": question_id,
                "resolved_by": resolved_by,
                "examined_count": saved.examined_count,
            },
        )
        return saved

    async def abandon(
        self,
        question_id: str,
        *,
        reason: str,
        abandoned_by: str,
        now: datetime | None = None,
    ) -> OpenQuestion:
        """Stop carrying the thread, and record that nobody ever found out.

        Abandonment is a recorded outcome, not a deletion. "We stopped looking"
        and "there was nothing there" are different facts, and a bank that
        dropped the row would be asserting the second one.
        """
        question = await self._require(question_id)
        closed = question.abandoned(
            reason=reason, abandoned_by=abandoned_by, now=now or self._clock.now()
        )
        saved = await self._questions.save(closed)
        logger.info(
            "memory_question_abandoned",
            extra={"question_id": question_id, "abandoned_by": abandoned_by},
        )
        return saved

    async def abandon_expired(
        self, *, district_id: str, abandoned_by: str, reason: str | None = None
    ) -> tuple[OpenQuestion, ...]:
        """Close every question in a district that outlived its window.

        Reads the repository directly rather than through :meth:`recall`, and
        that is deliberate: the sweep is a caretaker running over the whole
        district, not a caller being shown anything. It never returns a question
        to an agent, so gating it on scopes would mean a Tier II thread nobody
        held the scope for could never be swept at all.
        """
        now = self._clock.now()
        stored = await self._questions.list_open(district_id=district_id)
        swept: list[OpenQuestion] = []
        for question in stored:
            if not question.is_expired(now):
                continue
            swept.append(
                await self._questions.save(
                    question.abandoned(
                        reason=reason or "the window closed before anything settled it",
                        abandoned_by=abandoned_by,
                        now=now,
                    )
                )
            )
        return tuple(swept)

    # ---------------------------------------------------------- checkpoints

    async def checkpoint(
        self, question_id: str, *, agent_id: str, state: Mapping[str, Any]
    ) -> MemoryCheckpoint:
        """Store the position of a graph that ran out of budget mid-thought.

        The classification is copied from the question rather than passed in.
        A checkpoint of a Tier II thread is a Tier II record whatever the caller
        thinks, and a caller that could label its own checkpoint could label its
        way out of the scope gate on the resume.
        """
        question = await self._require(question_id)
        now = self._clock.now()
        checkpoint = MemoryCheckpoint(
            checkpoint_id=derive_checkpoint_id(
                question_id=question_id, agent_id=agent_id, created_at=now, state=state
            ),
            question_id=question_id,
            agent_id=agent_id,
            created_at=now,
            state=dict(state),
            classification=question.classification,
        )
        return await self._checkpoints.put(checkpoint)

    async def resume(
        self, question_id: str, *, scopes: Collection[Scope]
    ) -> MemoryCheckpoint | None:
        """The position to resume this question from, if the caller may see it.

        Gated on scopes for the same reason :meth:`recall` is, and it is the
        same gate: a checkpoint carries the state of a thread, so handing one to
        a caller that may not recall the question would return through the back
        door what the front door refused. ``None`` covers both "never
        checkpointed" and "not yours", which is the answer a caller in either
        situation is entitled to.
        """
        checkpoint = await self._checkpoints.latest(question_id)
        if checkpoint is None or not checkpoint.is_visible_to(scopes):
            return None
        return checkpoint

    # ------------------------------------------------------------ internals

    async def _require(self, question_id: str) -> OpenQuestion:
        question = await self._questions.get(question_id)
        if question is None:
            raise NotFoundError("open question not found", details={"question_id": question_id})
        return question
