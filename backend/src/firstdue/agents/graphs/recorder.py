"""The recorder's synthesis graph -- the report, and the threads the fire closed.

Every other reasoning graph in this fleet runs against a clock somebody is
watching. This one does not. ``incident-recorder`` runs *after* the incident
closes, with fifteen seconds and nothing waiting on it, which makes it the one
place in the incident loop where multi-step reasoning costs a commander nothing
if it takes the long way round.

It does two things with that room.

**It drafts the report from the whole record rather than from the log's order.**
The deterministic draft has always been a set of counts, which is honest and
close to useless as a starting point for the officer who has to file. Given the
head agent's briefing -- :mod:`firstdue.incident.focus` -- the draft leads with
the conflicts and observations the head judged material, because a pointer says
*this is the part of the record that mattered* and the log's own order says only
*this is when it was written down*.

**It closes questions the slow loop opened weeks ago and could not settle.**
``records-watcher`` asks in March whether a parcel has an unpermitted third
floor, cannot find the filing, and parks the thread. ``structure-watch`` ranks
the building up as the thread ages. In August a crew physically stands in the
building and the incident record carries what they observed -- which sits at the
top of the merge precedence and outranks every filed record. That is the moment
the question is answerable, and this graph is what answers it.

Three boundaries make that safe.

**Closing is conservative, and an open question is a correct state.** A wrongly
closed thread silently deletes weeks of accumulated work *and* tells every later
pass to stop looking, which is strictly worse than carrying it another month. So
a question is closed only on an identifier the incident record and the question
have in common -- see :func:`answered_by` -- and everything else is recorded
through :meth:`~firstdue.services.memory_bank.MemoryBank.rule_out` and left open.
"An incident stood in this building and did not settle it" is real, durable,
eliminated work; it is not an answer.

**The model may not author a fact.** It drafts prose and it orders the
examination; it never decides what is true. The narrative it returns is checked
against the deterministic floor it was handed -- every identifier and every
number in the draft must already appear in the floor or on the record -- and a
draft that introduces either is rejected in favour of the floor. The planner is
bounded as every planner here is: a closed list of question ids in, one of them
out, so the worst it can do is examine the threads in a poor order.

**The head points; it does not assert.** A :class:`Lead` carries a ref and a
reason and reaches the prompt as guidance about *what to look at*. Nothing in
this module treats a pointer's ref as a claim: an unmatched ref steers the draft
and closes nothing.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Collection, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    NODE_PARK,
    PLANNER_DEADLINE_MS,
    STOP,
    BudgetGuard,
    FixedOrderPlanner,
    GraphSpec,
    GraphState,
    GraphStop,
    NodeResult,
    ReasoningPlanner,
)
from firstdue.domain.enums import LogEntryType, Scope
from firstdue.domain.incidents import Incident
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.memory import MAX_MEMORY_TEXT
from firstdue.errors import FirstDueError
from firstdue.observability.logging import get_logger
from firstdue.ports.model import ModelClient
from firstdue.ports.repositories import IncidentLogRepository
from firstdue.services.memory_bank import MemoryBank

logger = get_logger(__name__)

NODE_ASSEMBLE: Final[str] = "assemble"
NODE_FOCUS: Final[str] = "focus"
NODE_ANSWER: Final[str] = "answer"
NODE_DRAFT: Final[str] = "draft"

#: The agent this graph belongs to, and the id the head's briefing addresses.
#: Spelled here rather than imported from :mod:`firstdue.incident.recorder`,
#: which imports this module -- the dependency runs one way only.
RECORDER_AGENT_ID: Final[str] = "incident-recorder"

#: The composition template. One id, so a recorded response is findable.
NERIS_TEMPLATE_ID: Final[str] = "neris-narrative"

#: How long a NERIS narrative may be, and how long the model has to write it.
#: Generous on both counts relative to the referral draft: this agent is off the
#: countdown, and the report is the longest prose the system produces.
NERIS_DRAFT_MAX_CHARS: Final[int] = 6_000
NERIS_DRAFT_DEADLINE_MS: Final[int] = 8_000

#: How much of one of the head's pointers this module will carry.
#:
#: Its own bounds rather than imports of ``MAX_FOCUS_REASON`` and the ``ref``
#: field's length, because the point of re-bounding at this boundary is that
#: this side does not move when the other one does. They are the same numbers
#: today and a unit test says so; if the head widens its reason field tomorrow,
#: what reaches a prompt from here does not widen with it.
MAX_LEAD_REF: Final[int] = 120
MAX_LEAD_REASON: Final[int] = 240

#: Entry content fields that hold an identifier this incident actually recorded.
#: Read by name rather than by scanning the whole content dict, because a
#: content dict also holds free text -- an IC's resolution note, an intake's
#: rejection reason -- and a citable list assembled by scanning would quietly
#: license the model to quote it.
_CITABLE_FIELDS: Final[tuple[str, ...]] = (
    "fact_id",
    "canonical_key",
    "conflict_id",
    "resolving_fact_id",
    "emission_id",
    "benchmark_id",
    "decision_id",
    "approval_id",
    "external_ref",
    "source_ref",
    "agent_ref",
    "rule_id",
)

#: The subset of those that are *evidence about the building*, and the only
#: ones that can close a question.
#:
#: Two entry types qualify and no others. ``FACT_OBSERVED`` is something
#: recorded from the scene; ``IC_RESOLUTION`` is a commander settling a
#: disagreement while standing in front of the thing. A brief emission is the
#: system restating what it already believed, and a policy decision is about the
#: system rather than about the building -- neither is new evidence, and a
#: question closed on one of them would be closed on its own prior guess.
_ON_SCENE_TYPES: Final[frozenset[LogEntryType]] = frozenset(
    {LogEntryType.FACT_OBSERVED, LogEntryType.IC_RESOLUTION}
)

_EVIDENCE_FIELDS: Final[tuple[str, ...]] = (
    "fact_id",
    "canonical_key",
    "conflict_id",
    "resolving_fact_id",
)

#: A token shaped like something this system names: a canonical key, a derived
#: id, an address id, a permit number, a timestamp.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*[A-Za-z0-9]")

_DIGITS = re.compile(r"\d+")


def identifiers_in(text: str) -> frozenset[str]:
    """Every token in ``text`` that is shaped like an identifier this system mints.

    A separator plus a digit, an underscore, or a dot. That shape is what
    distinguishes ``sf-1550-bryant``, ``fact_9c2``, and ``structure.stories``
    from ``ground-floor`` and ``cross-check``, which are English and must not
    cost a draft its acceptance.

    Deliberately over-inclusive rather than under: a false positive costs a
    well-written draft and ships the plain one, and a false negative lets an
    invented record number onto a report that goes to the state.
    """
    found: set[str] = set()
    for token in _TOKEN.findall(text):
        if not any(sep in token for sep in "._-:+"):
            continue
        if any(char.isdigit() for char in token) or "_" in token or "." in token:
            found.add(token.casefold())
    return frozenset(found)


def numbers_in(text: str) -> frozenset[str]:
    """Every run of digits in ``text``.

    A count, a floor number, an alarm level, a year. The draft may re-present
    any number the deterministic floor already stated and may not state one the
    floor did not -- which is what stops a model rounding four brief versions to
    "several" and then to "six".
    """
    return frozenset(_DIGITS.findall(text))


# ------------------------------------------------------- the head's briefing


class Lead(BaseModel):
    """One pointer from the head agent's briefing, coerced at the boundary.

    The briefing is composed by another agent and reaches this one through the
    incident log, so it is data crossing a trust boundary and is re-typed and
    re-bounded here rather than used as it arrives. A ref long enough to hold a
    sentence would be a way for one to reach the prompt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: An id or a canonical key. Something to go and read, never a value.
    ref: str = Field(min_length=1, max_length=MAX_LEAD_REF)
    #: Why the head thinks it matters. Guidance for the prose, never a claim.
    reason: str = Field(default="", max_length=MAX_LEAD_REASON)
    #: 1 highest .. 5 lowest, matching :class:`~firstdue.incident.focus.FocusPointer`.
    priority: int = 0


