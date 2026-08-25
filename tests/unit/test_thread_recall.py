"""Recall over open question threads, and the line between the two stores.

The memory bank keeps the record in the repositories and mirrors the prose into
a semantic index. These tests hold that split: the index may point, it may not
assert, it may not decide who sees what, and it may not take the fleet down when
it breaks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
from firstdue.adapters.memory.threads import InMemoryThreadIndex
from firstdue.domain.enums import Classification, Scope
from firstdue.domain.threads import (
    MAX_INDEXED_TEXT,
    ThreadMemory,
    build_thread_memory,
    indexable,
)
from firstdue.errors import ClassificationViolationError
from firstdue.ports.threads import ThreadIndex
from firstdue.services.memory_bank import MemoryBank

pytestmark = pytest.mark.anyio

EPOCH = datetime(2026, 3, 4, 8, 0, tzinfo=UTC)

PUBLIC_SCOPES = frozenset({Scope.READ_PUBLIC_RECORDS})
TIER_II_SCOPES = frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_TIER_II_METADATA})


@pytest.fixture
def index() -> InMemoryThreadIndex:
    return InMemoryThreadIndex()


@pytest.fixture
def questions() -> InMemoryOpenQuestionRepository:
    return InMemoryOpenQuestionRepository()


@pytest.fixture
def bank(questions, index, clock) -> MemoryBank:
    return MemoryBank(
        questions=questions,
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
        threads=index,
    )


async def _open(bank: MemoryBank, **overrides):
    payload = {
        "district_id": "sf-d7",
        "question": "does permit 201804-3321 exist in the published window",
        "waiting_on": "sf-permits publication of 201804-3321",
        "opened_by": "records-watcher",
        "opened_by_version": "1.0.0",
        "classification": Classification.PUBLIC,
        "address_id": "addr-1",
    }
    payload.update(overrides)
    return await bank.open(**payload)


# ------------------------------------------------------ the payload boundary


def test_the_indexed_text_is_the_question_and_what_it_waits_on() -> None:
    """A match a human reads should say both halves, not just the question."""
    question = _question()
    memory = build_thread_memory(question)
    assert question.question in memory.text
    assert question.waiting_on in memory.text


@pytest.mark.parametrize(
    "classification", [Classification.PHI, Classification.TIER_II_CONFIDENTIAL]
)
def test_a_forbidden_classification_cannot_be_built_into_a_payload(classification) -> None:
    """Writing a memory embeds it, so the embedding gate applies here too."""
    with pytest.raises(ClassificationViolationError):
        ThreadMemory(
            question_id="q-1",
            district_id="sf-d7",
            text="anything at all",
            classification=classification,
            opened_by="hazard-watcher",
            opened_at=EPOCH,
        )


@pytest.mark.parametrize(
    "classification", [Classification.PHI, Classification.TIER_II_CONFIDENTIAL]
)
def test_indexable_agrees_with_the_constructor(classification) -> None:
    """Two readers of one list. A disagreement would be a silent leak or a stall."""
    assert not indexable(_question(classification=classification))
    with pytest.raises(ClassificationViolationError):
        build_thread_memory(_question(classification=classification))


def test_a_public_thread_is_indexable() -> None:
    assert indexable(_question())


def test_the_ceiling_sits_under_what_the_service_accepts() -> None:
    """2048 is the live service's limit. This is the bound we enforce first."""
    assert MAX_INDEXED_TEXT < 2048


# ------------------------------------------------------------- the index ---


async def test_a_remembered_thread_is_recalled_by_meaning(index) -> None:
    await index.remember(_memory("q-1", "attic conversion permit never signed off"))
    await index.remember(_memory("q-2", "annual inspection fee received"))

    found = await index.recall_similar("unsigned attic conversion", district_id="sf-d7")

    assert [m.question_id for m in found] == ["q-1"]


async def test_recall_does_not_cross_a_district(index) -> None:
    """A thread from another jurisdiction is not merely less relevant."""
    await index.remember(_memory("q-1", "blocked stairwell storage", district_id="sf-d7"))
    await index.remember(_memory("q-2", "blocked stairwell storage", district_id="sf-d9"))

    found = await index.recall_similar("blocked stairwell", district_id="sf-d7")

    assert [m.question_id for m in found] == ["q-1"]


