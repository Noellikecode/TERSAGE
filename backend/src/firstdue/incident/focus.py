"""What each woken agent should look at first, said only in references.

``plan_handoffs`` decides *who* is woken. This module carries the other half of
the same sentence: *what each of them should look at first*. The two are kept
apart deliberately -- routing is an authorisation decision and is a rule table
(see :mod:`firstdue.incident.handoff`), while attention is a reading decision
and is the one place in the incident loop where a model is allowed to reason
over what the slow loop spent weeks accumulating.

**A pointer carries a reference, never a value.** This is the whole safety
argument and it is enforced by :meth:`FocusPointer._ref_is_a_reference` and by
:class:`FocusScope`, not by anyone's discipline. The head says *"look at
``fact_a1b2`` and ``conflict_c3d4``"*; it never says *"the building has three
storeys."* The project already draws this line everywhere else -- a model may
route, resolve, and point, and it may not author a fact -- and it matters more
here than anywhere: a head agent that could assert would be an unreviewed second
extractor whose claims arrive on a commander's screen with a commander's urgency
behind them, beside facts that went through an extractor, a span binding, a
provenance record and a confidence. Nothing on the receiving end could tell the
two apart.

Two mechanisms hold it:

* the **shape** check on :attr:`FocusPointer.ref` refuses the shapes a value
  takes -- free text, whitespace, a bare number, a quotation. It is a coarse
  filter and it is honest about that; ``three-storeys`` would pass it.
* the **closed list** in :class:`FocusScope` is what makes it airtight. A scope
  is derived from exactly one :class:`~firstdue.domain.profiles.ProfileSnapshot`
  and the questions recalled against it, and :func:`compose_focus` drops any
  pointer whose reference is not in that list, under the kind it was filed
  under. A reference that does not name something already on file cannot reach
  a focus at all, whatever it looks like.

**One focus, one snapshot.** :attr:`IncidentFocus.profile_version` is required
and binds the whole document, not each pointer. Two agents acting on focus
computed against different profile versions is the same class of bug
:mod:`firstdue.agents.structure_watch` documents at length for its single
district reading: the conflict one agent was pointed at may already be resolved
in the version the other one read, and neither of them can tell.

**Where it is stored.** As an incident log entry, under
:attr:`~firstdue.domain.enums.LogEntryType.FOCUS_COMPOSED`. The log is already
append-only, gapless, sealable, replayable, rendered by the console and written
through to the records system, and a focus is exactly the kind of thing an
investigation asks about afterwards -- *what was this agent told to look at, and
when*. A second Firestore collection would have been a second answer to that
question. :func:`read_focus` is the single way downstream agents read it back.

The log's existing rule survives unchanged: the entry carries ids, keys, and
outcomes, never the caller's transcript. Nothing in a focus is transcript --
:attr:`FocusPointer.reason` is a sentence about ids and is bounded so it cannot
become a quotation of one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firstdue.domain.enums import LogEntryType
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.memory import OpenQuestion
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.errors import ValidationError
from firstdue.observability.logging import get_logger
from firstdue.ports.repositories import IncidentLogRepository

logger = get_logger(__name__)

#: Who composes a focus. Must equal :data:`firstdue.incident.interceptor.AGENT_ID`;
#: spelled here rather than imported because the interceptor imports this module
#: and a cycle between the contract and its one caller helps nobody. A unit test
#: holds the two together.
COMPOSER_ID: Final[str] = "incident-interceptor"

#: How long a pointer's reason may be.
#:
#: The same argument as :data:`~firstdue.domain.memory.MAX_MEMORY_TEXT`, applied
#: to the one field on a focus that takes prose. A reason says *why these ids
#: matter to you*, in terms of ids and canonical keys -- a sentence somebody
#: could write from the profile without reopening the call recording. No
#: validator can tell a sentence about a record from a copy of one, but it can
#: refuse a field long enough to be the second, and that is what this is.
MAX_FOCUS_REASON: Final[int] = 240

#: How long an agent's headline may be. One line, and short enough that it is a
#: subject rather than a summary of findings.
MAX_FOCUS_HEADLINE: Final[int] = 200

#: The shape a reference takes: a prefixed id, a canonical key, or a namespaced
#: path. Lowercase first character, one structural separator, and no whitespace
#: anywhere -- ``fact_a1b2``, ``conflict_c3d4``, ``mq_9f1c``, ``structure.stories``,
#: ``geometry/sf-1550-bryant``. Everything a value looks like fails it: prose has
#: spaces, a bare measurement has no separator, a quotation has punctuation this
#: refuses outright.
_REFERENCE = re.compile(r"^[a-z][a-z0-9]*[_.:/-][A-Za-z0-9][A-Za-z0-9_.:/-]*$")

#: Characters that never appear in a reference or in a sentence about ids, and
#: do appear the moment somebody pastes a line of a transcript into one.
_QUOTING = re.compile(r"[\"'`\n\r\t]")


class FocusKind(StrEnum):
    """What sort of thing a pointer points at.

    Closed, because the kind decides which closed list a reference has to be
    found in (:meth:`FocusScope.holds`) and which agents have a reason to be
    handed it. An open string here would make "is this reference resolvable"
    unanswerable, which is the check the whole module rests on.
    """

    FACT = "FACT"
    CONFLICT = "CONFLICT"
    OPEN_QUESTION = "OPEN_QUESTION"
    GEOMETRY = "GEOMETRY"
    REFERRAL = "REFERRAL"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    SURVEY = "SURVEY"
    HAZARD = "HAZARD"


def geometry_ref(address_id: str) -> str:
    """The reference a geometry pointer uses.

    A :class:`~firstdue.domain.geometry.GeometrySpec` has no id of its own -- it
    is the one measured picture of one building -- so the reference is derived
    from the address rather than minted, which keeps it stable across passes and
    re-derivable by whoever reads the focus back.
    """
    return f"geometry/{address_id}"


def survey_ref(address_id: str) -> str:
    """The reference a survey pointer uses. Derived, for the same reason."""
    return f"survey/{address_id}"


def _one_line(value: str, *, field: str, limit: int) -> str:
    """Bound one line of a focus, and refuse the shape a quotation takes.

    Length and quoting, and nothing cleverer. A focus is written to the incident
    log, which is written through to the records system; the two ways a caller's
    words get there are a field long enough to hold them and a field somebody
    pasted them into with the quote marks still attached.
    """
    text = value.strip()
    if not text:
        raise ValidationError(f"{field} must not be empty", details={"field": field})
    if len(text) > limit:
        raise ValidationError(
            "a focus states why ids matter, never what a record or a caller said",
            details={"field": field, "length": len(text), "max": limit},
        )
    if _QUOTING.search(text):
        raise ValidationError(
            "a focus line carries no quotation and no line break",
            details={"field": field},
        )
    return text


class FocusPointer(BaseModel):
    """One thing to look at, and why it matters. A reference, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FocusKind
    #: An id or a canonical key. **Never a value.** See :meth:`_ref_is_a_reference`.
    ref: str = Field(min_length=3, max_length=120)
    #: Why it matters, in terms of ids and keys.
    reason: str = Field(min_length=1, max_length=MAX_FOCUS_REASON)
    #: 1 highest .. 5 lowest. Derived deterministically by whoever composed it.
    priority: int = Field(ge=1, le=5)

    @field_validator("ref")
    @classmethod
    def _ref_is_a_reference(cls, value: str) -> str:
        """Refuse a reference that is really a value.

        The failure this prevents is specific and it is not hypothetical. A head
        agent handed a snapshot and a transcript has everything it needs to write
        ``"three storeys"`` into a field the next agent will read, and the next
        agent has no way to tell that string from one that came off a permit --
        no source type, no confidence, no span, no fact id, nothing to check it
        against and nothing to raise a conflict with. It would be a second
        extractor, unreviewed, with a commander's urgency behind it.

        So a ``ref`` must *look like* a reference before it may be one: a
        prefixed id, a canonical key, or a namespaced path, with no whitespace
        and no quoting. That is a coarse filter and it is meant to be -- the
        closed list in :class:`FocusScope` is what actually decides whether a
        reference names something on file. This is the cheap check that runs
        first and catches the obvious mistake.
        """
        text = value.strip()
        if not _REFERENCE.fullmatch(text):
            raise ValidationError(
                "a focus pointer carries a reference -- an id or a canonical key -- "
                "never a value",
                details={"ref": text[:60]},
            )
        return text

    @field_validator("reason")
    @classmethod
    def _reason_is_a_sentence_about_ids(cls, value: str) -> str:
        return _one_line(value, field="reason", limit=MAX_FOCUS_REASON)


