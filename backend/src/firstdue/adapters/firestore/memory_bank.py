"""Firestore memory-bank repositories -- the enterprise store.

This is the adapter the component exists for. An open question has to outlive a
restart, a redeploy, and a scale-to-zero, and be picked back up when a municipal
record finally publishes weeks later; the in-memory adapter cannot do any of
that, and a Memory Bank that forgot on deploy would be a cache with a longer
docstring.

The invariants are the ones the in-memory adapter enforces, moved to where many
processes can be trusted with them: ``add`` uses ``create()`` so a duplicate
question id fails at the database rather than at a Python guard a second
instance could race past, and every read-modify-write runs inside a transaction.

**Document shape.** One document per question, keyed by the derived
``question_id``, following the codec used by every other Firestore repository
here: the model as one canonical JSON payload, with the fields the queries need
lifted out beside it. What is lifted is exactly what the composite indexes are
built on --

* ``open_questions``: ``(district_id ASC, status ASC, opened_at DESC)`` and
  ``(address_id ASC, status ASC, opened_at DESC)``
* ``memory_checkpoints``: ``(question_id ASC, created_at DESC)``

-- so the district-wide sweep the slow loop runs each pass and the
address-narrow read ``incident-recorder`` runs to close what the slow loop
opened are both single indexed queries rather than a collection scan. The
timestamps are lifted as native Firestore timestamps rather than the payload's
ISO strings, because an index has to order them and only the native type orders
correctly across offsets.
"""

from __future__ import annotations

from typing import Any

from firstdue.adapters.firestore.codec import decode, decode_all, encode
from firstdue.adapters.firestore.repositories import _commit, _Repository
from firstdue.domain.memory import MemoryCheckpoint, OpenQuestion, QuestionStatus
from firstdue.errors import AppendOnlyViolationError, NotFoundError


class FirestoreOpenQuestionRepository(_Repository):
    """Open questions. ``create`` is the enforcement, not a guard."""

    async def add(self, question: OpenQuestion) -> OpenQuestion:
        created = await self._store("open_questions").create(
            question.question_id, self._document(question)
        )
        if not created:
            raise AppendOnlyViolationError(
                "this question is already open; reopening is one question",
                details={"question_id": question.question_id},
            )
        return question

    async def get(self, question_id: str) -> OpenQuestion | None:
        document = await self._store("open_questions").get(question_id)
        return decode(OpenQuestion, document) if document else None

    async def save(self, question: OpenQuestion) -> OpenQuestion:
        """Persist a transition, refusing one that would undo an answer.

        The read and the write are one transaction because the slow loop and the
        incident loop touch the same thread from different processes: a pass
        that read the question while it was open and wrote back after somebody
        answered it would otherwise erase the answer and set the fleet looking
        again for something it already has.
        """
        store = self._store("open_questions")
        ref = store.ref(question.question_id)
        document = self._document(question)

        async def _save(transaction: Any) -> OpenQuestion:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFoundError(
                    "open question not found", details={"question_id": question.question_id}
                )
            stored = decode(OpenQuestion, snapshot.to_dict() or {})
            if (
                stored.status is QuestionStatus.RESOLVED
                and question.status is not QuestionStatus.RESOLVED
            ):
                raise AppendOnlyViolationError(
                    "a resolved question cannot be reopened by a stale write",
                    details={"question_id": question.question_id},
                )
            transaction.set(ref, document)
            return question

        result: OpenQuestion = await _commit(
            _save, store=store, entity=f"question {question.question_id}"
        )
        return result

    async def list_open(
        self, *, district_id: str, address_id: str | None = None
    ) -> tuple[OpenQuestion, ...]:
        """Open questions for a district, newest first.

        The equality filters are what the composite indexes serve; the ordering
        is applied in Python, as it is everywhere else in this adapter. A
        district carries tens of open threads, not thousands -- the scale limit
        that carries is recorded in ``docs/build-notes.md`` with the others.

        ``address_id`` narrows to one building rather than selecting the
        questions that have no address, and it uses the second index: the
        incident loop asks this form about the address it was dispatched to, and
        it must not pay for the district.
        """
        filters: list[tuple[str, str, Any]] = [
            ("status", "==", str(QuestionStatus.OPEN)),
        ]
        if address_id is None:
            filters.append(("district_id", "==", district_id))
        else:
            filters.append(("address_id", "==", address_id))
        documents = await self._store("open_questions").list(filters)
        questions = [
            question
            for question in decode_all(OpenQuestion, documents)
            if question.district_id == district_id
        ]
        questions.sort(key=lambda q: (q.opened_at, q.question_id), reverse=True)
        return tuple(questions)

    @staticmethod
    def _document(question: OpenQuestion) -> dict[str, Any]:
        return encode(
            question,
            question_id=question.question_id,
            district_id=question.district_id,
            address_id=question.address_id,
            status=str(question.status),
            opened_at=question.opened_at,
        )


class FirestoreCheckpointRepository(_Repository):
    """Graph positions, addressed by a hash of their contents."""

    async def put(self, checkpoint: MemoryCheckpoint) -> MemoryCheckpoint:
        """Store a position. An id that already exists is the same position.

        ``create`` rather than ``set``, and an existing id treated as success:
        the id is a hash of the contents, so a redelivered message writes the
        document it would have written anyway, and the resume path is never left
        choosing between two identical positions.
        """
        store = self._store("memory_checkpoints")
        created = await store.create(checkpoint.checkpoint_id, self._document(checkpoint))
        if created:
            return checkpoint
        existing = await store.get(checkpoint.checkpoint_id)
        if existing is None:  # pragma: no cover - create said it existed
            raise NotFoundError(
                "checkpoint vanished between create and read",
                details={"checkpoint_id": checkpoint.checkpoint_id},
            )
        return decode(MemoryCheckpoint, existing)

    async def latest(self, question_id: str) -> MemoryCheckpoint | None:
        documents = await self._store("memory_checkpoints").list(
            [("question_id", "==", question_id)]
        )
        checkpoints = decode_all(MemoryCheckpoint, documents)
        if not checkpoints:
            return None
        return max(checkpoints, key=lambda c: (c.created_at, c.checkpoint_id))

    @staticmethod
    def _document(checkpoint: MemoryCheckpoint) -> dict[str, Any]:
        return encode(
            checkpoint,
            checkpoint_id=checkpoint.checkpoint_id,
            question_id=checkpoint.question_id,
            agent_id=checkpoint.agent_id,
            created_at=checkpoint.created_at,
        )
