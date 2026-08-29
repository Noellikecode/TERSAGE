"""Who gets woken, and what they are handed.

This is an authorisation decision wearing the costume of a routing decision, and
that is why none of it is a model call.

Deciding which agents run on an incident is deciding which service accounts do
work, under which grant, against which classifications. A model that could
choose the set would be choosing who reads Tier II filings and who talks to a
utility -- exactly what section 6 puts out of its reach. So the model's entire
contribution ends at :class:`~firstdue.incident.intake.IntakeSignals`: six typed
fields it filled in by reading a transcript. From there a **rule table** decides,
and it decides by matching against what each agent's
:class:`~firstdue.domain.registry.AgentDescriptor` *declares*.

That last part is the second half of the design. A rule never names an agent.
It names the authority the work needs -- a capability and a set of scopes -- and
whichever catalogued incident-loop agents declare that authority are the ones
woken. Three properties follow that a hand-maintained agent list would not have:

* **An agent cannot be routed work it never claimed it could do.** The catalog
  an officer reads and the wiring that runs are the same statement.
* **Adding an agent to the fleet routes it automatically**, and adding one that
  declares nothing routes it nowhere.
* **A rule that matches nobody is visible.** It lands in
  :attr:`RoutingPlan.unmatched_rule_ids` rather than silently doing nothing, the
  same way :func:`~firstdue.registry.routing.unconsumed_topics` surfaces a topic
  with no consumer. A fire department finding out that "reported hazardous
  material" wakes nothing should find it out from a plan, not from an incident.

Nothing here is tactical. A rule decides who is *told* something; no rule
decides what anyone should do about it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Capability, Loop, Scope
from firstdue.domain.keys import INTAKE_KEYS, IntakeKeys
from firstdue.domain.registry import AgentDescriptor
from firstdue.incident.intake import (
    CHANNEL_LABEL,
    IntakeReading,
    IntakeSignals,
    ReportedItem,
    signals_from,
)


class Trigger(StrEnum):
    """What makes a rule fire.

    A closed enum rather than a predicate function, so the rule table is *data*:
    greppable, printable into the incident log, and impossible to write a
    condition into that reads something other than the signal set.
    """

    ALWAYS = "ALWAYS"
    ENTRAPMENT_REPORTED = "ENTRAPMENT_REPORTED"
    HAZMAT_REPORTED = "HAZMAT_REPORTED"
    FLOOR_OF_ORIGIN_REPORTED = "FLOOR_OF_ORIGIN_REPORTED"
    ALARM_LEVEL_REPORTED = "ALARM_LEVEL_REPORTED"
    ACCESS_REPORTED = "ACCESS_REPORTED"


def fires(trigger: Trigger, signals: IntakeSignals) -> bool:
    """Whether one trigger fires for one signal set.

    Total over the enum and reads nothing else. Every argument it can see is a
    bool or a bounded int that :func:`~firstdue.incident.intake.signals_from`
    derived deterministically -- there is no confidence, no model reference and
    no free text in scope, which is what makes "the model cannot influence
    routing" a property of the code rather than a claim about it.
    """
    match trigger:
        case Trigger.ALWAYS:
            return True
        case Trigger.ENTRAPMENT_REPORTED:
            return signals.entrapment_reported
        case Trigger.HAZMAT_REPORTED:
            return signals.hazmat_reported
        case Trigger.FLOOR_OF_ORIGIN_REPORTED:
            return signals.reported_floor_of_origin is not None
        case Trigger.ALARM_LEVEL_REPORTED:
            return signals.reported_alarm_level is not None
        case Trigger.ACCESS_REPORTED:
            return signals.access_reported


class WakeRule(BaseModel):
    """One row of the table: a condition, an authority, and what is handed over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=80)
    trigger: Trigger
    #: The capability an agent must **declare** to receive this handoff.
    required_capabilities: frozenset[Capability] = Field(min_length=1)
    #: The scopes it must declare. Both are matched against the descriptor, not
    #: against the grant: the grant says what this incident authorised, the
    #: descriptor says what the agent is *for*, and routing is about the second.
    required_scopes: frozenset[Scope] = Field(min_length=1)
    #: Which reported attributes travel with the handoff.
    hands_over: tuple[str, ...] = ()
    #: Why this row exists, in one sentence, for the officer reading the plan.
    why: str = Field(min_length=1, max_length=300)


