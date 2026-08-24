"""The interceptor's focus graph -- what the slow loop already knows about tonight.

Everything this graph reasons over was written down weeks ago and has been
sitting in a store nobody reads at 03:00. ``records-watcher`` filed a permit
saying two storeys. ``geometry-watcher`` measured three off lidar.
``structure-watch`` raised a conflict over the pair in March and nobody has
stood in the building since. ``hazard-watcher`` opened a question about who is
actually filing Tier II at that parcel and it is still open. Then a caller says
there are people on the third floor.

Each of those is already correct and already stored. What nobody does is put
them beside each other at the moment they matter, and that is the entire job
here: point ``sensor-fusion``, ``agency-notifier`` and ``incident-recorder`` at
*those specific ids*, each with a reason, before any of them starts work.

Three boundaries, and each one is load-bearing:

**The graph points; it never asserts.** A node produces a decision and counts,
like every node in :mod:`firstdue.agents.graphs.base`, and the pointers it
causes to be written are built by :func:`pointers_for` out of ids that came off
the snapshot and the memory bank. Every reference is re-checked against
:class:`~firstdue.incident.focus.FocusScope` before it reaches a focus. There is
no path from a node's decision to a value, for the same reason there is none in
the hazard cross-check: a head agent that could assert would be an unreviewed
second extractor.

**The graph does not choose the fleet.** ``plan_handoffs`` decides who is woken,
by capability match against the registry, and this module decides only what each
of them is pointed at -- also by capability match, through :data:`INTERESTS`.
That is why the table below names scopes and never agents: an agency notifier
gets hazard references because it declares ``notify:agency``, not because
somebody typed its id here, and an agent added to the catalog tomorrow is
pointed at the right things without an edit.

**Running out of budget is a state.** The incident is on a countdown and cannot
wait for a graph, so exhaustion routes to ``park`` and the agent falls back to
:func:`fallback_focus` -- the same collisions, ranked deterministically, with no
planner involved. The fallback is not a degraded stub; it is the graph minus the
one thing the model contributes, which is the *order*.

The instant brief is not on this path at all and cannot be. It is rendered,
persisted and transmitted before anything here is called, and
``BriefEmission`` refuses to exist with ``model_invoked=True`` on the instant
stage regardless (ADR 0004). Nothing in this module can block it, delay it, or
change it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
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
from firstdue.domain.enums import Capability, Loop, Scope
from firstdue.domain.keys import GEOMETRY_INVALIDATING_KEYS, IntakeKeys, Keys
from firstdue.domain.memory import OpenQuestion
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor
from firstdue.incident.focus import (
    COMPOSER_ID,
    AgentFocus,
    FocusKind,
    FocusPointer,
    FocusScope,
    IncidentFocus,
    compose_focus,
    focus_scope,
    geometry_ref,
    survey_ref,
)
from firstdue.observability.logging import get_logger
from firstdue.services.memory_bank import MemoryBank

logger = get_logger(__name__)

NODE_GATHER: Final[str] = "gather"
NODE_RECALL: Final[str] = "recall"
NODE_COLLIDE: Final[str] = "collide"
NODE_SELECT: Final[str] = "select"
NODE_CLOSE: Final[str] = "close"

#: How many collisions one focus may point at.
#:
#: A commander reads a screen, and so does an agent with a two-second latency
#: target. Twelve references would not be attention; it would be the profile
#: again, reordered. The bound is on the *composition* rather than on the
#: display for the same reason the step bound is in the router: a limit the
#: renderer applies is a limit that changes when somebody writes a new renderer.
MAX_COLLISIONS: Final[int] = 8

#: How many references one agent may be handed. Same argument, one level down.
MAX_POINTERS_PER_AGENT: Final[int] = 6

#: How many facts a single collision may cite. The interesting ones are the
#: facts a conflict is *between*; past a handful the citation is the fact set
#: rather than the disagreement.
MAX_FACTS_PER_COLLISION: Final[int] = 4


#: Which stored attributes each intake key bears on.
#:
#: The correlation is deterministic and it is a table rather than a judgement,
#: for the reason :mod:`firstdue.incident.handoff` gives about its own rule
#: table: this decides what an agent has its attention drawn to on a fireground,
#: and a condition a model wrote is a condition nobody reviewed.
#:
#: These are *bearings*, not equivalences. ``intake.reported_floor_of_origin``
#: bears on ``structure.stories`` because a caller naming a third floor is worth
#: reading beside what is on file about how many floors there are. It does not
#: assert a storey count, is never merged with one, and the caller's number
#: never appears in a pointer -- only the key it bears on does.
BEARS_ON: Final[dict[str, tuple[str, ...]]] = {
    IntakeKeys.REPORTED_FLOOR_OF_ORIGIN: (Keys.STORIES, Keys.HEIGHT_M),
    IntakeKeys.REPORTED_OCCUPANCY: (Keys.OCCUPANCY_TYPE, Keys.OCCUPANT_LOAD),
    IntakeKeys.ENTRAPMENT_REPORTED: (
        Keys.OCCUPANT_LOAD,
        Keys.STAIRWELL_COUNT,
        Keys.EGRESS_OBSTRUCTION,
        Keys.LIFE_SAFETY_NOTE,
    ),
    IntakeKeys.HAZMAT_REPORTED: (
        Keys.HAZARD_TIER_II_PRESENT,
        Keys.HAZARD_TIER_II_LOCATION,
    ),
    IntakeKeys.ACCESS_NOTE: (Keys.EGRESS_OBSTRUCTION,),
    # Deliberately empty, and deliberately present. A caller saying "this is a
    # second alarm" is recorded and applied to nothing -- the alarm level bounds
    # the incident grant, so a caller who could move it could widen the fleet's
    # authority. The same rule ``reported-alarm-level-is-recorded-never-applied``
    # states in the wake table, said again where attention is decided.
    IntakeKeys.REPORTED_ALARM_LEVEL: (),
}


class Interest(BaseModel):
    """Which agents have a reason to be handed one kind of reference.

    Declared as an authority -- a capability and a set of scopes -- and matched
    against what a :class:`~firstdue.domain.registry.AgentDescriptor` *declares*,
    never against an agent id. Exactly the shape
    :class:`~firstdue.incident.handoff.WakeRule` uses, and for the same three
    consequences: an agent cannot be pointed at something it never claimed it
    could act on, adding an agent to the catalog points it automatically, and an
    interest that matches nobody is a visible gap rather than silence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FocusKind
    required_capabilities: frozenset[Capability] = Field(min_length=1)
    required_scopes: frozenset[Scope] = Field(min_length=1)
    why: str = Field(min_length=1, max_length=300)


