"""Recall over open question threads, by meaning rather than by key.

:meth:`~firstdue.services.memory_bank.MemoryBank.recall` answers *what is this
district still carrying* -- a structural query, narrowed by district and address
and nothing else. This port answers a different question: *have we asked
something like this before, anywhere in the district*. A records watcher about
to open a thread on an unpublished permit reference wants to know whether
another agent is already waiting on the same filing, and no key it holds will
find that.

**A match is a pointer, never an answer.** This is the same rule
:mod:`firstdue.ports.vectors` states for narratives and
:mod:`firstdue.incident.focus` states for the incident head, and it is enforced
the same way: :class:`ThreadMatch` carries ids and a distance, and there is no
field on it that the question's *content* could ride in. The caller takes the
``question_id`` back to the repositories and reads the record. That is what
keeps this index outside the security boundary -- see below -- and it is why an
index that fell behind, or returned a thread that has since resolved, costs a
wasted lookup rather than a wrong answer.

**Authorization is not decided here.** The index is not asked whether a caller
may see a thread and its answer would not be trusted if it were. Scope gating
happens once, in :class:`~firstdue.services.memory_bank.MemoryBank`, against the
stored question -- so a match the caller is not authorized for is dropped after
the record is read, exactly as it is for a structural recall. An index that
enforced would be a second copy of the boundary, and a boundary implemented
twice is a boundary enforced once.

**What never reaches an implementation of this port.** ``PHI`` and
``TIER_II_CONFIDENTIAL`` prose, refused at payload construction in
:mod:`firstdue.domain.threads`. Those threads stay recallable by district and
are absent from the index, which is a smaller capability rather than a quieter
one: the bank counts every skip.

**Failure is degradation, not loss.** The repositories are the record. An
implementation that cannot reach its backing service raises, and the bank
records the failure and carries on -- a question that failed to index is stored,
recallable structurally, and merely not findable by meaning until something
reindexes it. Nothing here may swallow an error silently, because "no similar
threads" and "the index did not answer" are the same distinction this project
draws everywhere else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.threads import ThreadMemory


class ThreadMatch(BaseModel):
    """One nearby thread, named by id so the record can be read.

    Deliberately down to an id, a district and a distance. An earlier version
    also carried the address and the agent that opened the thread, which was
    convenience rather than capability -- the bank reads the stored question
    before it returns anything, so every one of those fields was about to be
    overwritten by the record's own. Carrying them made the match look like a
    small answer, which is exactly what this type is not allowed to be.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    #: Lower is nearer. The unit is the index's own; it is shown to a human and
    #: never compared across implementations, the same rule
    #: :class:`~firstdue.ports.vectors.VectorMatch` states for its distance.
    distance: float


@runtime_checkable
class ThreadIndex(Protocol):
    """Semantic recall over the question threads the fleet is carrying."""

    async def remember(self, memory: ThreadMemory) -> None:
        """Index one thread's prose, or update it if the thread is already there.

        Idempotent on ``question_id``: reopening a question re-indexes the same
        entry rather than adding a second, because the id is derived from the
        question's natural key and the index is keyed on it.

        Raises:
            Exception: implementation-specific, on a backing-service failure.
                The caller records it and continues; the record is elsewhere.
        """
        ...

    async def recall_similar(
        self, text: str, *, district_id: str, limit: int = 5
    ) -> tuple[ThreadMatch, ...]:
        """Threads in this district whose prose is nearest ``text``, nearest first.

        Narrowed to one district by the implementation, not by the caller
        filtering afterwards: a cross-district match is not merely noise, it is
        a thread from a jurisdiction this caller may have no standing in.
        """
        ...

    async def forget(self, question_id: str) -> None:
        """Stop offering a thread. Forgetting an absent thread is a no-op.

        Called when a question leaves the open set. The record is kept forever;
        what this drops is the *pointer*, so a settled thread stops surfacing as
        something still worth going to look at.

        **Best effort, and correctness does not rest on it.** Some backing stores
        cannot withdraw an entry at all -- the Vertex AI Memory Bank
        implementation is a documented no-op, because deleting a memory reserves
        its id forever and its scope is immutable, so there is no way to retract
        one without making the thread permanently un-reindexable if it reopens.
        That is survivable precisely because the bank re-reads every match
        against the record and drops anything no longer open, so an entry this
        method failed to withdraw can still never reach a caller. What it costs
        is a wasted lookup and a diluted result window, which
        :data:`~firstdue.services.memory_bank.RECALL_OVERFETCH` pays for.
        """
        ...