async def read_leads(
    log: IncidentLogRepository, incident_id: str, *, agent_id: str = RECORDER_AGENT_ID
) -> tuple[tuple[Lead, ...], tuple[str, ...]] | None:
    """The head's pointers for this agent, and the questions it named open.

    ``None`` means there is no briefing -- either the incident composed none, or
    this build has no :mod:`firstdue.incident.focus` at all. Both degrade to the
    behaviour the recorder had before the head existed, which is the whole point
    of returning a value here instead of raising: a report is not worth failing
    over a briefing that did not arrive.

    Resolved through :func:`importlib.import_module` rather than imported at
    module scope because the arrow between the two packages already runs the
    other way. ``firstdue.incident`` imports
    :class:`~firstdue.incident.recorder.IncidentRecorder`, which imports this
    module; a module-scope import back into ``firstdue.incident.focus`` would
    close that loop and make the incident package unimportable. It is the same
    cycle :mod:`firstdue.incident.intake` documents at the top of the file, from
    the other end.

    Whatever the head wrote is re-typed into :class:`Lead` here rather than used
    as it arrives. A briefing is composed by another agent and reaches this one
    through the log, so it crosses a trust boundary on the way in; coercing and
    re-bounding it at that boundary is what stops a field the head widens later
    from silently widening what reaches a prompt.
    """
    try:
        module = importlib.import_module("firstdue.incident.focus")
        focus = await module.read_focus(log, incident_id)
    except ImportError:
        return None
    except Exception as exc:  # pragma: no cover - the head is not this agent
        # Deliberately every exception. A malformed briefing costs the pointers
        # and must never cost the incident its report.
        logger.warning(
            "recorder_focus_unreadable",
            extra={"incident_id": incident_id, "error_type": type(exc).__name__},
        )
        return None
    if focus is None:
        return None

    mine = focus.for_agent(agent_id)
    # ``by_priority`` rather than a sort of this module's own: the head declares
    # the reading order, priority 1 highest, and a second ordering here would be
    # a second answer to "what should I read first" that nothing holds to the
    # first one.
    pointers = tuple(mine.by_priority()) if mine is not None else ()
    leads = tuple(
        Lead(
            ref=str(pointer.ref)[:MAX_LEAD_REF],
            reason=str(getattr(pointer, "reason", ""))[:MAX_LEAD_REASON],
            priority=int(getattr(pointer, "priority", 0)),
        )
        for pointer in pointers
        if str(getattr(pointer, "ref", "")).strip()
    )
    questions = tuple(str(qid) for qid in (focus.open_question_ids or ()) if str(qid).strip())
    return leads, questions