#: The table. Fixed order, so a focus is byte-identical across replays.
INTERESTS: Final[tuple[Interest, ...]] = (
    Interest(
        kind=FocusKind.FACT,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_PROFILE}),
        why=(
            "The facts a disagreement is between. Anything that reads the "
            "profile can look them up; nothing else has anywhere to put them."
        ),
    ),
    Interest(
        kind=FocusKind.CONFLICT,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_PROFILE}),
        why=(
            "An open conflict is the single most useful thing to hand an "
            "incident agent, because it is the one thing on the profile that "
            "says the record is not settled."
        ),
    ),
    Interest(
        kind=FocusKind.UNKNOWN_KEY,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_PROFILE}),
        why=(
            "An attribute with no record is not an attribute that is fine. "
            "Whoever reads the profile should be told which ones were never "
            "filed rather than inferring it from a gap."
        ),
    ),
    Interest(
        kind=FocusKind.GEOMETRY,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_GEOMETRY}),
        why=(
            "The measured picture is only useful to an agent that reads "
            "geometry, and it is the thing a disputed storey count would move."
        ),
    ),
    Interest(
        kind=FocusKind.HAZARD,
        required_capabilities=frozenset({Capability.NOTIFY}),
        required_scopes=frozenset({Scope.NOTIFY_AGENCY}),
        why=(
            "A hazard reference is a tell-somebody reference. It goes to "
            "whichever agent declares the authority to tell an agency, and to "
            "no other."
        ),
    ),
    Interest(
        kind=FocusKind.OPEN_QUESTION,
        required_capabilities=frozenset({Capability.WRITE}),
        required_scopes=frozenset({Scope.WRITE_RMS}),
        why=(
            "A thread the slow loop has been carrying since March can be closed "
            "by what an officer sees tonight -- but only by the agent that "
            "writes the record, which is the one that can make the closure "
            "durable."
        ),
    ),
    Interest(
        kind=FocusKind.REFERRAL,
        required_capabilities=frozenset({Capability.WRITE}),
        required_scopes=frozenset({Scope.WRITE_RMS}),
        why=(
            "An open referral has a case number at another department, and the "
            "incident record is where the two get joined."
        ),
    ),
    Interest(
        kind=FocusKind.SURVEY,
        required_capabilities=frozenset({Capability.WRITE}),
        required_scopes=frozenset({Scope.WRITE_RMS}),
        why=(
            "When somebody last stood in the building is a fact about the "
            "record, and it belongs beside the record rather than in a queue "
            "nobody opens during an incident."
        ),
    ),
)