class AgentFocus(BaseModel):
    """What one agent is pointed at, and the one line that frames it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    #: One line. Names the subject, asserts nothing about it -- the pointers
    #: carry the substance, and they carry it as references.
    headline: str = Field(min_length=1, max_length=MAX_FOCUS_HEADLINE)
    pointers: tuple[FocusPointer, ...] = ()

    @field_validator("headline")
    @classmethod
    def _headline_is_one_line(cls, value: str) -> str:
        return _one_line(value, field="headline", limit=MAX_FOCUS_HEADLINE)

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(pointer.ref for pointer in self.pointers)

    def by_priority(self) -> tuple[FocusPointer, ...]:
        """The pointers in the order they should be read. Stable, for replay."""
        return tuple(sorted(self.pointers, key=lambda p: (p.priority, str(p.kind), p.ref)))


class IncidentFocus(BaseModel):
    """One composition of attention, bound to one profile snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    composed_by: str = Field(min_length=1, max_length=120)
    composed_by_version: str = Field(min_length=1, max_length=40)
    composed_at: datetime
    #: Binds the **whole** focus to one snapshot, not each pointer to its own.
    #: See the module docstring: two agents acting on focus computed against two
    #: profile versions is the failure this field exists to make visible.
    profile_version: int = Field(ge=0)
    per_agent: tuple[AgentFocus, ...] = ()
    #: The slow loop's unfinished threads that bear on this incident, by id.
    #: Carried on the focus as a whole as well as on the pointers, because
    #: "which questions were open when this incident ran" is asked afterwards
    #: about the incident rather than about any one agent.
    open_question_ids: tuple[str, ...] = ()

    @field_validator("composed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """A focus outlives its process. A naive timestamp compares badly later."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValidationError(
                "composed_at must be timezone-aware", details={"field": "composed_at"}
            )
        return value

    @model_validator(mode="after")
    def _one_entry_per_agent(self) -> Self:
        """One agent, one focus.

        Two entries for one agent would mean two answers to "what should you
        look at", and :meth:`for_agent` would return whichever was written
        first -- silently, and differently depending on composition order.
        """
        seen = [entry.agent_id for entry in self.per_agent]
        if len(set(seen)) != len(seen):
            raise ValidationError(
                "a focus names each agent at most once",
                details={"incident_id": self.incident_id},
            )
        return self

    def for_agent(self, agent_id: str) -> AgentFocus | None:
        """What this agent should look at, or ``None`` if it was pointed nowhere.

        ``None`` is a real answer and not a degraded one: an agent the focus
        says nothing about has nothing accumulated bearing on what it was
        handed, and inventing a pointer to fill the gap would be the head
        asserting rather than pointing.
        """
        for entry in self.per_agent:
            if entry.agent_id == agent_id:
                return entry
        return None

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(entry.agent_id for entry in self.per_agent)

    @property
    def pointers(self) -> tuple[FocusPointer, ...]:
        return tuple(pointer for entry in self.per_agent for pointer in entry.pointers)

    @property
    def pointer_count(self) -> int:
        return len(self.pointers)

    @property
    def refs(self) -> tuple[str, ...]:
        """Every reference this focus points at, sorted and de-duplicated."""
        return tuple(sorted({pointer.ref for pointer in self.pointers}))

    def unresolved_against(self, scope: FocusScope) -> tuple[FocusPointer, ...]:
        """Pointers this scope cannot resolve. Empty for a focus composed against it.

        Re-checkable on the read side as well as at composition, and that is the
        point: a focus read back out of the log is a document, and the snapshot
        it was composed against is a separate document. Handing both to this
        method is how a reader confirms they are still talking about the same
        version rather than assuming it.
        """
        if scope.profile_version != self.profile_version:
            return self.pointers
        return tuple(pointer for pointer in self.pointers if not scope.holds(pointer))


