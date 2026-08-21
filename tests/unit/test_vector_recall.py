"""Semantic recall: the other half of the memory bank.

``VertexVectorIndex`` existed and was never constructed by anything, so the
PRD's "semantic recall over inspection narratives and survey notes" was an
adapter with no caller. These tests cover the in-memory index that fake mode
runs on, the classification gate that keeps confidential filings out of it, and
the boundary that keeps a match from ever becoming a fact.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.adapters.memory.vectors import InMemoryVectorIndex
from firstdue.domain.enums import Classification
from firstdue.domain.keys import Keys
from firstdue.domain.vectors import VectorPayload
from firstdue.errors import ClassificationViolationError
from firstdue.ports.vectors import VectorIndex

OBSERVED = datetime(2026, 3, 1, tzinfo=UTC)


def _payload(
    payload_id: str,
    text: str,
    *,
    address_id: str = "sf-0450-hayes",
    classification: Classification = Classification.PUBLIC,
) -> VectorPayload:
    return VectorPayload(
        payload_id=payload_id,
        address_id=address_id,
        canonical_key=Keys.NARRATIVE,
        text=text,
        classification=classification,
        source_ref=f"violation/{payload_id}",
        observed_at=OBSERVED,
    )


STAIRWELL = _payload("v1", "Rear stairwell partially obstructed by stored furniture and boxes.")
TRUSS = _payload("v2", "Lightweight parallel chord truss floor assembly over the basement.")
FEE = _payload("v3", "Annual billable inspection fee received and processed by the bureau.")


async def _index() -> InMemoryVectorIndex:
    index = InMemoryVectorIndex()
    await index.upsert((STAIRWELL, TRUSS, FEE))
    return index


def test_the_memory_index_satisfies_the_port() -> None:
    assert isinstance(InMemoryVectorIndex(), VectorIndex)


async def test_recall_ranks_the_relevant_filing_first() -> None:
    """A stub that returned nothing would pass a type check and no test."""
    index = await _index()
    matches = await index.query("blocked stairwell storage", limit=3)
    assert matches
    assert matches[0].payload_id == "v1"


async def test_an_unrelated_filing_does_not_surface() -> None:
    index = await _index()
    matches = await index.query("truss floor assembly", limit=3)
    assert matches[0].payload_id == "v2"
    assert "v3" not in {match.payload_id for match in matches}


async def test_recall_is_deterministic() -> None:
    """The demo has to produce the same answer on every run."""
    index = await _index()
    first = await index.query("stairwell obstructed", limit=3)
    second = await index.query("stairwell obstructed", limit=3)
    assert first == second


async def test_recall_respects_its_limit() -> None:
    index = await _index()
    assert len(await index.query("inspection stairwell truss fee", limit=1)) == 1


async def test_a_query_with_nothing_in_it_recalls_nothing() -> None:
    index = await _index()
    assert await index.query("the and of", limit=5) == ()


async def test_reindexing_the_same_filing_replaces_it() -> None:
    """Otherwise one narrative matches a query several times over."""
    index = await _index()
    await index.upsert((STAIRWELL,))
    matches = await index.query("stairwell obstructed", limit=10)
    assert [m.payload_id for m in matches].count("v1") == 1


async def test_a_match_carries_ids_and_never_text() -> None:
    """A match is a pointer to a filing, not a claim about a building."""
    index = await _index()
    match = (await index.query("stairwell obstructed", limit=1))[0]
    assert match.source_ref
    assert match.address_id
    assert not hasattr(match, "text")
    assert "value" not in match.model_dump()


# ------------------------------------------------------- the classification gate


@pytest.mark.parametrize(
    "classification", [Classification.PHI, Classification.TIER_II_CONFIDENTIAL]
)
def test_a_forbidden_classification_cannot_even_be_built(
    classification: Classification,
) -> None:
    """The gate is at construction, so there is no path that embeds by omission."""
    with pytest.raises(ClassificationViolationError):
        _payload("x", "confidential inventory", classification=classification)


async def test_the_index_refuses_a_reconstructed_forbidden_payload() -> None:
    """The gate again at the boundary it protects.

    A payload rebuilt from stored JSON bypasses the constructor check, so the
    adapter re-checks. Two independent refusals, because one leak is permanent.
    """
    index = InMemoryVectorIndex()
    smuggled = _payload("x", "chemical storage location")
    object.__setattr__(smuggled, "classification", Classification.TIER_II_CONFIDENTIAL)
    with pytest.raises(ClassificationViolationError):
        await index.upsert((smuggled,))


async def test_a_refused_batch_writes_nothing() -> None:
    """One confidential payload must not half-write the batch around it."""
    index = InMemoryVectorIndex()
    smuggled = _payload("x", "chemical storage location")
    object.__setattr__(smuggled, "classification", Classification.PHI)
    with pytest.raises(ClassificationViolationError):
        await index.upsert((STAIRWELL, smuggled))
    assert await index.query("stairwell obstructed", limit=5) == ()
