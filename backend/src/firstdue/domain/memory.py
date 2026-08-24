"""Durable memory -- the questions the fleet could not finish answering.

An agent that cannot finish a thought today loses it. ``records-watcher`` reads
a 2019 permit that cites a prior permit ``201804-3321``, cannot find that permit
anywhere in the published window, and the thread evaporates when the pass ends.
Next week it reads the same permit and fails identically. Nothing accumulates,
and when the missing record is finally published -- municipal records genuinely
arrive weeks or months late -- it lands in an empty room.

An :class:`OpenQuestion` is that thread, written down. It outlives a restart, a
redeploy, and a scale-to-zero, and it is picked back up when the record appears.

Four invariants are enforced *here*, not by convention in the service:

* **Ids are derived, never minted.** ``question_id`` is a pure function of
  (district, address, opener, normalised question). The same agent asking the
  same thing next week re-derives the same id, so the bank recognises the second
  ask as the first question rather than accumulating one duplicate per pass. A
  record whose id is not the derived one cannot be constructed at all.
* **A memory never holds document text.** See :data:`MAX_MEMORY_TEXT`.
* **A closed question says how it closed.** ``RESOLVED`` and ``ABANDONED`` both
  carry when, by whom, and what settled it. A question that reached a terminal
  state without saying why would be indistinguishable from one that was quietly
  dropped -- and "why did the system stop looking" is the question an
  investigator asks.
* **Recall is scope-gated.** Every memory carries a
  :class:`~firstdue.domain.enums.Classification`, and
  :func:`required_scope_for` says which scope reaches it. A question derived
  from a Tier II filing is invisible to a caller that does not hold
  ``read:tier-ii-metadata``, in the bank exactly as it is in the gateway.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firstdue.domain.enums import Classification, Scope
from firstdue.errors import ValidationError

#: The longest a remembered sentence may be.
#:
#: The project's rule is that a span which never held a document cannot leak
#: one. A memory holds ids, canonical keys, decisions, and a sentence somebody
#: could have written from memory -- it does not hold the permit. No validator
#: can tell prose from a quotation, but it can refuse a field long enough to be
#: one, and that is what this bound is: the point past which "a note about the
#: record" has become "a copy of the record".
#:
#: Deliberately shorter than :attr:`~firstdue.domain.facts.SourceSpan.quoted_text`
#: (1000). A provenance span exists to quote a document and is stored under the
#: fact it provenances; a memory has no such business, and lives in a store the
#: incident loop reads without a span's handling rules.
MAX_MEMORY_TEXT: Final[int] = 400

#: The longest a checkpoint's opaque graph state may serialize to.
#:
#: The same rule as :data:`MAX_MEMORY_TEXT`, applied to the one field that takes
#: an arbitrary shape. A LangGraph state that exhausted its budget is cursors,
#: counters, and identifiers -- kilobytes of them at the outside. A state large
#: enough to hold a scanned permit is one that is holding a scanned permit.
MAX_CHECKPOINT_STATE_BYTES: Final[int] = 16_384


class QuestionStatus(StrEnum):
    """Where a remembered thread stands.

    ``ABANDONED`` is distinct from ``RESOLVED`` because they answer different
    questions later. Resolved means the record arrived and settled it; abandoned
    means the clock ran out and nobody ever found out. Collapsing the two would
    make "we stopped looking" read as "there was nothing there", which is the
    same failure as rendering an unavailable source as an absence of hazard.
    """

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


#: The two terminal states. A question that reached one has an answer on file --
#: either what settled it, or the fact that nothing did.
CLOSED_STATUSES: Final[frozenset[QuestionStatus]] = frozenset(
    {QuestionStatus.RESOLVED, QuestionStatus.ABANDONED}
)


#: Which read scope reaches a memory of each statutory class.
#:
#: This is the gateway's vocabulary, not a parallel one. ``PUBLIC`` memories
#: follow the public records they were derived from; Tier II memories follow the
#: Tier II metadata scope, which is the one a standing grant may carry. ``PHI``
#: and ``RESTRICTED`` share ``read:ems-derived`` because they are exactly the
#: two classes :func:`~firstdue.gateway.engine.rule_standing_grant_cannot_reach_people`
#: refuses a standing grant: between incidents nothing in the fleet reaches
#: them, and inside an incident the derived read is the only door.
SCOPE_BY_CLASSIFICATION: Final[dict[Classification, Scope]] = {
    Classification.PUBLIC: Scope.READ_PUBLIC_RECORDS,
    Classification.TIER_II_CONFIDENTIAL: Scope.READ_TIER_II_METADATA,
    Classification.RESTRICTED: Scope.READ_EMS_DERIVED,
    Classification.PHI: Scope.READ_EMS_DERIVED,
}


def required_scope_for(classification: Classification) -> Scope | None:
    """The scope a caller must hold to be shown a memory of this class.

    ``None`` means no scope reaches it, and callers must read that as *deny*.
    The table above is total today; a statutory class added tomorrow without an
    entry becomes invisible to everyone rather than visible to anyone. Failing
    open here would be a leak, and failing closed is a bug report somebody files
    the same afternoon.
    """
    return SCOPE_BY_CLASSIFICATION.get(classification)


def is_visible_to(classification: Classification, scopes: Collection[Scope]) -> bool:
    """Whether a caller holding ``scopes`` may be shown this class of memory."""
    required = required_scope_for(classification)
    return required is not None and required in scopes


def normalise_question(question: str) -> str:
    """Collapse a question to the key two asks share when they are one ask.

    Deliberately shallow -- case and whitespace, nothing else. Two agents that
    phrase the same question with different spacing or capitalisation are asking
    one question. Two that phrase it differently are asking two, and no
    normaliser short of a model can tell those apart; a cleverer key would
    silently merge distinct threads, and a merged thread loses the one it
    swallowed. Carrying two near-duplicates is the cheaper mistake.
    """
    return " ".join(question.split()).casefold()


def derive_question_id(
    *,
    district_id: str,
    address_id: str | None,
    opened_by: str,
    question: str,
) -> str:
    """The id one open question always produces.

    Derived from the thread's natural key -- which district, which building (if
    any), which agent is asking, and what it is asking. Re-deriving it on the
    next pass is what makes reopening idempotent: the bank recognises the second
    ask as the same question instead of storing a fresh one every time the loop
    runs.

    The agent *version* is deliberately not part of it. An agent that ships a
    new version has not started asking a new question, and an id that rotated on
    deploy would lose every thread the fleet was holding.
    """
    material = "|".join((district_id, address_id or "", opened_by, normalise_question(question)))
    return f"mq_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def canonical_state(state: Mapping[str, Any]) -> str:
    """Serialize checkpoint state canonically, or refuse it.

    Canonical because a checkpoint is addressed by a hash of its contents:
    resuming the same graph twice must find one checkpoint, not two that differ
    by key order. Refusing is the other half -- state that will not serialize
    would be stored and then fail on the read, which is the worst moment to
    discover it.
    """
    try:
        payload = json.dumps(dict(state), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "checkpoint state must be JSON-serializable", details={"reason": type(exc).__name__}
        ) from exc
    size = len(payload.encode("utf-8"))
    if size > MAX_CHECKPOINT_STATE_BYTES:
        raise ValidationError(
            "checkpoint state is too large to be graph state; a memory never "
            "holds document contents",
            details={"bytes": size, "max": MAX_CHECKPOINT_STATE_BYTES},
        )
    return payload


def derive_checkpoint_id(
    *, question_id: str, agent_id: str, created_at: datetime, state: Mapping[str, Any]
) -> str:
    """The id one checkpoint always produces.

    A hash over what the checkpoint *is*: whose question, which agent, at which
    instant, holding what. Writing the same position twice -- a retry, a
    redelivered message -- therefore writes one document rather than two
    identical ones the resume path would have to choose between.
    """
    material = "|".join((question_id, agent_id, created_at.isoformat(), canonical_state(state)))
    return f"mckpt_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _require_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes.

    A memory outlives the process that wrote it by weeks. A naive timestamp
    would compare against an aware ``now`` with a ``TypeError`` on the expiry
    path -- and an expiry check that raises is an expiry check that never runs.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationError(f"{field_name} must be timezone-aware", details={"field": field_name})
    return value


def _require_short(value: str, field_name: str) -> str:
    """Bound one remembered sentence. See :data:`MAX_MEMORY_TEXT`."""
    if len(value) > MAX_MEMORY_TEXT:
        raise ValidationError(
            "a memory holds a short summary, never document contents",
            details={"field": field_name, "length": len(value), "max": MAX_MEMORY_TEXT},
        )
    return value


class OpenQuestion(BaseModel):
    """One thing an agent set out to establish and could not finish.

    Frozen, like every other record in the domain. A thread that changed under
    the agent reading it would make ``examined_count`` a number nobody could
    interpret; transitions return copies, and the store keeps the last one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    address_id: str | None = Field(default=None, max_length=120)

    #: What the agent is trying to establish, in a sentence.
    question: str = Field(min_length=1)
    #: Which agent is asking, at which pinned version. The version is recorded
    #: and never derived from today's build: a NIOSH replay has to say which
    #: code opened the thread, not which code happens to be deployed now.
    opened_by: str = Field(min_length=1, max_length=120)
    opened_by_version: str = Field(min_length=1, max_length=40)

    opened_at: datetime
    last_examined_at: datetime
    status: QuestionStatus = QuestionStatus.OPEN

    #: What would settle it -- the permit number, the filing, the survey.
    waiting_on: str = Field(min_length=1)
    #: What has been tried and eliminated, so the next pass does not retry it.
    #: This is the part that makes the memory cheaper than the search: without
    #: it, every pass re-walks the same dead ends at the same cost.
    ruled_out: tuple[str, ...] = ()
    #: Facts that bear on the question. Ids, never contents.
    evidence_fact_ids: tuple[str, ...] = ()

    #: Statutory handling class, inherited from whatever the question was
    #: derived from. Drives recall; see :func:`is_visible_to`.
    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    #: How many times an agent has looked at this thread, including the pass
    #: that opened it. One is the floor: a question nobody examined is one
    #: nobody opened.
    examined_count: int = Field(default=1, ge=1)

    #: When the thread stops being worth carrying. ``None`` means indefinitely,
    #: which is the right answer for a permit reference: the record may be two
    #: months late, and nobody knows in advance which.
    expires_at: datetime | None = None

    resolved_at: datetime | None = None
    resolved_by: str | None = Field(default=None, max_length=120)
    resolution: str | None = None

    @field_validator("opened_at", "last_examined_at", "expires_at", "resolved_at")
    @classmethod
    def _aware(cls, v: datetime | None, info: object) -> datetime | None:
        if v is None:
            return None
        return _require_aware(v, str(getattr(info, "field_name", "timestamp")))

    @field_validator("question", "waiting_on", "resolution")
    @classmethod
    def _short(cls, v: str | None, info: object) -> str | None:
        if v is None:
            return None
        return _require_short(v, str(getattr(info, "field_name", "text")))

    @field_validator("ruled_out")
    @classmethod
    def _short_eliminations(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for entry in v:
            _require_short(entry, "ruled_out")
        return v

    @model_validator(mode="after")
    def _check_derived_id(self) -> Self:
        """The id must be the one the natural key produces.

        Enforced on the record rather than trusted in the service, because the
        whole idempotency story rests on it: a hand-written id would store a
        second copy of a question the bank already holds, and the agent that
        opened it would never see what the first copy learned.
        """
        expected = derive_question_id(
            district_id=self.district_id,
            address_id=self.address_id,
            opened_by=self.opened_by,
            question=self.question,
        )
        if self.question_id != expected:
            raise ValidationError(
                "question_id must be derived from the question's natural key",
                details={"question_id": self.question_id, "expected": expected},
            )
        return self

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        if self.status in CLOSED_STATUSES:
            missing = [
                name
                for name, value in (
                    ("resolved_at", self.resolved_at),
                    ("resolved_by", self.resolved_by),
                    ("resolution", self.resolution),
                )
                if value is None
            ]
            if missing:
                raise ValidationError(
                    "a closed question must record how it closed",
                    details={"question_id": self.question_id, "missing": ",".join(missing)},
                )
        elif self.resolved_at or self.resolved_by or self.resolution:
            raise ValidationError(
                "an open question must not carry a resolution",
                details={"question_id": self.question_id},
            )
        if self.last_examined_at < self.opened_at:
            raise ValidationError(
                "a question cannot have been examined before it was opened",
                details={"question_id": self.question_id},
            )
        if len(set(self.ruled_out)) != len(self.ruled_out):
            raise ValidationError(
                "ruled_out must not repeat an elimination",
                details={"question_id": self.question_id},
            )
        return self

    # ------------------------------------------------------------- behaviour

    def _evolve(self, **update: Any) -> OpenQuestion:
        """Return the next version of this thread, revalidated.

        Not ``model_copy``. ``model_copy`` does not re-run validators, and a
        transition is exactly where unvalidated text arrives: the resolution
        that closes a question and the eliminations that accumulate on it are
        both written months after the record was created, by a different pass,
        and both are bounded for the same reason the question is. Every
        invariant on this class is therefore checked on every version of it,
        not only on the first.
        """
        return OpenQuestion.model_validate({**self.model_dump(), **update})

    @property
    def is_open(self) -> bool:
        return self.status is QuestionStatus.OPEN

    def is_expired(self, now: datetime) -> bool:
        """Whether the thread has outlived its window."""
        return self.expires_at is not None and now >= self.expires_at

    def is_visible_to(self, scopes: Collection[Scope]) -> bool:
        """Whether a caller holding ``scopes`` may be shown this memory."""
        return is_visible_to(self.classification, scopes)

    def examined(self, now: datetime) -> OpenQuestion:
        """Record that an agent looked at this thread again.

        Legal in every state, closed ones included. An agent that re-derives a
        question and finds it already answered *did* examine it, and that is
        precisely the pass the bank exists to make cheap: the count is of
        examinations, not of failures.
        """
        return self._evolve(
            last_examined_at=max(now, self.last_examined_at),
            examined_count=self.examined_count + 1,
        )

    def rule_out(self, *candidates: str, now: datetime) -> OpenQuestion:
        """Eliminate candidates so the next pass does not retry them.

        Additive and duplicate-tolerant: two passes that eliminate the same dead
        end recorded one elimination between them, and a pass that eliminated
        nothing new still counts as an examination.
        """
        eliminated = tuple(dict.fromkeys((*self.ruled_out, *(c for c in candidates if c.strip()))))
        return self.examined(now)._evolve(ruled_out=eliminated)

    def resolved(self, *, resolution: str, resolved_by: str, now: datetime) -> OpenQuestion:
        """Close the thread because something settled it.

        An ``ABANDONED`` question may still be resolved: the whole premise is
        that a municipal record can arrive after everybody stopped waiting, and
        a bank that refused the late answer would be refusing the case it was
        built for. What may not happen is re-resolving something already
        answered -- that would overwrite the record of what settled it first.
        """
        if self.status is QuestionStatus.RESOLVED:
            raise ValidationError(
                "question is already resolved", details={"question_id": self.question_id}
            )
        return self._evolve(
            status=QuestionStatus.RESOLVED,
            resolution=resolution,
            resolved_by=resolved_by,
            resolved_at=now,
            last_examined_at=max(now, self.last_examined_at),
            examined_count=self.examined_count + 1,
        )

    def abandoned(self, *, reason: str, abandoned_by: str, now: datetime) -> OpenQuestion:
        """Stop carrying the thread, and say so.

        Only an open question can be abandoned. Abandoning an answered one would
        replace the answer with a note saying nobody found one.
        """
        if self.status is not QuestionStatus.OPEN:
            raise ValidationError(
                "only an open question can be abandoned",
                details={"question_id": self.question_id, "status": str(self.status)},
            )
        return self._evolve(
            status=QuestionStatus.ABANDONED,
            resolution=reason,
            resolved_by=abandoned_by,
            resolved_at=now,
            last_examined_at=max(now, self.last_examined_at),
            examined_count=self.examined_count + 1,
        )


class MemoryCheckpoint(BaseModel):
    """One resumable position inside a graph that ran out of budget.

    Distinct from :class:`~firstdue.domain.runs.RunCheckpoint`, which is a
    position inside *one run* of an agent and dies with that run's record. This
    one belongs to the question, not to the run: the graph that exhausted its
    budget on Tuesday resumes on next week's pass, in a different process, from
    a different deployment.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str = Field(min_length=1, max_length=120)
    question_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    created_at: datetime

    #: Opaque graph state. Opaque to *this* module -- the bank does not
    #: interpret it -- but not unbounded; see :data:`MAX_CHECKPOINT_STATE_BYTES`.
    state: dict[str, Any] = Field(default_factory=dict)
    #: Inherited from the question. A checkpoint of a Tier II thread is a Tier II
    #: record, and resuming it is gated exactly as recalling the question is.
    classification: Classification

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "created_at")

    @model_validator(mode="after")
    def _check_derived_id(self) -> Self:
        expected = derive_checkpoint_id(
            question_id=self.question_id,
            agent_id=self.agent_id,
            created_at=self.created_at,
            state=self.state,
        )
        if self.checkpoint_id != expected:
            raise ValidationError(
                "checkpoint_id must be derived from the checkpoint's contents",
                details={"checkpoint_id": self.checkpoint_id, "expected": expected},
            )
        return self

    def is_visible_to(self, scopes: Collection[Scope]) -> bool:
        return is_visible_to(self.classification, scopes)
