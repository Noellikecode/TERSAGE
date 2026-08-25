"""In-memory thread recall: a second implementation, not a stub.

``make demo`` runs credential-free, so recall over open questions has to
actually recall something without a Google project behind it. This scores by
token overlap -- the same deterministic similarity
:class:`~firstdue.adapters.memory.vectors.InMemoryVectorIndex` uses over
narratives, reused rather than reinvented so the two cannot drift into different
notions of "near".

It is not an embedding and it does not pretend to be. What it shares with the
managed index is everything that matters architecturally: the same protocol, the
same classification refusal, the same district narrowing, the same
pointer-never-an-answer match shape, and the same rule that forgetting an absent
thread is a no-op.

What it does not share is durability. This is two dictionaries in the worker
process: it is per-instance, and it is gone on a restart or a scale-to-zero.
That is the honest difference between the two implementations and it is the
reason the managed one exists.
"""

from __future__ import annotations

import math

from firstdue.adapters.memory.vectors import _tokens
from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS
from firstdue.domain.threads import ThreadMemory
from firstdue.errors import ClassificationViolationError
from firstdue.ports.threads import ThreadMatch


class InMemoryThreadIndex:
    """Deterministic recall over the question threads the fleet is carrying."""

    def __init__(self) -> None:
        self._memories: dict[str, ThreadMemory] = {}
        self._tokens: dict[str, set[str]] = {}
        self.remembered = 0
        self.recalls = 0
        self.forgotten = 0

    @staticmethod
    def _guard(memory: ThreadMemory) -> None:
        """The gate, checked again at the boundary it protects.

        ``ThreadMemory`` refuses these at construction. Re-checking costs a set
        membership test and covers one reconstructed from stored JSON rather
        than built through the domain constructor.
        """
        if memory.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be handed to a managed recall index",
                details={
                    "classification": str(memory.classification),
                    "question_id": memory.question_id,
                },
            )

    async def remember(self, memory: ThreadMemory) -> None:
        self._guard(memory)
        # Keyed on question_id, so reopening replaces rather than accumulating
        # duplicates that would all match the same query.
        self._memories[memory.question_id] = memory
        self._tokens[memory.question_id] = _tokens(memory.text)
        self.remembered += 1

    async def recall_similar(
        self, text: str, *, district_id: str, limit: int = 5
    ) -> tuple[ThreadMatch, ...]:
        self.recalls += 1
        wanted = _tokens(text)
        if not wanted:
            return ()

        scored: list[tuple[float, str, ThreadMemory]] = []
        for question_id, tokens in self._tokens.items():
            memory = self._memories[question_id]
            # District narrowing happens here rather than in the caller: a
            # cross-district match is a thread from a jurisdiction this caller
            # may have no standing in, not merely a less relevant one.
            if memory.district_id != district_id or not tokens:
                continue
            shared = len(wanted & tokens)
            if not shared:
                continue
            similarity = shared / math.sqrt(len(wanted) * len(tokens))
            scored.append((similarity, question_id, memory))

        # Sorted by distance, then id: ties resolve the same way on every run,
        # which is what makes the demo reproducible.
        scored.sort(key=lambda row: (-row[0], row[1]))
        return tuple(
            ThreadMatch(
                question_id=question_id,
                district_id=memory.district_id,
                distance=round(1.0 - similarity, 6),
            )
            for similarity, question_id, memory in scored[:limit]
        )

    async def forget(self, question_id: str) -> None:
        if self._memories.pop(question_id, None) is not None:
            self._tokens.pop(question_id, None)
            self.forgotten += 1
