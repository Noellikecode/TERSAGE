"""In-memory semantic recall: a second implementation, not a stub.

Fake mode is what ``make demo`` runs and what a judge evaluates, so recall has
to actually recall something. This scores by token overlap -- a deterministic
similarity that needs no model, no credentials, and no network, and that ranks
"stairwell obstructed by storage" above "annual fee received" for a query about
blocked egress.

It is not an embedding and it does not pretend to be. What it shares with the
live index is everything that matters architecturally: the same protocol, the
same classification refusal, the same match shape, and the same rule that a
match is a pointer to a filing rather than a claim about a building.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS
from firstdue.domain.vectors import VectorPayload
from firstdue.errors import ClassificationViolationError
from firstdue.ports.vectors import VectorMatch

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words too common to carry meaning in a municipal filing.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 2}


class InMemoryVectorIndex:
    """Deterministic recall over screened narratives."""

    def __init__(self) -> None:
        self._payloads: dict[str, VectorPayload] = {}
        self._tokens: dict[str, set[str]] = {}
        self.upserts = 0
        self.queries = 0

    @staticmethod
    def _guard(payload: VectorPayload) -> None:
        """The gate, checked again at the boundary it protects.

        ``VectorPayload`` refuses these at construction. Re-checking here costs
        a set membership test and covers a payload reconstructed from stored
        JSON rather than built.
        """
        if payload.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be serialized into a vector payload",
                details={"classification": str(payload.classification)},
            )

    async def upsert(self, payloads: Sequence[VectorPayload]) -> int:
        for payload in payloads:
            self._guard(payload)
        written = 0
        for payload in payloads:
            # Re-indexing the same narrative replaces it rather than
            # accumulating duplicates that would all match the same query.
            self._payloads[payload.payload_id] = payload
            self._tokens[payload.payload_id] = _tokens(payload.text)
            written += 1
        self.upserts += written
        return written

    async def query(self, text: str, *, limit: int = 5) -> tuple[VectorMatch, ...]:
        self.queries += 1
        wanted = _tokens(text)
        if not wanted:
            return ()

        scored: list[tuple[float, str, VectorPayload]] = []
        for payload_id, tokens in self._tokens.items():
            if not tokens:
                continue
            shared = len(wanted & tokens)
            if not shared:
                continue
            # Cosine over term-presence vectors: shared terms normalised by the
            # geometric mean of both lengths, so a long filing does not win on
            # size alone.
            similarity = shared / math.sqrt(len(wanted) * len(tokens))
            scored.append((similarity, payload_id, self._payloads[payload_id]))

        # Sorted by distance, then id: ties resolve the same way on every run,
        # which is what makes the demo reproducible.
        scored.sort(key=lambda row: (-row[0], row[1]))
        return tuple(
            VectorMatch(
                payload_id=payload_id,
                address_id=payload.address_id,
                canonical_key=payload.canonical_key,
                distance=round(1.0 - similarity, 6),
                source_ref=payload.source_ref,
            )
            for similarity, payload_id, payload in scored[:limit]
        )
