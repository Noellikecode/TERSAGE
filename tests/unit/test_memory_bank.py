"""The Memory Bank: idempotent threads, scope-gated recall, durable positions.

The security property has its own tests and is asserted directly: a memory
derived from a Tier II filing must not come back to a caller that does not hold
``read:tier-ii-metadata``, and it must not come back as part of a result the
caller was otherwise entitled to.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
from firstdue.domain.enums import Classification, Scope
from firstdue.domain.memory import (
    MAX_MEMORY_TEXT,
    OpenQuestion,
    QuestionStatus,
    derive_question_id,
)
from firstdue.errors import AppendOnlyViolationError, NotFoundError, ValidationError
from firstdue.services.memory_bank import MemoryBank

pytestmark = pytest.mark.invariant

#: What the slow loop actually gets stuck on: a permit that cites a permit
#: nobody has published yet.
QUESTION = "Does prior permit 201804-3321 record a third storey at 450 Hayes?"
WAITING_ON = "publication of permit 201804-3321 in the SF permits dataset"

PUBLIC_READER = frozenset({Scope.READ_PUBLIC_RECORDS})
TIER_II_READER = frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_TIER_II_METADATA})


@pytest.fixture
def questions() -> InMemoryOpenQuestionRepository:
    return InMemoryOpenQuestionRepository()


@pytest.fixture
def checkpoints() -> InMemoryCheckpointRepository:
    return InMemoryCheckpointRepository()


@pytest.fixture
def bank(questions, checkpoints, clock) -> MemoryBank:
    return MemoryBank(questions=questions, checkpoints=checkpoints, clock=clock)


async def _open(bank: MemoryBank, **overrides):
    payload = {
        "district_id": "sf-d05",
        "address_id": "sf-0450-hayes",
        "question": QUESTION,
        "waiting_on": WAITING_ON,
        "opened_by": "records-watcher",
        "opened_by_version": "1.2.0",
        "classification": Classification.PUBLIC,
    }
    payload.update(overrides)
    return await bank.open(**payload)


# ------------------------------------------------------------- derived ids


def test_question_id_is_the_same_id_forever() -> None:
    """Same natural key, same id -- across processes, releases, and years."""
    first = derive_question_id(
        district_id="sf-d05",
        address_id="sf-0450-hayes",
        opened_by="records-watcher",
        question=QUESTION,
    )
    second = derive_question_id(
        district_id="sf-d05",
        address_id="sf-0450-hayes",
        opened_by="records-watcher",
        # Reflowed and recapitalised by whatever composed it this pass. Still
        # the same question, so still the same thread.
        question="  DOES prior permit 201804-3321\n record a third storey\tat 450 Hayes? ".upper(),
    )
    assert first == second


def test_a_different_question_is_a_different_thread() -> None:
    other = derive_question_id(
        district_id="sf-d05",
        address_id="sf-0450-hayes",
        opened_by="records-watcher",
        question="Does the 2019 permit close out the 2016 violation?",
    )
    assert other != derive_question_id(
        district_id="sf-d05",
        address_id="sf-0450-hayes",
        opened_by="records-watcher",
        question=QUESTION,
    )


def test_a_hand_written_question_id_is_refused(epoch) -> None:
    """The id is not a field a caller fills in; it is a function of the key."""
    with pytest.raises(ValidationError):
        OpenQuestion(
            question_id="mq_whatever",
            district_id="sf-d05",
            address_id="sf-0450-hayes",
            question=QUESTION,
            opened_by="records-watcher",
            opened_by_version="1.2.0",
            opened_at=epoch,
            last_examined_at=epoch,
            waiting_on=WAITING_ON,
            classification=Classification.PUBLIC,
            confidence=0.4,
        )


# ------------------------------------------------------------ idempotency


async def test_reopening_the_same_question_is_one_record(bank, questions) -> None:
    """The next pass picks the thread back up instead of starting a new one."""
    first = await _open(bank)
    second = await _open(bank)

    assert second.question_id == first.question_id
    assert second.examined_count == 2
    assert first.examined_count == 1
    stored = await questions.list_open(district_id="sf-d05")
    assert len(stored) == 1


async def test_reopening_carries_forward_what_was_eliminated(bank) -> None:
    """The point of the memory: the second pass does not re-walk the dead ends."""
    opened = await _open(bank)
    await bank.rule_out(opened.question_id, "sf-permits 2018 window", "assessor 2018 roll")

    reopened = await _open(bank)
    assert reopened.ruled_out == ("sf-permits 2018 window", "assessor 2018 roll")
    assert reopened.examined_count == 3


async def test_eliminating_the_same_dead_end_twice_records_it_once(bank) -> None:
    opened = await _open(bank)
    await bank.rule_out(opened.question_id, "sf-permits 2018 window")
    again = await bank.rule_out(opened.question_id, "sf-permits 2018 window")
    assert again.ruled_out == ("sf-permits 2018 window",)


async def test_a_question_opened_for_another_agent_is_a_separate_thread(bank) -> None:
    await _open(bank)
    other = await _open(bank, opened_by="hazard-watcher")
    recalled = await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER)
    assert len(recalled) == 2
    assert other.examined_count == 1


# ------------------------------------------------- recall as a scope gate


async def test_a_tier_ii_memory_is_invisible_without_the_scope(bank) -> None:
    """The security property, asserted directly.

    A caller holding only ``read:public-records`` sees the public thread and is
    not told that the Tier II one exists.
    """
    await _open(
        bank,
        question="Is the ammonia filing current?",
        classification=Classification.PUBLIC,
    )
    tier_ii = await _open(
        bank,
        question="Which building on the block holds the anhydrous ammonia?",
        classification=Classification.TIER_II_CONFIDENTIAL,
    )

    public_view = await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER)
    assert tier_ii.question_id not in {q.question_id for q in public_view}
    assert len(public_view) == 1

    tier_ii_view = await bank.recall(district_id="sf-d05", scopes=TIER_II_READER)
    assert tier_ii.question_id in {q.question_id for q in tier_ii_view}
    assert len(tier_ii_view) == 2


async def test_a_tier_ii_memory_is_invisible_by_id_too(bank) -> None:
    """Recall is not the only door, so the gate is not only on recall."""
    question = await _open(bank, classification=Classification.TIER_II_CONFIDENTIAL)
    assert await bank.get(question.question_id, scopes=PUBLIC_READER) is None
    assert await bank.get(question.question_id, scopes=TIER_II_READER) is not None


async def test_person_level_memory_needs_the_derived_read_scope(bank) -> None:
    question = await _open(bank, classification=Classification.PHI)
    assert await bank.recall(district_id="sf-d05", scopes=TIER_II_READER) == ()
    recalled = await bank.recall(district_id="sf-d05", scopes=frozenset({Scope.READ_EMS_DERIVED}))
    assert [q.question_id for q in recalled] == [question.question_id]


async def test_recall_cannot_be_called_without_scopes(bank) -> None:
    """Filtering is the default because there is no unfiltered call to make."""
    with pytest.raises(TypeError):
        await bank.recall(district_id="sf-d05")  # type: ignore[call-arg]


async def test_recall_narrows_to_one_address(bank) -> None:
    await _open(bank)
    await _open(bank, address_id="sf-0500-hayes", question="Is 500 Hayes the same parcel?")
    narrowed = await bank.recall(
        district_id="sf-d05", address_id="sf-0500-hayes", scopes=PUBLIC_READER
    )
    assert [q.address_id for q in narrowed] == ["sf-0500-hayes"]


# ----------------------------------------------------------- transitions


async def test_resolve_closes_the_thread_and_says_what_settled_it(bank) -> None:
    opened = await _open(bank)
    resolved = await bank.resolve(
        opened.question_id,
        resolution="permit 201804-3321 published; it records two storeys",
        resolved_by="records-watcher",
    )
    assert resolved.status is QuestionStatus.RESOLVED
    assert resolved.resolved_by == "records-watcher"
    assert resolved.resolved_at is not None
    assert await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER) == ()


async def test_a_resolved_question_is_not_resolved_twice(bank) -> None:
    opened = await _open(bank)
    await bank.resolve(opened.question_id, resolution="settled", resolved_by="records-watcher")
    with pytest.raises(ValidationError):
        await bank.resolve(opened.question_id, resolution="settled again", resolved_by="someone")


async def test_abandon_records_that_nobody_ever_found_out(bank) -> None:
    opened = await _open(bank)
    abandoned = await bank.abandon(
        opened.question_id, reason="the window closed", abandoned_by="memory-caretaker"
    )
    assert abandoned.status is QuestionStatus.ABANDONED
    assert abandoned.resolution == "the window closed"
    assert await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER) == ()


async def test_an_abandoned_thread_can_still_be_settled_when_the_record_lands(bank) -> None:
    """The case the bank exists for: the filing was two months late."""
    opened = await _open(bank)
    await bank.abandon(
        opened.question_id, reason="the window closed", abandoned_by="memory-caretaker"
    )
    settled = await bank.resolve(
        opened.question_id,
        resolution="permit 201804-3321 published in the June window",
        resolved_by="records-watcher",
    )
    assert settled.status is QuestionStatus.RESOLVED


async def test_an_answered_question_is_not_abandoned(bank) -> None:
    opened = await _open(bank)
    await bank.resolve(opened.question_id, resolution="settled", resolved_by="records-watcher")
    with pytest.raises(ValidationError):
        await bank.abandon(opened.question_id, reason="gave up", abandoned_by="caretaker")


async def test_transitions_on_an_unknown_question_are_not_found(bank) -> None:
    with pytest.raises(NotFoundError):
        await bank.resolve("mq_nothing", resolution="x", resolved_by="y")


async def test_a_resolved_question_cannot_be_reopened_by_a_stale_write(
    bank, questions, clock, epoch
) -> None:
    """Two loops touch one thread; the one holding a stale copy must not win."""
    opened = await _open(bank)
    stale = opened.examined(epoch)
    await bank.resolve(opened.question_id, resolution="settled", resolved_by="records-watcher")
    with pytest.raises(AppendOnlyViolationError):
        await questions.save(stale)


# ---------------------------------------------------------------- expiry


async def test_an_expired_thread_is_not_recalled_before_the_sweep_runs(bank, clock, epoch) -> None:
    """Whether an agent picks up a dead thread must not depend on cron timing."""
    await _open(bank, expires_at=epoch + timedelta(days=30))
    assert len(await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER)) == 1

    clock.advance(timedelta(days=31))
    assert await bank.recall(district_id="sf-d05", scopes=PUBLIC_READER) == ()


async def test_the_sweep_abandons_what_outlived_its_window(bank, questions, clock, epoch) -> None:
    expiring = await _open(bank, expires_at=epoch + timedelta(days=30))
    await _open(bank, question="Is the hydrant on Octavia still in service?")

    clock.advance(timedelta(days=31))
    swept = await bank.abandon_expired(district_id="sf-d05", abandoned_by="memory-caretaker")

    assert [q.question_id for q in swept] == [expiring.question_id]
    assert swept[0].status is QuestionStatus.ABANDONED
    assert len(await questions.list_open(district_id="sf-d05")) == 1


# ----------------------------------------------------------- checkpoints


async def test_a_checkpoint_round_trips_through_the_repository(bank, clock) -> None:
    opened = await _open(bank)
    state = {"cursor": "page-4", "seen": ["permit/2019-1", "permit/2019-2"], "budget_left": 0}
    stored = await bank.checkpoint(opened.question_id, agent_id="records-watcher", state=state)

    resumed = await bank.resume(opened.question_id, scopes=PUBLIC_READER)
    assert resumed is not None
    assert resumed.checkpoint_id == stored.checkpoint_id
    assert resumed.state == state


async def test_resume_returns_the_most_recent_position(bank, clock) -> None:
    opened = await _open(bank)
    await bank.checkpoint(opened.question_id, agent_id="records-watcher", state={"cursor": "p1"})
    clock.advance(timedelta(minutes=5))
    await bank.checkpoint(opened.question_id, agent_id="records-watcher", state={"cursor": "p2"})

    resumed = await bank.resume(opened.question_id, scopes=PUBLIC_READER)
    assert resumed is not None
    assert resumed.state == {"cursor": "p2"}


async def test_writing_the_same_position_twice_stores_it_once(bank, checkpoints) -> None:
    opened = await _open(bank)
    first = await bank.checkpoint(
        opened.question_id, agent_id="records-watcher", state={"cursor": "p1"}
    )
    second = await bank.checkpoint(
        opened.question_id, agent_id="records-watcher", state={"cursor": "p1"}
    )
    assert first.checkpoint_id == second.checkpoint_id


async def test_a_never_checkpointed_question_resumes_from_nothing(bank) -> None:
    opened = await _open(bank)
    assert await bank.resume(opened.question_id, scopes=PUBLIC_READER) is None


async def test_a_checkpoint_inherits_the_question_classification(bank) -> None:
    """A caller cannot label its own checkpoint out of the scope gate."""
    opened = await _open(bank, classification=Classification.TIER_II_CONFIDENTIAL)
    stored = await bank.checkpoint(
        opened.question_id, agent_id="hazard-watcher", state={"cursor": "p1"}
    )
    assert stored.classification is Classification.TIER_II_CONFIDENTIAL
    assert await bank.resume(opened.question_id, scopes=PUBLIC_READER) is None
    assert await bank.resume(opened.question_id, scopes=TIER_II_READER) is not None


# ------------------------------------------- a memory holds no document text


async def test_an_over_long_question_is_refused(bank) -> None:
    with pytest.raises(ValidationError):
        await _open(bank, question="x" * (MAX_MEMORY_TEXT + 1))


async def test_an_over_long_waiting_on_is_refused(bank) -> None:
    with pytest.raises(ValidationError):
        await _open(bank, waiting_on="x" * (MAX_MEMORY_TEXT + 1))


async def test_an_over_long_resolution_is_refused(bank) -> None:
    opened = await _open(bank)
    with pytest.raises(ValidationError):
        await bank.resolve(
            opened.question_id,
            resolution="x" * (MAX_MEMORY_TEXT + 1),
            resolved_by="records-watcher",
        )


async def test_an_over_long_elimination_is_refused(bank) -> None:
    opened = await _open(bank)
    with pytest.raises(ValidationError):
        await bank.rule_out(opened.question_id, "x" * (MAX_MEMORY_TEXT + 1))


async def test_graph_state_large_enough_to_be_a_document_is_refused(bank) -> None:
    opened = await _open(bank)
    with pytest.raises(ValidationError):
        await bank.checkpoint(
            opened.question_id, agent_id="records-watcher", state={"scan": "x" * 20_000}
        )


async def test_graph_state_that_will_not_serialize_is_refused_on_the_way_in(bank) -> None:
    """Better here than on the read, months later, when it is needed."""
    opened = await _open(bank)
    with pytest.raises(ValidationError):
        await bank.checkpoint(
            opened.question_id, agent_id="records-watcher", state={"seen": {object()}}
        )
