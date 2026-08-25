"""What of an open question may be handed to a managed recall service.

The memory bank keeps two different things about one thread, and this module is
the boundary between them.

**The record stays ours.** Everything an :class:`~firstdue.domain.memory.OpenQuestion`
accumulates -- what it has ruled out, which facts it rests on, how many passes
have examined it, whether it resolved and what settled it -- is a state machine
with transitions the fleet checks, and it lives in the repositories behind
:mod:`firstdue.ports.memory`. Nothing here replaces it and nothing here is read
back as truth.

**The prose is what gets indexed.** A thread is also a sentence somebody could
search for: *"does permit 201804-3321 exist in the published window?"*. That
sentence, and the sentence saying what the thread is waiting on, are what a
managed memory service is actually good at -- retrieval by meaning rather than
by key, which :meth:`~firstdue.services.memory_bank.MemoryBank.recall` cannot do
because ``list_open`` narrows by district and address and nothing else.

Three limits are enforced at construction rather than at the adapter, so a
payload that could not be stored cannot be built in the first place.

**Sensitive classifications never reach a managed embedding service.** The same
gate the vector layer applies -- :data:`~firstdue.domain.enums.VECTOR_FORBIDDEN_CLASSIFICATIONS`
-- applies here and for the same reason, sharpened by one fact about this
particular service: writing a memory *embeds its text*, so a confidential Tier II
filing handed to it would be sent to an embedding model and stored outside the
project's own database. A question raised by such a filing stays in Firestore,
recallable by district the way it always was, and is simply absent from the
index. That is a smaller capability, not a quieter one: :class:`ThreadMemory`
raises rather than dropping the payload, and the caller records the refusal.

**A remembered sentence is bounded.** :data:`~firstdue.domain.memory.MAX_MEMORY_TEXT`
already bounds every sentence a question carries, and the service imposes its
own ceiling on a stored fact. :data:`MAX_INDEXED_TEXT` is the smaller of the two
and is checked here, so the failure is a validation error at the boundary rather
than an ``InvalidArgument`` from a remote API weeks into a district's history.

**Text is prose, never state.** There is deliberately no field on this model that
a serialized graph checkpoint, a fact id list, or an eliminations list could
travel in. What the index holds is a question and what it waits on; what the
question *knows* is not the index's business, and a payload that carried it
would put the state machine in two places with no rule for which one wins.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS, Classification
from firstdue.domain.memory import MAX_MEMORY_TEXT, OpenQuestion
from firstdue.errors import ClassificationViolationError, ValidationError

#: The ceiling on indexed prose.
#:
#: Vertex AI Memory Bank refuses a ``Memory.fact`` longer than 2048 characters,
#: verified against the live service rather than read off a doc page. This sits
#: below that with room for two things: the join between the two sentences, and
#: the identifying tag the Vertex adapter prefixes so a retrieved match can name
#: the thread it came from (see :mod:`firstdue.adapters.vertex.threads` -- the
#: retrieval response carries neither the resource id nor the display name, so
#: the id has to travel in the text). A ``question_id`` is at most 120
#: characters, so the worst case is comfortably inside the service ceiling.
#:
#: It is also far above ``2 * MAX_MEMORY_TEXT`` -- the largest a question and its
#: ``waiting_on`` can be -- so the bound the domain already enforces is the one
#: that actually binds, and this is a backstop rather than a second policy.
MAX_INDEXED_TEXT: Final[int] = 1800

#: How the two sentences are joined into one indexed string. A separator rather
#: than a space, so the two halves stay legible to a human reading a match.
_JOIN: Final[str] = " -- waiting on: "


class ThreadMemory(BaseModel):
    """One question's prose, ready to hand to a recall index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    address_id: str | None = Field(default=None, max_length=120)
    #: The question and what it waits on, joined. Prose only -- see the module
    #: docstring on why no state field exists here.
    text: str = Field(min_length=1, max_length=MAX_INDEXED_TEXT)
    classification: Classification
    #: Which agent is carrying the thread, so a match can say who to ask.
    opened_by: str = Field(min_length=1, max_length=120)
    opened_at: datetime

    @model_validator(mode="after")
    def _refuse_forbidden_classifications(self) -> Self:
        """PHI and Tier II are never handed to a managed embedding service."""
        if self.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be handed to a managed recall index",
                details={
                    "classification": str(self.classification),
                    "question_id": self.question_id,
                },
            )
        return self


def indexable(question: OpenQuestion) -> bool:
    """Whether this thread's prose may be handed to the index at all.

    Separate from :class:`ThreadMemory` on purpose. The model *raises* for a
    forbidden classification, because a caller that built one meant to store it;
    this answers the prior question -- should we even try -- so the memory bank
    can skip a Tier II thread as a matter of routine without treating routine as
    an error. Both read the one list, so they cannot disagree.
    """
    return question.classification not in VECTOR_FORBIDDEN_CLASSIFICATIONS


def build_thread_memory(question: OpenQuestion) -> ThreadMemory:
    """Derive the indexable prose from a stored question.

    Raises:
        ClassificationViolationError: for ``PHI`` and ``TIER_II_CONFIDENTIAL``.
            Call :func:`indexable` first when a skip is the intended behaviour.
        ValidationError: when the joined prose exceeds :data:`MAX_INDEXED_TEXT`.
            Both halves are already bounded by ``MAX_MEMORY_TEXT``, so this is
            reachable only if that bound is raised without revisiting this one.
    """
    text = f"{question.question}{_JOIN}{question.waiting_on}"
    if len(text) > MAX_INDEXED_TEXT:  # pragma: no cover - guarded by MAX_MEMORY_TEXT
        raise ValidationError(
            "joined question prose exceeds what the recall index accepts",
            details={
                "length": len(text),
                "max": MAX_INDEXED_TEXT,
                "sentence_max": MAX_MEMORY_TEXT,
                "question_id": question.question_id,
            },
        )
    return ThreadMemory(
        question_id=question.question_id,
        district_id=question.district_id,
        address_id=question.address_id,
        text=text,
        classification=question.classification,
        opened_by=question.opened_by,
        opened_at=question.opened_at,
    )