#: The table. Order is fixed so a plan is byte-identical across replays.
WAKE_RULES: Final[tuple[WakeRule, ...]] = (
    WakeRule(
        rule_id="intake-is-recorded",
        trigger=Trigger.ALWAYS,
        required_capabilities=frozenset({Capability.READ, Capability.WRITE}),
        required_scopes=frozenset({Scope.WRITE_RMS}),
        hands_over=INTAKE_KEYS,
        why=(
            "The narrative that opened the incident is part of the incident's "
            "record, whether or not it said anything worth routing."
        ),
    ),
    WakeRule(
        rule_id="reported-entrapment-reaches-the-notifier",
        trigger=Trigger.ENTRAPMENT_REPORTED,
        required_capabilities=frozenset({Capability.NOTIFY}),
        required_scopes=frozenset({Scope.NOTIFY_AGENCY}),
        hands_over=(IntakeKeys.ENTRAPMENT_REPORTED, IntakeKeys.REPORTED_FLOOR_OF_ORIGIN),
        why=(
            "A caller who says somebody is inside is the reason mutual aid and "
            "EMS are told anything at all. What is passed on is that they said "
            "it, marked as a report."
        ),
    ),
    WakeRule(
        rule_id="reported-hazardous-material-reaches-the-notifier",
        trigger=Trigger.HAZMAT_REPORTED,
        required_capabilities=frozenset({Capability.NOTIFY}),
        required_scopes=frozenset({Scope.NOTIFY_AGENCY}),
        hands_over=(IntakeKeys.HAZMAT_REPORTED,),
        why=(
            "County hazmat and OEM are notified on what was reported at the "
            "scene, separately from whatever the Tier II filings say."
        ),
    ),
    WakeRule(
        rule_id="reported-hazardous-material-is-checked-against-tier-ii",
        trigger=Trigger.HAZMAT_REPORTED,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_TIER_II_METADATA}),
        hands_over=(IntakeKeys.HAZMAT_REPORTED,),
        why=(
            "A material named on a phone should be checked against the filings. "
            "No incident-loop agent declares read:tier-ii-metadata, so this rule "
            "fires and matches nobody -- which is a gap the plan states rather "
            "than one the incident discovers."
        ),
    ),
    WakeRule(
        rule_id="reported-floor-of-origin-reaches-the-thermal-scan",
        trigger=Trigger.FLOOR_OF_ORIGIN_REPORTED,
        required_capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_GEOMETRY}),
        hands_over=(IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,),
        why=(
            "The storey a caller named is where a thermal pass has a reason to "
            "look first. The geometry it is registered against stays the "
            "measured one."
        ),
    ),
    WakeRule(
        rule_id="reported-access-restriction-reaches-the-street-authority",
        trigger=Trigger.ACCESS_REPORTED,
        required_capabilities=frozenset({Capability.NOTIFY}),
        required_scopes=frozenset({Scope.NOTIFY_AGENCY, Scope.REQUEST_ROAD_CLOSURE}),
        hands_over=(IntakeKeys.ACCESS_NOTE,),
        why=(
            "A blocked approach is a street matter, so it goes to the agent that "
            "declares the road-closure scope -- one that only declared "
            "notify:agency would not be handed it."
        ),
    ),
    WakeRule(
        rule_id="reported-alarm-level-is-recorded-never-applied",
        trigger=Trigger.ALARM_LEVEL_REPORTED,
        required_capabilities=frozenset({Capability.READ, Capability.WRITE}),
        required_scopes=frozenset({Scope.WRITE_RMS}),
        hands_over=(IntakeKeys.REPORTED_ALARM_LEVEL,),
        why=(
            "A caller saying 'this is a second alarm' is recorded and never "
            "applied. The alarm level bounds the incident grant, so a caller who "
            "could raise it could widen the fleet's authority."
        ),
    ),
)