def audience_for(
    kind: FocusKind,
    descriptors: Sequence[AgentDescriptor],
    *,
    now: datetime | None = None,
    exclude_agent_id: str = COMPOSER_ID,
) -> tuple[str, ...]:
    """Which catalogued incident agents may be pointed at this kind of reference.

    A capability match, sorted, and blind to agent ids -- see :class:`Interest`.
    An empty result is a stated gap: the kind was worth pointing at and nobody
    in this catalog declared the authority to act on it.

    The composer excludes *itself*, the same way
    :func:`~firstdue.incident.handoff.eligible` does. It declares ``read:profile``
    and would otherwise match most of the table, and a focus that pointed the
    head agent at the collisions it just derived would be a document telling
    itself what it already decided -- noise on the record, and one edit away
    from a loop.
    """
    interests = [interest for interest in INTERESTS if interest.kind is kind]
    if not interests:
        return ()
    matched = {
        descriptor.agent_id
        for descriptor in descriptors
        if descriptor.agent_id != exclude_agent_id
        and descriptor.loop is Loop.INCIDENT
        and not (now is not None and descriptor.is_deprecated(now))
        for interest in interests
        if interest.required_capabilities <= descriptor.capabilities
        and interest.required_scopes <= descriptor.required_scopes
    }
    return tuple(sorted(matched))


# --------------------------------------------------------------- collisions


class Collision(BaseModel):
    """One attribute that several sources have something to say about.

    A collision is not a finding about the building. It is a finding about the
    *record*: these ids all bear on this canonical key, and at least one of them
    says the picture is unsettled. What makes it worth a commander's attention
    is that the pieces were produced months apart by agents that never met.

    Everything on it is an id, a key, or a count. There is no field a value
    could ride in, which is the same property :class:`~firstdue.agents.graphs.base.NodeResult`
    has and for the same reason: this object reaches a span, a trace and the
    incident log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The attribute, or ``""`` for a question nothing on this profile anchors.
    canonical_key: str = Field(default="", max_length=120)
    #: True when the intake reported something that bears on this attribute.
    #: The *fact that* it was reported, never what was reported.
    reported: bool = False
    conflict_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    #: The highest severity among this attribute's open conflicts. Computed by
    #: the deterministic engine; copied here, never re-judged.
    severity: int = Field(default=0, ge=0, le=5)
    #: True when the brief asked for this attribute and found no record.
    unknown: bool = False
    #: Dates the questions were opened, by question id. A record timestamp, not
    #: a statement about the building -- it is what makes "open since March"
    #: sayable without asserting anything.
    opened_on: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable id for this collision. The planner's closed list is these."""
        if self.canonical_key:
            return self.canonical_key
        return f"question:{self.question_ids[0]}" if self.question_ids else "question:none"

    @property
    def priority(self) -> int:
        """1 highest .. 5 lowest, from a table an officer can re-derive by hand.

        The top of the scale is the collision this whole feature exists for: the
        caller reported something bearing on an attribute the record already
        disagrees with itself about, or that somebody opened a question about
        and never closed. That is the one worth interrupting for. Everything
        below it is ordinary profile hygiene, ranked so the graph running out of
        budget still emits the useful half first.
        """
        if self.reported and (self.conflict_ids or self.question_ids):
            return 1
        if self.reported or self.severity >= 4:
            return 2
        if self.conflict_ids:
            return 3
        if self.question_ids:
            return 4
        return 5

    @property
    def rank(self) -> tuple[int, int, str]:
        """Total order, so two runs over one snapshot select the same collisions."""
        return (self.priority, -self.severity, self.key)