async def test_remembering_the_same_thread_twice_stores_it_once(index) -> None:
    """Reopening is one thread. Two entries would both match one query."""
    await index.remember(_memory("q-1", "attic conversion permit"))
    await index.remember(_memory("q-1", "attic conversion permit, now also unsigned"))

    found = await index.recall_similar("attic conversion", district_id="sf-d7")

    assert [m.question_id for m in found] == ["q-1"]


async def test_a_forgotten_thread_stops_matching(index) -> None:
    await index.remember(_memory("q-1", "attic conversion permit"))
    await index.forget("q-1")

    assert await index.recall_similar("attic conversion", district_id="sf-d7") == ()


async def test_forgetting_a_thread_that_was_never_there_is_not_an_error(index) -> None:
    await index.forget("q-never")


async def test_the_index_refuses_a_forbidden_classification_at_its_own_boundary(index) -> None:
    """Re-checked here, for a payload rebuilt from stored JSON rather than built."""
    smuggled = ThreadMemory.model_construct(
        question_id="q-1",
        district_id="sf-d7",
        address_id=None,
        text="tier two chemical inventory",
        classification=Classification.TIER_II_CONFIDENTIAL,
        opened_by="hazard-watcher",
        opened_at=EPOCH,
    )
    with pytest.raises(ClassificationViolationError):
        await index.remember(smuggled)


async def test_the_in_memory_index_satisfies_the_port(index) -> None:
    assert isinstance(index, ThreadIndex)


# ------------------------------------------------- the bank drives the index


async def test_opening_a_question_indexes_its_prose(bank, index) -> None:
    opened = await _open(bank)

    found = await index.recall_similar("permit 201804-3321", district_id="sf-d7")

    assert [m.question_id for m in found] == [opened.question_id]


async def test_resolving_a_thread_drops_its_pointer(bank, index) -> None:
    """The record is kept forever. It is the pointer that goes."""
    opened = await _open(bank)
    await bank.resolve(
        opened.question_id, resolution="the permit published", resolved_by="records-watcher"
    )

    assert await index.recall_similar("permit 201804-3321", district_id="sf-d7") == ()


async def test_abandoning_a_thread_drops_its_pointer(bank, index) -> None:
    opened = await _open(bank)
    await bank.abandon(
        opened.question_id, reason="nobody ever published it", abandoned_by="records-watcher"
    )

    assert await index.recall_similar("permit 201804-3321", district_id="sf-d7") == ()


async def test_a_tier_ii_thread_is_stored_but_never_indexed(bank, index, questions) -> None:
    """A smaller capability, not a quieter one: the skip is counted."""
    opened = await _open(bank, classification=Classification.TIER_II_CONFIDENTIAL)

    assert await questions.get(opened.question_id) is not None
    assert await index.recall_similar("permit 201804-3321", district_id="sf-d7") == ()
    assert bank.index_skipped == 1


async def test_recall_similar_returns_the_stored_records(bank) -> None:
    opened = await _open(bank)

    found = await bank.recall_similar(
        "permit 201804-3321", district_id="sf-d7", scopes=PUBLIC_SCOPES
    )

    assert [q.question_id for q in found] == [opened.question_id]


async def test_recall_similar_regates_on_scopes_against_the_record(bank, index) -> None:
    """The index points; the record decides. A pointer is not an authorization."""
    opened = await _open(bank, classification=Classification.TIER_II_CONFIDENTIAL)
    # Force a pointer the bank would never have written, to prove the gate is
    # applied on the way out rather than only on the way in.
    await index.remember(
        _memory(opened.question_id, "tier two inventory question", district_id="sf-d7")
    )

    assert (
        await bank.recall_similar("tier two inventory", district_id="sf-d7", scopes=PUBLIC_SCOPES)
        == ()
    )

    visible = await bank.recall_similar(
        "tier two inventory", district_id="sf-d7", scopes=TIER_II_SCOPES
    )
    assert [q.question_id for q in visible] == [opened.question_id]


