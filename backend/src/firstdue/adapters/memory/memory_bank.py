"""In-memory memory-bank repositories with real semantics.

Real means the same things it means for the other in-memory adapters: a mutex
around every mutation so concurrent writers behave the way Firestore
transactions do, a create that refuses a duplicate id rather than overwriting
it, and an idempotent checkpoint write that returns the stored position instead
of a second copy of it.

These are not stubs. The bank's idempotent-open path depends on ``add`` losing
loudly when two instances derive the same question id at the same instant, and
that has to be true here as well as in Firestore -- otherwise the unit suite
proves a property the deployed system does not have.

What this adapter does *not* have is durability, which is the entire point of
the component. It backs the fake-mode demo and the tests; a deployment that
needs a question to survive a scale-to-zero runs the Firestore adapter.
"""

from __future__ import annotations

import asyncio

from firstdue.domain.memory import MemoryCheckpoint, OpenQuestion, QuestionStatus
from firstdue.errors import AppendOnlyViolationError, NotFoundError


class InMemoryOpenQuestionRepository:
    """Open questions, keyed by their derived id."""

    def __init__(self) -> None:
        self._by_id: dict[str, OpenQuestion] = {}
        self._lock = asyncio.Lock()

    async def add(self, question: OpenQuestion) -> OpenQuestion:
        async with self._lock:
            if question.question_id in self._by_id:
                raise AppendOnlyViolationError(
                    "this question is already open; reopening is one question",
                    details={"question_id": question.question_id},
                )
            self._by_id[question.question_id] = question
            return question

    async def get(self, question_id: str) -> OpenQuestion | None:
        return self._by_id.get(question_id)

    async def save(self, question: OpenQuestion) -> OpenQuestion:
        async with self._lock:
            stored = self._by_id.get(question.question_id)
            if stored is None:
                raise NotFoundError(
                    "open question not found", details={"question_id": question.question_id}
                )
            if stored.status is QuestionStatus.RESOLVED and question.status is not (
                QuestionStatus.RESOLVED
            ):
                # A pass that read the thread while it was open, and wrote back
                # after somebody answered it, would otherwise erase the answer
                # and set the fleet looking again for something it already has.
                raise AppendOnlyViolationError(
                    "a resolved question cannot be reopened by a stale write",
                    details={"question_id": question.question_id},
                )
            self._by_id[question.question_id] = question
            return question

    async def list_open(
        self, *, district_id: str, address_id: str | None = None
    ) -> tuple[OpenQuestion, ...]:
        """Newest first, and deterministically so.

        The id breaks ties on ``opened_at`` because a deterministic clock hands
        two questions opened in one pass the same instant, and a recall whose
        order depended on dict insertion would replay differently than it ran.
        """
        matched = [
            question
            for question in self._by_id.values()
            if question.district_id == district_id
            and question.status is QuestionStatus.OPEN
            and (address_id is None or question.address_id == address_id)
        ]
        matched.sort(key=lambda q: (q.opened_at, q.question_id), reverse=True)
        return tuple(matched)


class InMemoryCheckpointRepository:
    """Graph positions, addressed by a hash of their contents."""

    def __init__(self) -> None:
        self._by_id: dict[str, MemoryCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def put(self, checkpoint: MemoryCheckpoint) -> MemoryCheckpoint:
        async with self._lock:
            existing = self._by_id.get(checkpoint.checkpoint_id)
            if existing is not None:
                # The id is derived from the contents, so an existing id means
                # the identical position. Storing it twice is storing it once.
                return existing
            self._by_id[checkpoint.checkpoint_id] = checkpoint
            return checkpoint

    async def latest(self, question_id: str) -> MemoryCheckpoint | None:
        matched = [c for c in self._by_id.values() if c.question_id == question_id]
        if not matched:
            return None
        return max(matched, key=lambda c: (c.created_at, c.checkpoint_id))