def detect_collisions(
    snapshot: ProfileSnapshot,
    *,
    reported_keys: Sequence[str] = (),
    questions: Sequence[OpenQuestion] = (),
    unknown_keys: Sequence[str] = (),
) -> tuple[Collision, ...]:
    """Every attribute this incident has more than one source of truth about.

    Pure, and deliberately so -- the same argument
    :func:`~firstdue.agents.graphs.hazard.detect_ambiguities` makes. What counts
    as a collision is a rule an officer can re-derive from the same profile, not
    a judgement a model made once at 03:00 and nobody can reconstruct.

    Questions are attached to attributes by their ``evidence_fact_ids``, never
    by reading their text. An id match is a fact about the record; a text match
    would be an opinion about it, and a wrong one would point an agent at a
    thread about a different building.
    """
    bearing: set[str] = set()
    for intake_key in reported_keys:
        bearing.update(BEARS_ON.get(intake_key, ()))

    facts_by_key = {key: fact.fact_id for key, fact in snapshot.facts.items()}
    fact_owner = {fact_id: key for key, fact_id in facts_by_key.items()}

    conflicts_by_key: dict[str, list[Any]] = {}
    for conflict in snapshot.conflicts:
        conflicts_by_key.setdefault(conflict.canonical_key, []).append(conflict)

    questions_by_key: dict[str, list[OpenQuestion]] = {}
    unanchored: list[OpenQuestion] = []
    for question in questions:
        anchors = {
            fact_owner[fact_id] for fact_id in question.evidence_fact_ids if fact_id in fact_owner
        }
        if not anchors:
            unanchored.append(question)
            continue
        for key in anchors:
            questions_by_key.setdefault(key, []).append(question)

    unknown = set(unknown_keys)
    keys = set(conflicts_by_key) | set(questions_by_key) | (bearing & (set(facts_by_key) | unknown))

    found: list[Collision] = []
    for key in sorted(keys):
        conflicts = conflicts_by_key.get(key, [])
        threads = questions_by_key.get(key, [])
        fact_ids = {facts_by_key[key]} if key in facts_by_key else set()
        for conflict in conflicts:
            fact_ids.update(conflict.fact_ids)
        found.append(
            Collision(
                canonical_key=key,
                reported=key in bearing,
                conflict_ids=tuple(sorted(c.conflict_id for c in conflicts)),
                fact_ids=tuple(sorted(fact_ids))[:MAX_FACTS_PER_COLLISION],
                question_ids=tuple(sorted(q.question_id for q in threads)),
                severity=max((c.severity for c in conflicts), default=0),
                unknown=key in unknown,
                opened_on={q.question_id: q.opened_at.date().isoformat() for q in threads},
            )
        )

    # A question nothing on this profile anchors is still a question somebody is
    # carrying about this building. It is ranked below the anchored ones and it
    # is not dropped: "the slow loop is still asking about this address" is
    # worth the recorder knowing even when no fact on file is attached to it.
    for question in sorted(unanchored, key=lambda q: q.question_id):
        found.append(
            Collision(
                question_ids=(question.question_id,),
                opened_on={question.question_id: question.opened_at.date().isoformat()},
            )
        )

    return tuple(sorted(found, key=lambda c: c.rank))


# ----------------------------------------------------------------- pointers


def pointers_for(collision: Collision, *, snapshot: ProfileSnapshot) -> tuple[FocusPointer, ...]:
    """Turn one collision into references, each with a reason about ids.

    Every ``ref`` produced here came off the snapshot or off a recalled
    question, and every ``reason`` is a sentence about those ids and the
    canonical key they are filed under. Nothing about what any of them says
    appears, which is why this function can be read end to end to check the
    module's central claim.
    """
    key = collision.canonical_key
    priority = collision.priority
    pointers: list[FocusPointer] = []

    for conflict_id in collision.conflict_ids:
        pointers.append(
            FocusPointer(
                kind=FocusKind.CONFLICT,
                ref=conflict_id,
                reason=(
                    f"{key} is disputed: {conflict_id} is open at severity "
                    f"{collision.severity} over {len(collision.fact_ids)} facts"
                    + (" and the intake reported on this attribute." if collision.reported else ".")
                ),
                priority=priority,
            )
        )

    for question_id in collision.question_ids:
        opened = collision.opened_on.get(question_id, "")
        since = f" open since {opened}" if opened else ""
        anchored = f" on {key}" if key else " on this address"
        pointers.append(
            FocusPointer(
                kind=FocusKind.OPEN_QUESTION,
                ref=question_id,
                reason=(
                    f"the slow loop has been carrying {question_id}{anchored}{since} "
                    f"and nothing has settled it."
                ),
                priority=priority,
            )
        )

    if key and (collision.conflict_ids or collision.reported):
        for fact_id in collision.fact_ids:
            pointers.append(
                FocusPointer(
                    kind=FocusKind.FACT,
                    ref=fact_id,
                    reason=f"{fact_id} is one of the records filed under {key}.",
                    priority=min(5, priority + 1),
                )
            )

    if collision.unknown and key:
        pointers.append(
            FocusPointer(
                kind=FocusKind.UNKNOWN_KEY,
                ref=key,
                reason=(
                    f"{key} has no record on this profile, so there is nothing "
                    f"on file to check a report against."
                ),
                priority=min(5, priority + 1),
            )
        )

    if key.startswith("hazard."):
        pointers.append(
            FocusPointer(
                kind=FocusKind.HAZARD,
                ref=key,
                reason=(
                    f"{key} is the hazard attribute this incident bears on; what "
                    f"is filed under it is what an agency would be told."
                ),
                priority=priority,
            )
        )

    if key in GEOMETRY_INVALIDATING_KEYS and snapshot.geometry is not None:
        pointers.append(
            FocusPointer(
                kind=FocusKind.GEOMETRY,
                ref=geometry_ref(snapshot.address_id),
                reason=(
                    f"the measured geometry for {snapshot.address_id} is what a "
                    f"change to {key} would move."
                ),
                priority=min(5, priority + 1),
            )
        )

    if collision.conflict_ids and snapshot.last_human_survey is not None:
        pointers.append(
            FocusPointer(
                kind=FocusKind.SURVEY,
                ref=survey_ref(snapshot.address_id),
                reason=(
                    f"a human survey of {snapshot.address_id} is on file and is "
                    f"the kind of record that settles {key}."
                ),
                priority=5,
            )
        )

    return tuple(pointers)