class FocusScope(BaseModel):
    """Every reference a focus composed against one snapshot may point at.

    Derived from exactly one snapshot at one ``profile_version``, plus the
    questions recalled for that address. This is the closed list that turns "a
    pointer carries a reference" from a shape check into an enforced property:
    :func:`compose_focus` drops anything not in it, under the kind it was filed
    under, so a pointer can only ever name something already on file.

    Kind-aware on purpose. A reference that is a real fact id filed under
    ``CONFLICT`` is a mislabel, and a mislabelled pointer sends an agent to the
    wrong store to look for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    profile_version: int = Field(ge=0)
    #: The closed list, per kind. Sorted, so a scope is comparable across runs.
    refs: dict[FocusKind, tuple[str, ...]] = Field(default_factory=dict)

    def holds(self, pointer: FocusPointer) -> bool:
        return pointer.ref in self.refs.get(pointer.kind, ())

    def of(self, kind: FocusKind) -> tuple[str, ...]:
        return self.refs.get(kind, ())

    @property
    def size(self) -> int:
        return sum(len(refs) for refs in self.refs.values())


def focus_scope(
    snapshot: ProfileSnapshot,
    *,
    questions: Sequence[OpenQuestion] = (),
    unknown_keys: Iterable[str] = (),
) -> FocusScope:
    """The closed list of references this snapshot supports.

    Everything here comes off the snapshot or off questions already recalled
    under the caller's own scopes -- there is no path by which a reference the
    composer invented reaches the list, because the composer is not consulted.

    ``unknown_keys`` is passed in rather than derived, because "which attributes
    have no record" is a question about what the brief asked for, and the
    snapshot only knows what it has.
    """
    # The resolved winner per attribute, **and** every fact a conflict is
    # between. The losing side of a disagreement is not on ``facts`` -- that is
    # the resolved view -- and it is exactly the fact a pointer about a conflict
    # needs to name. Leaving it out would drop half of every collision.
    resolved = {fact.fact_id for fact in snapshot.facts.values()}
    disputed = {fact_id for conflict in snapshot.conflicts for fact_id in conflict.fact_ids}
    facts = tuple(sorted(resolved | disputed))
    hazard_facts = tuple(
        sorted(fact.fact_id for key, fact in snapshot.facts.items() if key.startswith("hazard."))
    )
    hazard_keys = tuple(sorted(key for key in snapshot.facts if key.startswith("hazard.")))
    refs: dict[FocusKind, tuple[str, ...]] = {
        FocusKind.FACT: facts,
        FocusKind.CONFLICT: tuple(sorted(c.conflict_id for c in snapshot.conflicts)),
        FocusKind.OPEN_QUESTION: tuple(sorted(q.question_id for q in questions)),
        FocusKind.REFERRAL: tuple(sorted(snapshot.open_referral_ids)),
        FocusKind.UNKNOWN_KEY: tuple(sorted(set(unknown_keys))),
        # A hazard pointer names either the filed fact or the attribute it is
        # filed under. Both, because "no Tier II filing on record" is an answer
        # with a key and no fact id, and that answer is the one worth pointing a
        # notifier at as loudly as a filing that exists.
        FocusKind.HAZARD: tuple(sorted(set(hazard_facts) | set(hazard_keys))),
        FocusKind.GEOMETRY: (
            (geometry_ref(snapshot.address_id),) if snapshot.geometry is not None else ()
        ),
        FocusKind.SURVEY: (
            (survey_ref(snapshot.address_id),) if snapshot.last_human_survey is not None else ()
        ),
    }
    return FocusScope(
        address_id=snapshot.address_id,
        profile_version=snapshot.profile_version,
        refs=refs,
    )


def compose_focus(
    *,
    incident_id: str,
    scope: FocusScope,
    per_agent: Sequence[AgentFocus],
    open_question_ids: Sequence[str] = (),
    composed_by: str = COMPOSER_ID,
    composed_by_version: str,
    composed_at: datetime,
) -> IncidentFocus:
    """Build a focus, keeping only what this snapshot can resolve.

    The single sanctioned constructor, and the place the closed-list rule is
    enforced. A pointer the scope does not hold is *dropped and counted*, not
    raised on: composing attention happens on a countdown, and a graph that
    produced one unresolvable pointer out of nine has produced eight useful ones
    -- refusing the lot would trade a small defect for no focus at all. The drop
    is logged with the kind and the count so it is a defect somebody can see.

    An agent left with no pointers is dropped too. Pointing an agent at nothing
    is noise on a screen that has none to spare, and its absence already means
    "nothing on file bears on what you were handed" -- see
    :meth:`IncidentFocus.for_agent`.
    """
    kept: list[AgentFocus] = []
    dropped = 0
    for entry in per_agent:
        resolvable = tuple(pointer for pointer in entry.pointers if scope.holds(pointer))
        dropped += len(entry.pointers) - len(resolvable)
        if not resolvable:
            continue
        kept.append(entry.model_copy(update={"pointers": resolvable}))

    if dropped:
        logger.warning(
            "focus_pointer_unresolvable",
            extra={
                "incident_id": incident_id,
                "profile_version": scope.profile_version,
                "dropped": dropped,
            },
        )

    resolvable_questions = set(scope.of(FocusKind.OPEN_QUESTION))
    return IncidentFocus(
        incident_id=incident_id,
        address_id=scope.address_id,
        composed_by=composed_by,
        composed_by_version=composed_by_version,
        composed_at=composed_at,
        profile_version=scope.profile_version,
        per_agent=tuple(kept),
        open_question_ids=tuple(
            sorted({q for q in open_question_ids if q in resolvable_questions})
        ),
    )


# ---------------------------------------------------------------- persistence


def focus_entry_id(incident_id: str, sequence: int) -> str:
    """Derived, not minted.

    The same discipline the rest of the domain uses for ids that have to survive
    a replay: the entry for sequence *n* of one incident is the same entry id
    every time it is rebuilt, so replaying an incident produces the log it
    produced the first time rather than one that differs only in its ids.
    """
    return f"entry_focus_{incident_id}_{sequence}"[:120]


def focus_log_entry(
    focus: IncidentFocus,
    *,
    sequence: int,
    now: datetime,
    profile_snapshot_id: str = "",
) -> IncidentLogEntry:
    """The log entry that stores one focus.

    Ids, keys, counts, and the reasons -- which are sentences about ids. Nothing
    from the call and nothing from a source document, which is the rule every
    other entry type in this log already keeps.

    ``profile_snapshot_id`` defaults to the same ``"pending"`` placeholder
    :meth:`~firstdue.incident.recorder.IncidentRecorder._append` uses for every
    non-emission entry: the caller that has the incident on hand knows the
    snapshot id and the composer does not, and inventing one that looked real
    would be worse than a placeholder that plainly is not.
    """
    return IncidentLogEntry(
        entry_id=focus_entry_id(focus.incident_id, sequence),
        incident_id=focus.incident_id,
        sequence=sequence,
        entry_type=LogEntryType.FOCUS_COMPOSED,
        occurred_at=now,
        profile_snapshot_id=profile_snapshot_id or "pending",
        agent_versions={focus.composed_by: focus.composed_by_version},
        content={
            # The whole document, so a reader reconstructs exactly what each
            # agent was told rather than a summary of it.
            "focus": focus.model_dump(mode="json"),
            # Flattened beside it for the console and the NERIS draft, which
            # count entries and never parse them.
            "profile_version": focus.profile_version,
            "agent_ids": list(focus.agent_ids),
            "pointer_count": focus.pointer_count,
            "open_question_ids": list(focus.open_question_ids),
        },
    )


async def read_focus(log: IncidentLogRepository, incident_id: str) -> IncidentFocus | None:
    """The focus this incident is running on, or ``None`` if none was composed.

    The single way a downstream agent reads it, so there is one answer to "what
    was I pointed at" rather than one per caller. The **latest** focus wins: a
    second composition supersedes the first for the same reason a later brief
    version does -- it was composed against more, and an agent acting on the
    earlier one would be acting on a picture the incident has moved past. Both
    remain in the log, because nothing in the log is ever replaced.
    """
    stored = await log.get_log(incident_id)
    latest: IncidentLogEntry | None = None
    for entry in stored.entries:
        if entry.entry_type is LogEntryType.FOCUS_COMPOSED:
            latest = entry
    if latest is None:
        return None
    payload: Any = latest.content.get("focus")
    if not isinstance(payload, dict):  # pragma: no cover - focus_log_entry always writes one
        logger.warning(
            "focus_entry_malformed",
            extra={"incident_id": incident_id, "sequence": latest.sequence},
        )
        return None
    return IncidentFocus.model_validate(payload)


__all__ = [
    "COMPOSER_ID",
    "MAX_FOCUS_HEADLINE",
    "MAX_FOCUS_REASON",
    "AgentFocus",
    "FocusKind",
    "FocusPointer",
    "FocusScope",
    "IncidentFocus",
    "compose_focus",
    "focus_entry_id",
    "focus_log_entry",
    "focus_scope",
    "geometry_ref",
    "read_focus",
    "survey_ref",
]