#: The table, by rule id. A plan names the rules that fired and the rules that
#: matched nobody, and both are only ids -- so anything explaining one of them
#: to an officer has to be able to reach back to the sentence the rule already
#: carries about why it exists, rather than inventing a second one beside it.
RULES_BY_ID: Final[dict[str, WakeRule]] = {rule.rule_id: rule for rule in WAKE_RULES}


class Handoff(BaseModel):
    """One agent, and everything the intake gives it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    #: ``agent_id@version``. Pinned, because a run record has to name what ran.
    agent_ref: str = Field(min_length=1, max_length=160)
    #: Every rule that selected this agent, sorted. More than one is normal.
    rule_ids: tuple[str, ...] = ()
    #: The reported items themselves. Handed in process, never inside an
    #: ``AgentInput`` -- an envelope carries identifiers, never record content.
    items: tuple[ReportedItem, ...] = ()
    #: One line for the incident log and the console. Attribution, no advice.
    note: str = Field(min_length=1, max_length=500)

    @property
    def intake_keys(self) -> tuple[str, ...]:
        return tuple(sorted({item.intake_key for item in self.items}))


class WithheldHandoff(BaseModel):
    """An agent a rule selected that this incident's authority cannot cover.

    Recorded rather than dropped, and rather than woken-and-denied. Waking an
    agent whose declared scopes the incident grant does not carry produces a
    ``DENIED`` run on every incident: real, correct, and indistinguishable in a
    log from an agent that was denied for a reason somebody should look at. So
    the check moves in front of the wake, and what would have been a denial
    becomes a stated gap naming the exact scope that is missing.

    A withheld handoff is a question for whoever owns the catalog, not a
    runtime error: either the descriptor declares a scope the agent does not
    need, or the incident grant is missing one it does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    agent_ref: str = Field(min_length=1, max_length=160)
    rule_ids: tuple[str, ...] = ()
    #: Declared by the agent, absent from the incident grant. Sorted.
    missing_scopes: tuple[str, ...] = ()