def standing_pointers(snapshot: ProfileSnapshot) -> tuple[FocusPointer, ...]:
    """References that belong to the address rather than to any one collision."""
    return tuple(
        FocusPointer(
            kind=FocusKind.REFERRAL,
            ref=referral_id,
            reason=(
                f"{referral_id} is an open referral on {snapshot.address_id}; its "
                f"case number belongs in the incident record."
            ),
            priority=5,
        )
        for referral_id in sorted(snapshot.open_referral_ids)
    )


def _headline(agent_id: str, pointers: Sequence[FocusPointer]) -> str:
    """One line: how many references, of what kinds. Counts and kinds only.

    Not a summary of what was found. A headline that said what the records
    disagree about would be the head asserting, one indirection away from the
    pointers that exist to stop exactly that.
    """
    counts: dict[str, int] = {}
    for pointer in pointers:
        counts[str(pointer.kind).lower()] = counts.get(str(pointer.kind).lower(), 0) + 1
    breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    return f"{len(pointers)} references for {agent_id}: {breakdown}."[:200]


def focus_from(
    collisions: Sequence[Collision],
    *,
    incident_id: str,
    snapshot: ProfileSnapshot,
    scope: FocusScope,
    descriptors: Sequence[AgentDescriptor],
    questions: Sequence[OpenQuestion] = (),
    self_agent_id: str = COMPOSER_ID,
    composed_by_version: str,
    composed_at: datetime,
) -> IncidentFocus:
    """Distribute the selected collisions to whoever declared the authority to act.

    The whole of the fleet decision is :func:`audience_for`, which reads
    descriptors and never an agent id. Pointers are de-duplicated per agent by
    (kind, ref) -- several collisions legitimately reach the same geometry or the
    same survey -- and capped by priority, so an agent that would have been
    handed a dozen references gets the six that matter and the rest stay in the
    profile where they already were.
    """
    per_agent: dict[str, dict[tuple[FocusKind, str], FocusPointer]] = {}
    for collision in collisions:
        for pointer in pointers_for(collision, snapshot=snapshot):
            for agent_id in audience_for(
                pointer.kind,
                descriptors,
                now=composed_at,
                exclude_agent_id=self_agent_id,
            ):
                bucket = per_agent.setdefault(agent_id, {})
                existing = bucket.get((pointer.kind, pointer.ref))
                # Keep the most urgent reason for a reference two collisions
                # both reached. The alternative -- last write wins -- would make
                # the priority depend on iteration order.
                if existing is None or pointer.priority < existing.priority:
                    bucket[(pointer.kind, pointer.ref)] = pointer

    for pointer in standing_pointers(snapshot):
        for agent_id in audience_for(
            pointer.kind, descriptors, now=composed_at, exclude_agent_id=self_agent_id
        ):
            per_agent.setdefault(agent_id, {}).setdefault((pointer.kind, pointer.ref), pointer)

    drafted: list[AgentFocus] = []
    for agent_id in sorted(per_agent):
        ordered = sorted(
            per_agent[agent_id].values(),
            key=lambda p: (p.priority, str(p.kind), p.ref),
        )[:MAX_POINTERS_PER_AGENT]
        if not ordered:
            continue
        drafted.append(
            AgentFocus(
                agent_id=agent_id,
                headline=_headline(agent_id, ordered),
                pointers=tuple(ordered),
            )
        )

    return compose_focus(
        incident_id=incident_id,
        scope=scope,
        per_agent=drafted,
        open_question_ids=tuple(q.question_id for q in questions),
        composed_by=self_agent_id,
        composed_by_version=composed_by_version,
        composed_at=composed_at,
    )


