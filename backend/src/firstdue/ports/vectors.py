"""Semantic recall protocol.

Structured facts live in Firestore and are what the system *believes*. This is
the other half of the memory bank: the narratives those facts were read out of,
searchable by meaning rather than by key.

The distinction matters more here than anywhere else in the system. A vector
match is **not a fact**. It has no canonical key of its own, no merge
precedence, and no confidence that decays -- it is a pointer to a filing
somebody once wrote, offered so an officer can go read it. Nothing downstream
may turn a match into an assertion about a building, and nothing here returns
text: a match carries the ids that lead back to the record, and the record is
where the words are.

``PHI`` and ``TIER_II_CONFIDENTIAL`` never reach this layer at all. That is
enforced at payload construction, again at the adapter boundary, and again by
the implementations below.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.vectors import VectorPayload


class VectorMatch(BaseModel):
    """One neighbour, with the ids that trace it back to a filing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: str = Field(min_length=1, max_length=120)
    #: Lower is nearer. The unit is the index's, and it is shown, not compared
    #: across indexes.
    distance: float
    #: Where the text came from, so a match is followable.
    source_ref: str = Field(default="", max_length=400)


@runtime_checkable
class VectorIndex(Protocol):
    """Semantic recall over narratives the system has already screened."""

    async def upsert(self, payloads: Sequence[VectorPayload]) -> int:
        """Embed and store. Returns how many were written.

        Raises:
            ClassificationViolationError: for any payload carrying a
                classification that may never be embedded.
        """
        ...

    async def query(self, text: str, *, limit: int = 5) -> tuple[VectorMatch, ...]:
        """Nearest neighbours for a query string, nearest first."""
        ...
