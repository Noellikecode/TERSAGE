"""Memory-bank repository protocols.

Three rules run through both of them:

* **A question is created once and then edited.** Unlike a fact, an open
  question is a living thread -- it accumulates eliminations and examinations
  and eventually closes -- so ``save`` exists here where it deliberately does
  not on the append-only stores. What ``add`` refuses is a *second* creation of
  the same id: reopening is one question, and the duplicate is the bug.
* **Checkpoints are addressed by their contents.** ``put`` is idempotent for the
  same reason :class:`~firstdue.ports.repositories.SnapshotRepository.put` is: a
  redelivered message must not leave the resume path choosing between two
  identical positions.
* **Reading is by district, not by scan.** ``list_open`` is the query the slow
  loop runs on every pass and the one ``incident-recorder`` runs to close what
  the slow loop opened, so it takes the fields the Firestore composite indexes
  are built on and nothing else.

Scope filtering is deliberately *not* here. A repository that filtered would
make the security boundary a property of the adapter, and there are two
adapters; :class:`~firstdue.services.memory_bank.MemoryBank` filters once, above
both of them, where it is one function a reviewer can read.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from firstdue.domain.memory import MemoryCheckpoint, OpenQuestion


@runtime_checkable
class OpenQuestionRepository(Protocol):
    """Durable storage for threads the fleet has not finished."""

    async def add(self, question: OpenQuestion) -> OpenQuestion:
        """Create a question.

        Raises:
            AppendOnlyViolationError: when the id already exists. Two instances
                polling one district will both derive the same id for the same
                question, and exactly one of them may create it -- the loser
                re-reads and records an examination instead.
        """
        ...

    async def get(self, question_id: str) -> OpenQuestion | None: ...

    async def save(self, question: OpenQuestion) -> OpenQuestion:
        """Persist a transition on an existing question.

        Raises:
            NotFoundError: when nothing was ever opened under this id.
        """
        ...

    async def list_open(
        self, *, district_id: str, address_id: str | None = None
    ) -> tuple[OpenQuestion, ...]:
        """Open questions for a district, newest first.

        ``address_id`` narrows to one building. Omitting it means *every*
        building in the district, not "questions with no address" -- the slow
        loop asks the district-wide form once per pass, and the incident loop
        asks the narrow one about the address it was dispatched to.
        """
        ...


@runtime_checkable
class CheckpointRepository(Protocol):
    """Durable storage for graph state that ran out of budget mid-thought."""

    async def put(self, checkpoint: MemoryCheckpoint) -> MemoryCheckpoint:
        """Store a position. Storing an id that already exists is a no-op."""
        ...

    async def latest(self, question_id: str) -> MemoryCheckpoint | None:
        """The most recent position for a question, or ``None``.

        ``None`` is the ordinary answer for a question nobody has checkpointed,
        and callers must start from the beginning rather than treat it as an
        error.
        """
        ...