def fallback_focus(
    *,
    incident_id: str,
    snapshot: ProfileSnapshot,
    descriptors: Sequence[AgentDescriptor],
    questions: Sequence[OpenQuestion] = (),
    reported_keys: Sequence[str] = (),
    unknown_keys: Sequence[str] = (),
    self_agent_id: str = COMPOSER_ID,
    composed_by_version: str,
    composed_at: datetime,
) -> IncidentFocus:
    """The focus a graph that ran out of budget -- or never ran -- still produces.

    The profile's own open conflicts and open questions, ranked by
    :attr:`Collision.priority`, capped, and distributed the same way the graph's
    output is. Not a stub: it is the identical pipeline with the planner removed,
    so the only thing an exhausted graph costs is the *order* the collisions
    would have been considered in. An incident cannot wait for a graph, and it
    must never be handed nothing because one was slow.
    """
    collisions = detect_collisions(
        snapshot,
        reported_keys=reported_keys,
        questions=questions,
        unknown_keys=unknown_keys,
    )[:MAX_COLLISIONS]
    return focus_from(
        collisions,
        incident_id=incident_id,
        snapshot=snapshot,
        scope=focus_scope(snapshot, questions=questions, unknown_keys=unknown_keys),
        descriptors=descriptors,
        questions=questions,
        self_agent_id=self_agent_id,
        composed_by_version=composed_by_version,
        composed_at=composed_at,
    )


# --------------------------------------------------------------------- state


class FocusGraphState(GraphState):
    """What the focus graph knows. Ids and counts; no snapshot, no transcript.

    The snapshot stays on :class:`FocusComposer` rather than in the state, and
    that is not an optimisation. State is what a checkpoint serializes and what
    a driver copies between nodes; a snapshot in it would put resolved fact
    *values* -- and on a bad day a Tier II storage location -- into both.
    """

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    #: Binds everything downstream to one snapshot. See :mod:`firstdue.incident.focus`.
    profile_version: int = Field(default=0, ge=0)

    #: Intake keys the reading reported on. Keys, never the reported values.
    reported_keys: tuple[str, ...] = ()
    #: Attributes the brief asked for and found no record of.
    unknown_keys: tuple[str, ...] = ()

    #: Set by ``gather``: what the snapshot has, counted.
    conflict_count: int = Field(default=0, ge=0)
    fact_count: int = Field(default=0, ge=0)
    gathered: bool = False

    #: Set by ``recall``: the slow loop's unfinished threads, by id.
    question_ids: tuple[str, ...] = ()
    recalled: bool = False

    collisions: tuple[Collision, ...] = ()
    collided: bool = False

    #: Collision keys, in the order the graph decided to point at them.
    selected: tuple[str, ...] = ()

    def checkpoint_payload(self) -> dict[str, Any]:
        payload = super().checkpoint_payload()
        payload.update(
            {
                "incident_id": self.incident_id,
                "address_id": self.address_id,
                "profile_version": self.profile_version,
                "selected": list(self.selected),
                "open_questions": len(self.question_ids),
            }
        )
        return payload

    @property
    def unselected(self) -> tuple[Collision, ...]:
        chosen = set(self.selected)
        return tuple(c for c in self.collisions if c.key not in chosen)

    def chosen_collisions(self) -> tuple[Collision, ...]:
        """The selected collisions, in selection order."""
        by_key = {c.key: c for c in self.collisions}
        return tuple(by_key[key] for key in self.selected if key in by_key)


# --------------------------------------------------------------------- nodes