# ------------------------------------------------------------- the evidence


class IncidentEvidence(BaseModel):
    """What the incident record says, reduced to identifiers and counts.

    The whole record goes into this and nothing else comes out of it. A log
    entry's ``content`` holds an IC's note and an intake's rejection reason, and
    both are department records that belong in the log rather than in a prompt,
    a span, or a draft's allowed vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Every identifier the record carries. The closed list a draft may cite.
    citable: tuple[str, ...] = ()
    #: The subset observed on scene. The only evidence that can close a thread.
    on_scene: tuple[str, ...] = ()
    tallies: dict[str, int] = Field(default_factory=dict)
    entries: int = Field(default=0, ge=0)


def evidence_from(entries: Sequence[IncidentLogEntry], *, incident: Incident) -> IncidentEvidence:
    """Reduce one incident's log to what a draft may say and what may close a thread.

    Pure, so an officer can re-derive by hand which identifiers this incident
    put in play -- and so the rule that decides whether a question was answered
    is a rule rather than a judgement something made once.
    """
    citable: set[str] = {
        incident.incident_id.casefold(),
        incident.address_id.casefold(),
        incident.cad_ref.casefold(),
    }
    on_scene: set[str] = set()
    tallies: dict[str, int] = {}

    for entry in entries:
        tallies[str(entry.entry_type)] = tallies.get(str(entry.entry_type), 0) + 1
        citable.add(entry.entry_id.casefold())
        for field_name in _CITABLE_FIELDS:
            value = str(entry.content.get(field_name) or "").strip()
            if value:
                citable.add(value.casefold())
        if entry.entry_type not in _ON_SCENE_TYPES:
            continue
        for field_name in _EVIDENCE_FIELDS:
            value = str(entry.content.get(field_name) or "").strip()
            if value:
                on_scene.add(value.casefold())

    return IncidentEvidence(
        citable=tuple(sorted(citable)),
        on_scene=tuple(sorted(on_scene)),
        tallies=tallies,
        entries=len(entries),
    )


def answered_by(
    *,
    question_text: str,
    waiting_on: str,
    evidence_fact_ids: Sequence[str],
    on_scene: Collection[str],
) -> tuple[str, ...]:
    """The on-scene identifiers that answer this question, or an empty tuple.

    **This is the rule, and it is deliberately narrow.** A question is answered
    only where it and the incident record name the *same identifier*: a
    canonical key, a fact id, or a conflict id. The question's own sentence is
    read for identifier-shaped tokens only -- it is never compared to the record
    by meaning, because a resemblance judged by a model is exactly the kind of
    "close enough" that closes a thread nobody actually settled.

    That narrowness has a cost and it is the right one to pay. Questions phrased
    entirely in English -- "does 450 Hayes have an unpermitted third floor?" --
    name no identifier and are therefore never closed here, however plainly the
    incident answered them. They stay open with this incident recorded against
    them, which is a thread a person can finish in ten seconds. The opposite
    error is a thread nobody can recover, because the record of what was still
    unknown is gone.
    """
    named = identifiers_in(question_text) | identifiers_in(waiting_on)
    named |= {fact_id.casefold() for fact_id in evidence_fact_ids}
    matched = named & {token.casefold() for token in on_scene}
    return tuple(sorted(matched))


# ---------------------------------------------------------------- the floor


def deterministic_narrative(
    incident: Incident,
    *,
    evidence: IncidentEvidence,
    leads: Sequence[Lead],
    resolved: Sequence[str],
    left_open: Sequence[str],
    disclaimer: str,
) -> str:
    """The report text the officer gets whatever the model does.

    The floor and the fallback at once, and the *source of the vocabulary* the
    polished draft is checked against -- which is why it states every count and
    every identifier explicitly rather than summarising. A floor that said
    "several resolutions" would license a draft to say "six".

    It leads with the head's pointers when there are any. That ordering is the
    graph's first product and it holds with no model wired at all: the head
    judged which parts of the record mattered, and a report that opens with
    those instead of with the log's clerical order is a better report before
    anyone has written a sentence.
    """
    closed_at = incident.closed_at.isoformat() if incident.closed_at is not None else "not recorded"
    lines = [
        f"Incident {incident.incident_id} at {incident.address_id}, "
        f"CAD reference {incident.cad_ref}, alarm level {incident.alarm_level}.",
        f"Dispatched {incident.dispatched_at.isoformat()}; closed {closed_at}.",
    ]

    if leads:
        lines.append("")
        lines.append("Material to this incident, as briefed by the incident head:")
        lines.extend(
            f"- {lead.ref}: {lead.reason}" if lead.reason else f"- {lead.ref}" for lead in leads
        )

    lines.append("")
    lines.append("Recorded in the incident log:")
    lines.extend(
        f"- {entry_type}: {count}" for entry_type, count in sorted(evidence.tallies.items())
    )
    lines.append(f"- log entries: {evidence.entries}")

    if resolved:
        lines.append("")
        lines.append(
            "Prior open questions this incident answered: " + ", ".join(sorted(resolved)) + "."
        )
    if left_open:
        lines.append("")
        lines.append(
            "Prior open questions this incident examined and did not answer: "
            + ", ".join(sorted(left_open))
            + "."
        )

    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


def reject_narrative(
    text: str,
    *,
    accepted: bool,
    floor: str,
    citable: Collection[str],
    disclaimer: str,
) -> str | None:
    """Why a composed narrative cannot ship, or ``None`` when it can.

    Every check is against the *floor* rather than against the prompt, for the
    reason :func:`firstdue.agents.actions._reject_draft` gives: a rule phrased
    against what the model was asked would pass any draft that echoed the
    request and slipped a new claim in beside it.

    The two that matter are the last two. An identifier the floor and the record
    do not contain is a record number this incident does not have, and a number
    the floor does not contain is a count nobody counted. Either one on a report
    that goes to the state is the failure this whole system is built to make
    impossible, so both are refused and the plain draft ships.
    """
    if not accepted:
        return "not_accepted"
    body = text.strip()
    if not body:
        return "empty"
    if len(body) > NERIS_DRAFT_MAX_CHARS:
        return "over_length"
    if disclaimer not in body:
        return "disclaimer_dropped"

    allowed = identifiers_in(floor) | {token.casefold() for token in citable}
    if identifiers_in(body) - allowed:
        return "identifier_introduced"
    if numbers_in(body) - numbers_in(floor):
        return "number_introduced"
    return None


# ----------------------------------------------------------------- the state


class NerisGraphState(GraphState):
    """What the synthesis knows. Identifiers and counts; never a log entry."""

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)

    #: What the record put in play. Set by ``assemble``.
    evidence: IncidentEvidence = Field(default_factory=IncidentEvidence)
    #: The head's pointers, highest priority first. Empty without a briefing.
    leads: tuple[Lead, ...] = ()
    #: Whether a briefing was found at all. Distinct from an empty one: a head
    #: that pointed at nothing and a head that never ran are different states,
    #: and only the second means "behave as this agent did before".
    briefed: bool = False

    #: Threads the head named, still to examine. Drains one per ``answer``.
    pending_questions: tuple[str, ...] = ()
    #: Threads this incident closed, and threads it examined and left open.
    resolved_questions: tuple[str, ...] = ()
    examined_questions: tuple[str, ...] = ()

    narrative: str = Field(default="", max_length=NERIS_DRAFT_MAX_CHARS)
    #: ``deterministic`` or ``model``. Empty until ``draft`` has run.
    narrative_source: str = Field(default="", max_length=20)
    #: Why a composed draft was refused, when one was. A stable code, never the
    #: draft -- the rejected text is the last thing that belongs in a record.
    draft_rejection: str = Field(default="", max_length=60)

    assembled: bool = False
    focused: bool = False
    drafted: bool = False

    def checkpoint_payload(self) -> dict[str, Any]:
        payload = super().checkpoint_payload()
        payload.update(
            {
                "incident_id": self.incident_id,
                "address_id": self.address_id,
                "pending_questions": list(self.pending_questions),
                "resolved_questions": list(self.resolved_questions),
                "examined_questions": list(self.examined_questions),
                "narrative_source": self.narrative_source,
            }
        )
        return payload


# ----------------------------------------------------------------- the nodes


class NerisSynthesis:
    """The nodes of the synthesis, bound to one incident's collaborators.

    A class rather than closures for the reason
    :class:`~firstdue.agents.graphs.hazard.HazardCrossCheck` is one: the nodes
    and the router have to see the same budget the driver is charging, and
    keeping them in one object is what stops the two being wired differently in
    the two places that build a graph.
    """

    def __init__(
        self,
        *,
        incident: Incident,
        log: IncidentLogRepository,
        budget: BudgetGuard,
        disclaimer: str,
        memory: MemoryBank | None = None,
        memory_scopes: Collection[Scope] = (),
        model: ModelClient | None = None,
        planner: ReasoningPlanner | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._incident = incident
        self._log = log
        self._budget = budget
        self._disclaimer = disclaimer
        self._memory = memory
        self._memory_scopes = frozenset(memory_scopes)
        self._model = model
        self._planner = planner or FixedOrderPlanner()
        self._agent_version = agent_version

    @property
    def resolved_by(self) -> str:
        """Who closed the thread, pinned to the version that closed it.

        The version is on the attribution rather than derived from today's
        build, for the reason :class:`~firstdue.domain.memory.OpenQuestion`
        pins ``opened_by_version``: a replay has to say which code closed a
        question, not which code happens to be deployed when someone asks.
        """
        return f"{RECORDER_AGENT_ID}@{self._agent_version}"

    # ---------------------------------------------------------------- nodes

    async def assemble(self, state: NerisGraphState) -> NodeResult:
        """Read the whole incident record and reduce it to identifiers and counts."""
        log = await self._log.get_log(state.incident_id)
        evidence = evidence_from(log.entries, incident=self._incident)
        return NodeResult(
            decision=f"assembled:{evidence.entries}",
            updates={"evidence": evidence, "assembled": True},
            counts={
                "entries": evidence.entries,
                "citable": len(evidence.citable),
                "on_scene": len(evidence.on_scene),
            },
        )

    async def focus(self, state: NerisGraphState) -> NodeResult:
        """Pick up the head agent's briefing, if this incident produced one.

        Absence is a state and not a failure. Without a briefing the graph
        drafts from the record alone and examines no threads at all -- because
        the questions it would close are the ones the head named, and going
        hunting through the bank on its own would make the recorder a second
        agent deciding what an incident was about.
        """
        briefing = await read_leads(self._log, state.incident_id)
        if briefing is None:
            return NodeResult(
                decision="focus:absent",
                updates={"focused": True},
                counts={"leads": 0, "questions": 0},
            )
        leads, questions = briefing
        # A pointer's ref is citable. It is not evidence -- it closes nothing,
        # and it is deliberately absent from ``on_scene`` -- but
        # ``compose_focus`` already refused any ref that does not name something
        # on the profile snapshot, so a report that mentions one is naming a
        # record that exists rather than inventing one.
        widened = state.evidence.model_copy(
            update={
                "citable": tuple(
                    sorted({*state.evidence.citable, *(lead.ref.casefold() for lead in leads)})
                )
            }
        )
        return NodeResult(
            decision=f"focus:{len(leads)}",
            updates={
                "leads": leads,
                "evidence": widened,
                "pending_questions": questions,
                "briefed": True,
                "focused": True,
            },
            counts={"leads": len(leads), "questions": len(questions)},
        )

    async def answer(self, state: NerisGraphState) -> NodeResult:
        """Examine one thread the head named, and close it only if the record answers it.

        One per visit, and the planner chooses which. The choice and the
        examination are one node rather than two because examining a thread
        produces no new evidence for a separate planning step to react to -- it
        removes one option and changes nothing else -- so a ``plan`` node here
        would double the step cost of the loop and decide the same thing twice.

        The planner is bounded exactly as every planner in this package is: a
        closed list of question ids in, one of them out, an answer that is not
        one of them discarded. So a confused model costs the order the threads
        are examined in, and can never cost a thread its accumulated work.
        """
        options = state.pending_questions
        if not options or self._memory is None:
            # No bank wired: the head named threads and there is nowhere to read
            # or close them. Dropping them here rather than at the router keeps
            # the router pure and the reason visible in the trace.
            return NodeResult(
                decision="answer:no_bank" if options else "answer:nothing_named",
                updates={"pending_questions": ()},
                counts={"pending": len(options)},
            )

        chosen = await self._planner.choose(
            node=NODE_ANSWER,
            options=options,
            counts={
                "pending": len(options),
                "resolved": len(state.resolved_questions),
                "on_scene": len(state.evidence.on_scene),
            },
            deadline_ms=PLANNER_DEADLINE_MS,
        )
        question_id = chosen if chosen in options else options[0]
        remaining = tuple(qid for qid in options if qid != question_id)
        updates: dict[str, Any] = {"pending_questions": remaining}

        outcome, matched = await self._examine(question_id, state)
        if outcome == "resolved":
            updates["resolved_questions"] = (*state.resolved_questions, question_id)
        if outcome in {"resolved", "ruled_out"}:
            updates["examined_questions"] = (*state.examined_questions, question_id)
        if outcome == "ruled_out":
            # One elimination for the pass, not one per thread. What this
            # incident eliminated is the *same* thing for every question it
            # examined -- itself -- and it is already recorded against each
            # thread individually in the bank.
            eliminated = f"incident:{state.incident_id}"
            updates["ruled_out"] = tuple(dict.fromkeys((*state.ruled_out, eliminated)))

        return NodeResult(
            decision=f"{outcome}:{question_id}"[:120],
            updates=updates,
            counts={"pending": len(remaining), "matched": matched},
        )

    async def draft(self, state: NerisGraphState) -> NodeResult:
        """Write the report: the deterministic floor, polished only if it survives checking.

        The floor is composed first and unconditionally, so the node has
        something to ship before it has anything to check. A model failure, a
        refusal, or a draft that introduced a record number are all the same
        outcome here -- the plain report, which is worse written and exactly as
        true.
        """
        floor = deterministic_narrative(
            self._incident,
            evidence=state.evidence,
            leads=state.leads,
            resolved=state.resolved_questions,
            left_open=tuple(
                qid for qid in state.examined_questions if qid not in state.resolved_questions
            ),
            disclaimer=self._disclaimer,
        )[:NERIS_DRAFT_MAX_CHARS]
        shipped: dict[str, Any] = {
            "narrative": floor,
            "narrative_source": "deterministic",
            "drafted": True,
            "stop": GraphStop.CLOSED,
        }
        if self._model is None:
            return NodeResult(
                decision="draft:deterministic",
                updates=shipped,
                counts={"chars": len(floor), "leads": len(state.leads)},
            )

        try:
            composed = await self._model.compose(
                template_id=NERIS_TEMPLATE_ID,
                fields={
                    # The floor goes in as a field: the model is rewriting a
                    # document it was handed, not reconstructing an incident.
                    "deterministic_narrative": floor,
                    "lead_with": [lead.ref for lead in state.leads],
                    "disclaimer": self._disclaimer,
                },
                max_chars=NERIS_DRAFT_MAX_CHARS,
                deadline_ms=NERIS_DRAFT_DEADLINE_MS,
            )
        except Exception as exc:
            # Deliberately every exception, as the referral draft does. A report
            # that cannot be produced because the wording service is down is an
            # incident nobody can file on.
            logger.warning(
                "neris_draft_model_unavailable",
                extra={"incident_id": state.incident_id, "error_type": type(exc).__name__},
            )
            return NodeResult(
                decision="draft:model_unavailable",
                updates={**shipped, "draft_rejection": "model_unavailable"},
                counts={"chars": len(floor), "leads": len(state.leads)},
            )

        rejection = reject_narrative(
            composed.text,
            accepted=composed.accepted,
            floor=floor,
            citable=state.evidence.citable,
            disclaimer=self._disclaimer,
        )
        if rejection is not None:
            logger.info(
                "neris_draft_rejected",
                extra={"incident_id": state.incident_id, "reason": rejection},
            )
            return NodeResult(
                decision=f"draft:{rejection}",
                updates={**shipped, "draft_rejection": rejection},
                counts={"chars": len(floor), "leads": len(state.leads)},
            )

        polished = composed.text.strip()
        return NodeResult(
            decision="draft:model",
            updates={**shipped, "narrative": polished, "narrative_source": "model"},
            counts={"chars": len(polished), "leads": len(state.leads)},
        )

    async def park(self, state: NerisGraphState) -> NodeResult:
        """Stop on a ceiling, and ship the plain report anyway.

        The one thing that must not happen is an incident that closes with no
        draft at all, so the floor is composed here from whatever the graph got
        to. Nothing is opened and nothing is ruled out: a thread the graph never
        reached was not eliminated by anything, and recording it as eliminated
        would be a lie the next pass acts on.
        """
        stop = self._budget.exhausted() or GraphStop.OUT_OF_TIME
        floor = deterministic_narrative(
            self._incident,
            evidence=state.evidence,
            leads=state.leads,
            resolved=state.resolved_questions,
            left_open=tuple(
                qid for qid in state.examined_questions if qid not in state.resolved_questions
            ),
            disclaimer=self._disclaimer,
        )[:NERIS_DRAFT_MAX_CHARS]
        return NodeResult(
            decision=f"park:{stop}",
            updates={
                "stop": stop,
                "waiting_on": "the incident record synthesis to finish",
                "narrative": floor,
                "narrative_source": "deterministic",
                "drafted": True,
            },
            counts={"pending": len(state.pending_questions), "chars": len(floor)},
        )

    # --------------------------------------------------------------- router

    def route(self, state: NerisGraphState) -> str:
        """Where the graph goes next. Pure, and the only place the budget bites.

        Both ceilings are checked first, so an exhausted graph parks with a
        plain report rather than starting a model call it cannot finish -- and
        so the bound holds identically under either driver.
        """
        if state.stop is not None:
            return STOP
        if self._budget.exhausted() is not None:
            return NODE_PARK
        if not state.assembled:
            return NODE_ASSEMBLE
        if not state.focused:
            return NODE_FOCUS
        if state.pending_questions:
            return NODE_ANSWER
        if not state.drafted:
            return NODE_DRAFT
        return STOP

    def spec(self) -> GraphSpec[NerisGraphState]:
        return GraphSpec(
            state_type=NerisGraphState,
            entry=NODE_ASSEMBLE,
            nodes={
                NODE_ASSEMBLE: self.assemble,
                NODE_FOCUS: self.focus,
                NODE_ANSWER: self.answer,
                NODE_DRAFT: self.draft,
                NODE_PARK: self.park,
            },
            router=self.route,
        )

    # ------------------------------------------------------------ internals

    async def _examine(self, question_id: str, state: NerisGraphState) -> tuple[str, int]:
        """Decide what this incident did to one remembered thread, and do it.

        Four outcomes, and three of them leave the thread open. ``unreachable``
        is a thread this agent may not read -- the bank gates recall on the
        memory's statutory class, and a recorder that could close what it cannot
        read would be closing a thread it never saw. ``off_building`` is a
        thread about somewhere else, which an incident here says nothing about
        either way. ``ruled_out`` is the ordinary case and the valuable one: a
        crew stood in this building and the record still does not settle it,
        which is durable eliminated work rather than an answer.
        """
        if self._memory is None:  # pragma: no cover - the caller checked
            return "no_bank", 0
        try:
            question = await self._memory.get(question_id, scopes=self._memory_scopes)
        except FirstDueError as exc:
            logger.warning(
                "recorder_question_unreadable",
                extra={"question_id": question_id, "error_code": str(exc.code)},
            )
            return "unreachable", 0
        if question is None or not question.is_open:
            return "unreachable", 0
        if question.address_id != state.address_id:
            return "off_building", 0

        matched = answered_by(
            question_text=question.question,
            waiting_on=question.waiting_on,
            evidence_fact_ids=question.evidence_fact_ids,
            on_scene=state.evidence.on_scene,
        )
        # Both writes can lose a race with another pass -- the thread can be
        # resolved or swept between the read above and the write here. Losing
        # that race is not this incident's problem to solve: the other writer
        # already recorded an outcome, and overwriting it would be the one thing
        # this module exists to prevent.
        try:
            if not matched:
                await self._memory.rule_out(question_id, f"incident:{state.incident_id}")
                return "ruled_out", 0
            await self._memory.resolve(
                question_id,
                resolution=self._resolution_text(state.incident_id, matched),
                resolved_by=self.resolved_by,
            )
        except FirstDueError as exc:
            logger.warning(
                "recorder_question_write_refused",
                extra={"question_id": question_id, "error_code": str(exc.code)},
            )
            return "unreachable", 0
        logger.info(
            "recorder_closed_open_question",
            extra={
                "question_id": question_id,
                "incident_id": state.incident_id,
                "matched": len(matched),
            },
        )
        return "resolved", len(matched)

    def _resolution_text(self, incident_id: str, matched: Sequence[str]) -> str:
        """What closed the thread, said in identifiers.

        Identifiers rather than prose because this string is stored in durable
        memory, which is bounded at :data:`~firstdue.domain.memory.MAX_MEMORY_TEXT`
        for the same reason every other remembered sentence is: a resolution
        that quoted the observation would put the observation somewhere nothing
        screened it into. The ids point at the incident log, which is where the
        observation actually lives.
        """
        return (
            f"Settled on scene during incident {incident_id}; "
            f"the incident record observed {', '.join(matched)}."
        )[:MAX_MEMORY_TEXT]


__all__ = [
    "NERIS_DRAFT_DEADLINE_MS",
    "NERIS_DRAFT_MAX_CHARS",
    "NERIS_TEMPLATE_ID",
    "NODE_ANSWER",
    "NODE_ASSEMBLE",
    "NODE_DRAFT",
    "NODE_FOCUS",
    "RECORDER_AGENT_ID",
    "IncidentEvidence",
    "Lead",
    "NerisGraphState",
    "NerisSynthesis",
    "answered_by",
    "deterministic_narrative",
    "evidence_from",
    "identifiers_in",
    "numbers_in",
    "read_leads",
    "reject_narrative",
]