async def test_a_stale_pointer_to_a_closed_thread_is_dropped(bank, index, questions) -> None:
    """An index that fell behind costs a lookup, never a wrong answer."""
    opened = await _open(bank)
    await questions.save(
        (await questions.get(opened.question_id)).resolved(
            resolution="settled", resolved_by="incident-recorder", now=EPOCH
        )
    )
    # The pointer survives; the record says the thread is closed.
    await index.remember(_memory(opened.question_id, "permit 201804-3321 published"))

    assert (
        await bank.recall_similar("permit 201804-3321", district_id="sf-d7", scopes=PUBLIC_SCOPES)
        == ()
    )


async def test_an_expired_thread_is_not_recalled_by_meaning_either(bank, index, clock) -> None:
    """The same rule structural recall applies, applied on the same read."""
    opened = await _open(bank, expires_at=EPOCH + timedelta(days=1))
    await index.remember(_memory(opened.question_id, "permit 201804-3321 window"))
    clock.advance(timedelta(days=2))

    assert (
        await bank.recall_similar("permit 201804-3321", district_id="sf-d7", scopes=PUBLIC_SCOPES)
        == ()
    )


async def test_recall_similar_cannot_be_called_without_scopes(bank) -> None:
    with pytest.raises(TypeError):
        await bank.recall_similar("anything", district_id="sf-d7")  # type: ignore[call-arg]


# ------------------------------------------------------- failure is not loss


class _BrokenIndex:
    """Every verb fails. The bank must survive all of them."""

    async def remember(self, memory: ThreadMemory) -> None:
        raise RuntimeError("memory bank unreachable")

    async def recall_similar(self, text, *, district_id, limit=5):
        raise RuntimeError("memory bank unreachable")

    async def forget(self, question_id: str) -> None:
        raise RuntimeError("memory bank unreachable")


async def test_a_broken_index_does_not_stop_a_question_being_opened(questions, clock) -> None:
    """The one thing the memory bank exists to keep doing."""
    bank = MemoryBank(
        questions=questions,
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
        threads=_BrokenIndex(),
    )

    opened = await _open(bank)

    assert await questions.get(opened.question_id) is not None
    assert bank.index_failed == 1


async def test_a_broken_index_does_not_stop_a_thread_closing(questions, clock) -> None:
    bank = MemoryBank(
        questions=questions,
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
        threads=_BrokenIndex(),
    )
    opened = await _open(bank)

    closed = await bank.resolve(
        opened.question_id, resolution="settled", resolved_by="incident-recorder"
    )

    assert closed.resolution == "settled"


async def test_a_broken_index_recalls_nothing_rather_than_raising(questions, clock) -> None:
    bank = MemoryBank(
        questions=questions,
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
        threads=_BrokenIndex(),
    )

    assert await bank.recall_similar("anything", district_id="sf-d7", scopes=PUBLIC_SCOPES) == ()
    assert bank.index_failed >= 1


async def test_a_bank_with_no_index_still_opens_and_recalls_structurally(questions, clock) -> None:
    """``None`` is a configuration, not a degradation of the record."""
    bank = MemoryBank(
        questions=questions,
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
    )

    opened = await _open(bank)

    assert await bank.recall_similar("anything", district_id="sf-d7", scopes=PUBLIC_SCOPES) == ()
    assert [
        q.question_id for q in await bank.recall(district_id="sf-d7", scopes=PUBLIC_SCOPES)
    ] == [opened.question_id]


# ------------------------------------------------------------------ helpers


def _question(*, classification: Classification = Classification.PUBLIC):
    from firstdue.domain.memory import OpenQuestion, derive_question_id

    question = "does permit 201804-3321 exist in the published window"
    return OpenQuestion(
        question_id=derive_question_id(
            district_id="sf-d7",
            address_id="addr-1",
            opened_by="records-watcher",
            question=question,
        ),
        district_id="sf-d7",
        address_id="addr-1",
        question="does permit 201804-3321 exist in the published window",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        opened_at=EPOCH,
        last_examined_at=EPOCH,
        waiting_on="sf-permits publication of 201804-3321",
        classification=classification,
        confidence=0.5,
    )


def _memory(question_id: str, text: str, *, district_id: str = "sf-d7") -> ThreadMemory:
    return ThreadMemory(
        question_id=question_id,
        district_id=district_id,
        address_id="addr-1",
        text=text,
        classification=Classification.PUBLIC,
        opened_by="records-watcher",
        opened_at=EPOCH,
    )