class FocusComposer:
    """The nodes of the focus graph, bound to one incident.

    A class rather than closures for the reason
    :class:`~firstdue.agents.graphs.hazard.HazardCrossCheck` gives: the nodes
    share the snapshot, the bank and the planner, and the router has to see the
    same budget the driver is charging. One object is what stops the two being
    wired up differently in the two places that build a graph.
    """

    def __init__(
        self,
        *,
        snapshot: ProfileSnapshot,
        budget: BudgetGuard,
        memory: MemoryBank | None = None,
        scopes: Collection[Scope] = (),
        planner: ReasoningPlanner | None = None,
        max_collisions: int = MAX_COLLISIONS,
    ) -> None:
        self._snapshot = snapshot
        self._budget = budget
        self._memory = memory
        self._scopes = frozenset(scopes)
        self._planner = planner or FixedOrderPlanner()
        self._max_collisions = max_collisions
        #: Recalled questions, held here rather than in the state: a question
        #: carries a sentence somebody wrote, and the state is what a checkpoint
        #: serializes. The state keeps the ids.
        self.questions: tuple[OpenQuestion, ...] = ()

    # ---------------------------------------------------------------- nodes

    async def gather(self, state: FocusGraphState) -> NodeResult:
        """Read what the incident already opened on. No I/O, no decision.

        Deliberately its own node rather than setup done before the graph
        starts: it is charged a step and it emits a span, so the trace says what
        the graph was reasoning over and not merely what it concluded.
        """
        return NodeResult(
            decision=f"gather:{len(self._snapshot.conflicts)}",
            updates={
                "gathered": True,
                "profile_version": self._snapshot.profile_version,
                "conflict_count": len(self._snapshot.conflicts),
                "fact_count": len(self._snapshot.facts),
            },
            counts={
                "conflicts": len(self._snapshot.conflicts),
                "facts": len(self._snapshot.facts),
                "reported_keys": len(state.reported_keys),
                "unknown_keys": len(state.unknown_keys),
            },
        )

    async def recall(self, state: FocusGraphState) -> NodeResult:
        """Ask the memory bank what is still open on this address.

        This is the node the feature exists for. Everything else in the incident
        loop reads one profile snapshot; this reads the threads the slow loop has
        been carrying for weeks and could not finish, and surfaces them at the
        one moment somebody is standing in front of the building.

        Scope-gated, and the gate is the bank's rather than a second one here:
        the scopes passed in are the *incident grant's*, so a Tier II thread is
        returned only to an incident whose grant carries the Tier II scope. A
        bank that is unreachable costs the questions and never the pass.
        """
        if self._memory is None:
            return NodeResult(decision="recall:no_bank", updates={"recalled": True})
        try:
            recalled = await self._memory.recall(
                district_id=state.district_id,
                address_id=state.address_id,
                scopes=self._scopes,
            )
        except Exception as exc:
            # The bank is durable memory, not a prerequisite. An incident that
            # could not reach it is an incident with less context, which is
            # exactly the situation this system was in before the bank existed.
            logger.warning(
                "focus_recall_unavailable",
                extra={"incident_id": state.incident_id, "error_type": type(exc).__name__},
            )
            return NodeResult(decision="recall:unavailable", updates={"recalled": True})

        self.questions = tuple(recalled)
        return NodeResult(
            decision=f"recall:{len(recalled)}",
            updates={
                "recalled": True,
                "question_ids": tuple(sorted(q.question_id for q in recalled)),
            },
            counts={"questions": len(recalled)},
        )

    async def collide(self, state: FocusGraphState) -> NodeResult:
        """Correlate everything read so far, by canonical key. Deterministic."""
        collisions = detect_collisions(
            self._snapshot,
            reported_keys=state.reported_keys,
            questions=self.questions,
            unknown_keys=state.unknown_keys,
        )[: self._max_collisions]
        top = collisions[0].priority if collisions else 0
        return NodeResult(
            decision=f"collide:{len(collisions)}",
            updates={"collisions": collisions, "collided": True},
            counts={
                "collisions": len(collisions),
                "top_priority": top,
                "questions": len(state.question_ids),
            },
        )

    async def select(self, state: FocusGraphState) -> NodeResult:
        """Choose which collision to point at next, from a closed list of them.

        The planner may reorder the collisions this pass already derived and may
        do nothing else: it is handed their keys and integer counts, and an
        answer that is not one of them is discarded in favour of the ranked
        order. So the worst a planner can do -- a confused model, a timeout, a
        wrong guess -- is put the second-most-urgent collision first. It cannot
        add one, remove one, or say anything about what any of them means.
        """
        options = tuple(collision.key for collision in state.unselected)
        if not options:
            return NodeResult(decision="select:none", counts={"unselected": 0})
        chosen = await self._planner.choose(
            node=NODE_SELECT,
            options=options,
            counts={
                "unselected": len(options),
                "selected": len(state.selected),
                "questions": len(state.question_ids),
                "conflicts": state.conflict_count,
            },
            deadline_ms=PLANNER_DEADLINE_MS,
        )
        picked = chosen if chosen in options else options[0]
        return NodeResult(
            decision=f"select:{picked}"[:120],
            updates={"selected": (*state.selected, picked)},
            counts={"selected": len(state.selected) + 1, "unselected": len(options) - 1},
        )

    async def close(self, state: FocusGraphState) -> NodeResult:
        """Everything worth pointing at has been pointed at."""
        return NodeResult(
            decision=f"close:{len(state.selected)}",
            updates={"stop": GraphStop.CLOSED},
            counts={"selected": len(state.selected), "collisions": len(state.collisions)},
        )

    async def park(self, state: FocusGraphState) -> NodeResult:
        """Stop, and say why. The agent composes the fallback focus from here.

        Never raises and never blocks. An incident is on a countdown, and a
        graph that could hold it up would be a worse feature than no graph --
        which is why the caller treats this as a *state* and falls back to
        :func:`fallback_focus` rather than as a failure to report.
        """
        stop = self._budget.exhausted() or GraphStop.UNRESOLVED
        return NodeResult(
            decision=f"park:{stop}",
            updates={
                "stop": stop,
                "waiting_on": (
                    f"{len(state.unselected)} collisions on {state.address_id} "
                    f"that the focus budget did not reach"
                )[:200],
            },
            counts={"selected": len(state.selected), "unselected": len(state.unselected)},
        )

    # --------------------------------------------------------------- router

    def route(self, state: FocusGraphState) -> str:
        """Where the graph goes next. Pure, and the only place the budget bites.

        Both ceilings are checked first, so an exhausted graph parks rather than
        starting a recall it cannot finish -- and so the bound holds identically
        under either driver.
        """
        if state.stop is not None:
            return STOP
        if self._budget.exhausted() is not None:
            return NODE_PARK
        if not state.gathered:
            return NODE_GATHER
        if not state.recalled:
            return NODE_RECALL
        if not state.collided:
            return NODE_COLLIDE
        if state.unselected and len(state.selected) < self._max_collisions:
            return NODE_SELECT
        return NODE_CLOSE

    def spec(self) -> GraphSpec[FocusGraphState]:
        return GraphSpec(
            state_type=FocusGraphState,
            entry=NODE_GATHER,
            nodes={
                NODE_GATHER: self.gather,
                NODE_RECALL: self.recall,
                NODE_COLLIDE: self.collide,
                NODE_SELECT: self.select,
                NODE_CLOSE: self.close,
                NODE_PARK: self.park,
            },
            router=self.route,
        )