class RoutingPlan(BaseModel):
    """What the interceptor decided, and what it could not place.

    Frozen and fully derived: rebuilding it from the same reading and the same
    catalog produces the same plan, which is what a replay checks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    handoffs: tuple[Handoff, ...] = ()
    #: Rules that fired. Recorded even when they matched, so the log says why
    #: an agent was woken rather than only that it was.
    fired_rule_ids: tuple[str, ...] = ()
    #: Rules that fired and matched no catalogued agent. A stated gap.
    unmatched_rule_ids: tuple[str, ...] = ()
    #: Agents matched by a rule but outside this incident's authority.
    withheld: tuple[WithheldHandoff, ...] = ()

    @property
    def woken_agent_ids(self) -> tuple[str, ...]:
        return tuple(handoff.agent_id for handoff in self.handoffs)

    @property
    def withheld_agent_ids(self) -> tuple[str, ...]:
        return tuple(entry.agent_id for entry in self.withheld)


def eligible(
    descriptor: AgentDescriptor,
    rule: WakeRule,
    *,
    now: datetime,
    exclude_agent_id: str,
) -> bool:
    """Whether one catalogued agent may receive one rule's handoff.

    Five clauses, and the two that carry the design are the last two: the
    agent's **declared** capabilities must cover what the rule needs, and its
    **declared** scopes must cover what the rule needs. An agent that would in
    fact be able to do the work but never said so in the catalog is not woken,
    because the catalog is what a department reads to find out what this fleet
    can reach.
    """
    return (
        descriptor.agent_id != exclude_agent_id
        and descriptor.loop is Loop.INCIDENT
        and not descriptor.is_deprecated(now)
        and rule.required_capabilities <= descriptor.capabilities
        and rule.required_scopes <= descriptor.required_scopes
    )


def _note(agent_id: str, reading: IntakeReading, items: Sequence[ReportedItem]) -> str:
    """The line that travels with a handoff.

    States the channel, the attributes, and nothing else. No verb of advice
    appears here and none may: the interceptor routes information, and an agent
    reading a suggestion out of a handoff would be acting on one.
    """
    channel = CHANNEL_LABEL[reading.channel]
    attributes = ", ".join(sorted({item.intake_key for item in items})) or "no attributes"
    return (
        f"Handed to {agent_id} from the {channel} ({reading.source_ref}): "
        f"{attributes}. Reported, not observed."
    )[:500]


def plan_handoffs(
    reading: IntakeReading,
    *,
    descriptors: Sequence[AgentDescriptor],
    now: datetime,
    self_agent_id: str,
    authorised_scopes: frozenset[Scope] | None = None,
    rules: Sequence[WakeRule] = WAKE_RULES,
) -> RoutingPlan:
    """Decide who is woken. Deterministic, and blind to the model.

    The only thing this reads out of ``reading`` is
    :func:`~firstdue.incident.intake.signals_from` -- six typed fields -- plus
    the items belonging to the keys a matched rule declared it hands over. It
    never reads ``model_ref``, ``model_confidence``, ``rejection_reason`` or
    ``unknowns``, so two readings that differ only in what the model said about
    itself produce the same plan.

    A rejected reading is not special-cased. It simply has no items, so every
    signal is false and only the unconditional rules fire: if Vertex is down,
    the intake is recorded and nobody else is woken.

    ``authorised_scopes`` is the incident grant's scope set. It never *adds* an
    agent -- it only withholds one the catalog matched but this incident's
    authority does not cover, which is the difference between routing and
    generating a denial per incident. Omitting it plans against the catalog
    alone, which is what a unit test wants and what a production caller should
    not do.
    """
    signals = signals_from(reading)
    by_agent: dict[str, list[str]] = {}
    items_by_agent: dict[str, dict[str, ReportedItem]] = {}
    refs: dict[str, str] = {}
    fired: list[str] = []
    unmatched: list[str] = []
    withheld: dict[str, tuple[AgentDescriptor, list[str]]] = {}

    for rule in rules:
        if not fires(rule.trigger, signals):
            continue
        fired.append(rule.rule_id)
        matched = sorted(
            (d for d in descriptors if eligible(d, rule, now=now, exclude_agent_id=self_agent_id)),
            key=lambda d: d.agent_id,
        )
        if not matched:
            unmatched.append(rule.rule_id)
            continue
        handed = [item for item in reading.items if item.intake_key in rule.hands_over]
        for descriptor in matched:
            if authorised_scopes is not None and not (
                descriptor.required_scopes <= authorised_scopes
            ):
                _, rule_ids = withheld.setdefault(descriptor.agent_id, (descriptor, []))
                rule_ids.append(rule.rule_id)
                continue
            by_agent.setdefault(descriptor.agent_id, []).append(rule.rule_id)
            refs[descriptor.agent_id] = descriptor.ref
            bucket = items_by_agent.setdefault(descriptor.agent_id, {})
            for item in handed:
                # Keyed by (key, offset) so one attribute reported twice in one
                # transcript survives, and one attribute matched by two rules
                # does not become two lines saying the same thing.
                bucket[f"{item.intake_key}:{item.span.start_offset}"] = item

    handoffs = tuple(
        Handoff(
            agent_id=agent_id,
            agent_ref=refs[agent_id],
            rule_ids=tuple(sorted(set(rule_ids))),
            items=tuple(item for _, item in sorted(items_by_agent.get(agent_id, {}).items())),
            note=_note(agent_id, reading, list(items_by_agent.get(agent_id, {}).values())),
        )
        for agent_id, rule_ids in sorted(by_agent.items())
    )
    return RoutingPlan(
        incident_id=reading.incident_id,
        handoffs=handoffs,
        fired_rule_ids=tuple(fired),
        unmatched_rule_ids=tuple(unmatched),
        withheld=tuple(
            WithheldHandoff(
                agent_id=agent_id,
                agent_ref=descriptor.ref,
                rule_ids=tuple(sorted(set(rule_ids))),
                missing_scopes=tuple(
                    sorted(
                        str(scope)
                        for scope in descriptor.required_scopes - (authorised_scopes or frozenset())
                    )
                ),
            )
            for agent_id, (descriptor, rule_ids) in sorted(withheld.items())
        ),
    )


__all__ = [
    "RULES_BY_ID",
    "WAKE_RULES",
    "Handoff",
    "RoutingPlan",
    "Trigger",
    "WakeRule",
    "WithheldHandoff",
    "eligible",
    "fires",
    "plan_handoffs",
]