def graph_focus(
    state: FocusGraphState,
    *,
    composer: FocusComposer,
    snapshot: ProfileSnapshot,
    descriptors: Sequence[AgentDescriptor],
    self_agent_id: str = COMPOSER_ID,
    composed_by_version: str,
    composed_at: datetime,
) -> IncidentFocus:
    """The focus a finished graph run produces, from the collisions it selected.

    Separate from the nodes on purpose, and the separation is the safety
    argument in one line: the nodes decide *which* collisions and in what order,
    and this decides what each of them becomes -- out of ids that came off the
    snapshot, through a closed list that re-checks every one of them. The two
    halves do not meet, so there is no path from a node's decision to a value.
    """
    return focus_from(
        state.chosen_collisions(),
        incident_id=state.incident_id,
        snapshot=snapshot,
        scope=focus_scope(snapshot, questions=composer.questions, unknown_keys=state.unknown_keys),
        descriptors=descriptors,
        questions=composer.questions,
        self_agent_id=self_agent_id,
        composed_by_version=composed_by_version,
        composed_at=composed_at,
    )


__all__ = [
    "BEARS_ON",
    "INTERESTS",
    "MAX_COLLISIONS",
    "MAX_POINTERS_PER_AGENT",
    "NODE_CLOSE",
    "NODE_COLLIDE",
    "NODE_GATHER",
    "NODE_RECALL",
    "NODE_SELECT",
    "Collision",
    "FocusComposer",
    "FocusGraphState",
    "Interest",
    "audience_for",
    "detect_collisions",
    "fallback_focus",
    "focus_from",
    "graph_focus",
    "pointers_for",
    "standing_pointers",
]
