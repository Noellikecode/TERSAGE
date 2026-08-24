"""Who has to be told, what each of them is told, and who is told nothing twice.

``agency-notifier`` has always had a rule table: a solar array on the record
means the utility hears about it, a Tier II filing means the county does. The
table is right and it stays. What it could not do is the part a chief's aide
does on the radio -- notice that the *head agent is pointing at this condition
right now*, notice that this partner was already told forty minutes ago and has
not answered, and say the same fact to a gas utility and to a mutual-aid
battalion in the two different sentences those two audiences need.

That is what this graph adds, and it is bounded on four sides.

**The model may not author a hazard.** Every clause in every draft is a fixed
sentence attached to a :class:`PartnerRule`, and a rule fires only when the
canonical key it names carries a *known* value on the incident's own profile
snapshot. The fact id travels onto the draft and is printed in the text. A
polished draft that names a hazard whose key is not among the conditions this
draft was built from is rejected by :func:`reject_notification_draft` and the
deterministic text ships. The check is here rather than in the prompt because a
prompt is a request and a check is a guarantee -- the same argument
:func:`firstdue.agents.actions._reject_draft` makes about a referral, and the
stakes are higher: a referral is read next week by a plans examiner, and this is
read in four minutes by somebody deciding whether to cut a gas main.

**Nothing here is tactical.** A notification states conditions and cites the
records they came from. It does not tell a battalion chief to go defensive, does
not order an evacuation, and does not predict what this fire will do -- see
"What the system will never do" in ``docs/architecture.md``. Tactical vocabulary
in a polished draft is a rejection, not a style note.

**The graph decides who to call; the gateway decides what a call may do.**
Nothing in this module writes, stages, or sends. It returns a
:class:`NotificationPlan`, and :class:`~firstdue.incident.resources.ResourceAgent`
puts every entry in it through :meth:`PolicyEngine.decide` exactly as it always
has. So a graph that decided a gas shutoff was warranted produces a *staged
request with a chief's name on it* and no gas main moves, and that is a property
of there being no code path from here to
:class:`~firstdue.ports.writes.ExternalWriteTarget` rather than of this module
being careful.

**Late beats never.** The budget is the descriptor's own five seconds
(:func:`~firstdue.agents.graphs.base.graph_budget`) and a hard step bound, both
checked in the router. A graph that exhausts either one parks, and the agent
ships :func:`deterministic_plan` -- today's rule table, no focus, no polish, no
suppression. A partner notified late by a fallback beats a partner not notified
because a graph was thinking.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

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
from firstdue.domain.enums import Department, LogEntryType
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import IntegerValue, QuantityValue
from firstdue.observability.logging import get_logger
from firstdue.ports.model import ModelClient, ProseResult
from firstdue.ports.repositories import IncidentLogRepository

logger = get_logger(__name__)

AGENT_ID: Final[str] = "agency-notifier"

NODE_ASSESS: Final[str] = "assess"
NODE_MATCH: Final[str] = "match"
NODE_SUPPRESS: Final[str] = "suppress"
NODE_DRAFT: Final[str] = "draft"
NODE_CLOSE: Final[str] = "close"

#: The template the wording model is asked for. Named and versioned because a
#: reworded template is a different prompt and the recorded traces should say so.
NOTIFICATION_TEMPLATE_ID: Final[str] = "partner-notification-v1"

#: How long a draft may be. Bounded by what a chief's approval card can show:
#: :meth:`ResourceAgent._stage` puts the intent and this detail into
#: ``prefilled_summary``, which is capped at 500 characters, and a chief reading
#: a truncated sentence is a chief reading half of why they are being asked.
NOTIFICATION_DRAFT_MAX_CHARS: Final[int] = 400

#: One wording call. Well inside the agent's five seconds, and a partner is
#: better notified in plain English than not notified in polished English.
NOTIFICATION_DRAFT_DEADLINE_MS: Final[int] = 1_500

#: The sentence that has to survive every draft. It is what makes the message a
#: notification rather than an instruction, and a model that drops it has
#: changed what the message *is*.
NOTIFICATION_DISCLAIMER: Final[str] = "Conditions on record only; not a tactical instruction."

#: How long a partner has to answer before a repeat notice is warranted. Ten
#: minutes is a working figure, not a standard: what matters is that "already
#: told" and "told and ignored" produce different behaviour at all.
ESCALATE_AFTER_SECONDS: Final[float] = 600.0

#: Clauses one draft may carry. A partner given six conditions reads none of
#: them, and the ones past the third are the ones the rule table ranked lowest.
MAX_CLAUSES_PER_DRAFT: Final[int] = 3


# ------------------------------------------------------------- the rule table


@dataclass(frozen=True, slots=True)
class PartnerRule:
    """One condition on the record, and the one partner it means something to.

    ``clause`` is a fixed sentence. It is the only prose in this module that
    describes a hazard, it names no value the record does not carry, and the
    optional ``{value}`` placeholder is filled from the fact's own
    :meth:`~firstdue.domain.values._ValueBase.render` -- so a clause is a
    restatement of a stored observation and never an inference from one.
    """

    rule_id: str
    #: The canonical key whose *known* value makes this rule fire.
    canonical_key: str
    #: The :data:`firstdue.incident.resources.ALL_KINDS` entry this rule asks
    #: for. Cross-checked against that catalog by the unit tests, because a
    #: rule naming a kind that does not exist is a partner nobody calls.
    kind_id: str
    #: Who hears it. Must match the kind's ``receiving_department``.
    audience: Department
    clause: str
    #: Ordering only. It decides which partner is called first and which clause
    #: is dropped when a draft will not fit; it is never rendered.
    urgency: int
    #: True when this call is made *only* because the head agent's briefing
    #: pointed at the condition. Every gated commitment sits behind this flag:
    #: the record alone tells a partner what is there, and it takes the
    #: interceptor's own focus to turn that into a request for a chief.
    on_focus_only: bool = False


#: What this agent knows how to tell whom. Ordered by urgency for readability;
#: nothing depends on the order of the tuple itself.
#:
#: Read it as three pairs. A photovoltaic array is a *utility* condition,
#: because the DC side of it is still live after the service is pulled and the
#: crew cannot reach it from the panel -- and when the interceptor is pointing
#: at that array, the same fact is grounds for staging an electric shutoff a
#: chief can tap. A PHMSA pipeline is a *county* condition, and a pointed-at
#: pipeline is the most urgent call this agent makes. A Tier II filing is a
#: county condition and, pointed at, a hazmat request. The EV charger is
#: mutual-aid's business: an arriving battalion should know there is lithium in
#: the garage before it commits, and nobody needs to spend a resource over it.
PARTNER_RULES: Final[tuple[PartnerRule, ...]] = (
    PartnerRule(
        rule_id="notify.pipeline.oem",
        canonical_key=Keys.HAZARD_PIPELINE_PROXIMITY_M,
        kind_id="county-oem",
        audience=Department.COUNTY_OEM,
        clause="a PHMSA-regulated pipeline is on record {value} from this address",
        urgency=4,
    ),
    PartnerRule(
        rule_id="notify.pipeline.gas-shutoff",
        canonical_key=Keys.HAZARD_PIPELINE_PROXIMITY_M,
        kind_id="gas-shutoff",
        audience=Department.UTILITY,
        clause="a PHMSA-regulated pipeline is on record {value} from this address",
        urgency=5,
        on_focus_only=True,
    ),
    PartnerRule(
        rule_id="notify.tier-ii.oem",
        canonical_key=Keys.HAZARD_TIER_II_PRESENT,
        kind_id="county-oem",
        audience=Department.COUNTY_OEM,
        clause="a Tier II hazardous-materials filing is on record for this address",
        urgency=4,
    ),
    PartnerRule(
        rule_id="notify.tier-ii.hazmat",
        canonical_key=Keys.HAZARD_TIER_II_PRESENT,
        kind_id="hazmat-team",
        audience=Department.COUNTY_OEM,
        clause="a Tier II hazardous-materials filing is on record for this address",
        urgency=5,
        on_focus_only=True,
    ),
    PartnerRule(
        rule_id="notify.solar.utility",
        canonical_key=Keys.HAZARD_SOLAR_ARRAY,
        kind_id="utility-conditions",
        audience=Department.UTILITY,
        clause="a photovoltaic array is on record; its DC side stays live after the service is cut",
        urgency=3,
    ),
    PartnerRule(
        rule_id="notify.solar.electric-shutoff",
        canonical_key=Keys.HAZARD_SOLAR_ARRAY,
        kind_id="electric-shutoff",
        audience=Department.UTILITY,
        clause="a photovoltaic array is on record; its DC side stays live after the service is cut",
        urgency=4,
        on_focus_only=True,
    ),
    PartnerRule(
        rule_id="notify.ev-charger.mutual-aid",
        canonical_key=Keys.HAZARD_EV_CHARGER,
        kind_id="mutual-aid",
        audience=Department.FIRE,
        clause="an EV charger is on record; lithium-ion storage may be present",
        urgency=2,
    ),
    PartnerRule(
        rule_id="notify.unpermitted.building",
        canonical_key=Keys.UNPERMITTED_CONSTRUCTION,
        kind_id="building-department",
        audience=Department.BUILDING,
        clause="unpermitted construction is on record for this address",
        urgency=1,
    ),
)

#: Every canonical key any rule watches. The closed set a focus pointer may
#: name: a pointer at anything else is a pointer this agent has no partner for.
WATCHED_KEYS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(rule.canonical_key for rule in PARTNER_RULES)
)


# -------------------------------------------------------------- the audiences


#: How each partner is addressed. The same incident reads differently to a gas
#: utility, to a mutual-aid battalion, and to county OEM, and the difference is
#: what they are expected to *do about it* -- so the opener says who is calling
#: and why, and the clauses that follow are identical restatements of the same
#: stored facts.
_OPENERS: Final[dict[Department, str]] = {
    Department.UTILITY: "SFFD, incident in progress at {address_id}. Utility conditions on file:",
    Department.FIRE: "SFFD mutual-aid notice, incident at {address_id}. On file for the address:",
    Department.COUNTY_OEM: "SFFD situational-awareness notice, incident at {address_id}. On file:",
    Department.POLICE: "SFFD, incident at {address_id}. Street conditions on file:",
    Department.BUILDING: "SFFD, incident at {address_id}. Recorded building conditions:",
    Department.WATER: "SFFD, incident at {address_id}. Water-supply conditions on file:",
    Department.PUBLIC_WORKS: "SFFD, incident at {address_id}. Conditions on file:",
}

_DEFAULT_OPENER: Final[str] = "SFFD, incident at {address_id}. Conditions on file:"

#: Prefixed to a draft the partner has already been sent once and not answered.
_REPEAT_PREFIX: Final[str] = "Repeat notice, no acknowledgement on file."


# ---------------------------------------------------------------- validation


_FACT_ID_TOKEN: Final[re.Pattern[str]] = re.compile(r"fact_[0-9a-f]{8,}")

#: Hazard words, and the canonical key that entitles a draft to use them. A
#: polished draft that says "pipeline" for an incident whose record carries no
#: pipeline fact has authored a hazard, which is the one thing the model may
#: never do -- and it is the failure mode a wording model actually has, because
#: "utility" and "gas main" are words that travel together in its training data.
_HAZARD_VOCABULARY: Final[tuple[tuple[str, str], ...]] = (
    ("solar", Keys.HAZARD_SOLAR_ARRAY),
    ("photovoltaic", Keys.HAZARD_SOLAR_ARRAY),
    ("pv array", Keys.HAZARD_SOLAR_ARRAY),
    ("pipeline", Keys.HAZARD_PIPELINE_PROXIMITY_M),
    ("gas main", Keys.HAZARD_PIPELINE_PROXIMITY_M),
    ("lithium", Keys.HAZARD_EV_CHARGER),
    ("ev charger", Keys.HAZARD_EV_CHARGER),
    ("tier ii", Keys.HAZARD_TIER_II_PRESENT),
    ("hazardous-materials", Keys.HAZARD_TIER_II_PRESENT),
    ("hazardous materials", Keys.HAZARD_TIER_II_PRESENT),
    ("unpermitted", Keys.UNPERMITTED_CONSTRUCTION),
)

#: Words that turn a notification into an order. This project does not tell an
#: incident commander what to do, and it does not tell a partner agency what to
#: tell one either.
_TACTICAL_VOCABULARY: Final[tuple[str, ...]] = (
    "evacuat",
    "defensive",
    "offensive",
    "attack line",
    "ventilat",
    "recommend",
    "advise",
    "should ",
    "must ",
    "deploy",
)


def reject_notification_draft(result: ProseResult, *, draft: NotificationDraft) -> str | None:
    """Why a polished draft cannot be sent, or ``None`` when it can.

    Every rule is phrased against the *draft's own evidence* -- the conditions
    it was built from and the fact ids it cites -- rather than against the
    deterministic sentences, because a check phrased against the template would
    pass any draft that copied the template and added a claim beside it.

    Rejection is not an error. The deterministic text ships and the partner gets
    a notification that is worse-written and exactly as true.
    """
    if not result.accepted:
        return "not_accepted"

    text = result.text.strip()
    if not text:
        return "empty"
    if len(text) > NOTIFICATION_DRAFT_MAX_CHARS:
        return "over_length"

    if NOTIFICATION_DISCLAIMER not in text:
        return "disclaimer_dropped"
    if any(fact_id not in text for fact_id in draft.cited_fact_ids):
        return "fact_id_dropped"
    if set(_FACT_ID_TOKEN.findall(text)) - set(draft.cited_fact_ids):
        return "fact_id_introduced"

    # The disclaimer is removed before the vocabulary scans: it is the one
    # sentence that is allowed to contain the word "tactical", and a scan that
    # tripped on it would reject every well-formed draft there is.
    scanned = text.replace(NOTIFICATION_DISCLAIMER, " ").lower()
    if any(term in scanned for term in _TACTICAL_VOCABULARY):
        return "tactical_language"
    sourced = set(draft.condition_keys)
    if any(term in scanned and key not in sourced for term, key in _HAZARD_VOCABULARY):
        return "unsourced_hazard"
    return None


# --------------------------------------------------------------------- state


class Condition(BaseModel):
    """One watched key that the incident's own snapshot carries a value for.

    A condition is a *pointer at a stored observation*: the key, the fact id
    that observation was written under, and -- for a measured quantity only --
    the way the record itself renders it. Nothing here is derived, averaged, or
    inferred, which is what makes citing it in a message to another agency an
    act of quotation rather than an assertion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_key: str = Field(min_length=1, max_length=120)
    fact_id: str = Field(min_length=1, max_length=120)
    #: How the record renders a measured value, for the ``{value}`` placeholder.
    #: Quantities and integers only: a free-text value on a hazard key is a
    #: Tier II storage location, and that does not travel to a mutual-aid pager.
    rendered: str = Field(default="", max_length=60)
    #: True when the head agent's briefing pointed at this condition.
    focused: bool = False
    #: The pointer's own priority on the briefing's scale -- **1 is highest and
    #: 5 is lowest**, per :class:`~firstdue.incident.focus.FocusPointer`, and 0
    #: means nothing pointed here. Kept on the composer's scale rather than
    #: flipped on the way in, because a number that means the opposite of what
    #: the field it came from means is the bug nobody finds until an ordering
    #: looks wrong at three in the morning. It is inverted once, in
    #: :func:`_focus_bonus`, and used for ordering and nothing else.
    focus_priority: int = Field(default=0, ge=0, le=5)


class NotificationDraft(BaseModel):
    """One partner, and exactly what they are told.

    Built in two passes: :meth:`PartnerNotification.match` decides the partner
    and the evidence, :meth:`PartnerNotification.draft` writes the words. The
    split is why a suppressed partner never costs a wording call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind_id: str = Field(min_length=1, max_length=60)
    audience: Department
    #: The keys this draft is entitled to speak about. The validator's whole
    #: notion of "sourced".
    condition_keys: tuple[str, ...] = ()
    #: The observations quoted in the text, printed in it so the partner can
    #: pull the same records.
    cited_fact_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    #: What is sent. Empty until ``draft`` has run.
    detail: str = Field(default="", max_length=NOTIFICATION_DRAFT_MAX_CHARS)
    urgency: int = Field(default=0, ge=0, le=9)
    #: True when the head agent's briefing is why this partner is on the list.
    from_focus: bool = False
    #: True when this partner was told once already and did not answer.
    escalation: bool = False
    #: True when a validated polish shipped. False means the deterministic text
    #: did, which is the case worth counting.
    polished: bool = False


class NotificationPlan(BaseModel):
    """Who this pass decided to call, and how it got there.

    A plan is inert. It names kinds and carries text; it holds no repository,
    no target, and no authority, and the only thing that can act on it is
    :meth:`~firstdue.incident.resources.ResourceAgent.notify`, which puts every
    entry through the gateway one at a time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    drafts: tuple[NotificationDraft, ...] = ()
    #: Partners already told, and not yet due a repeat.
    suppressed: tuple[str, ...] = ()
    #: Why the graph stopped. ``CLOSED`` is the only one that used the graph's
    #: own drafts; every other value means :func:`deterministic_plan` shipped.
    stop: GraphStop = GraphStop.CLOSED
    steps: int = Field(default=0, ge=0)
    #: True when the rule table alone produced this -- no focus, no polish, no
    #: suppression. The default, and what every process running today gets.
    deterministic: bool = True
    #: Focus pointers naming something this agent has no partner for. Counted
    #: rather than acted on: an unresolvable pointer is dropped, never guessed.
    unresolved_pointers: int = Field(default=0, ge=0)


class NotifierGraphState(GraphState):
    """What one notification pass knows.

    The snapshot lives here and goes no further: it is the incident's own
    profile read, and :meth:`checkpoint_payload` below is deliberately narrower
    than this class is.
    """

    incident_id: str = Field(min_length=1, max_length=120)
    #: The incident's profile read. Every condition comes from this and from
    #: nowhere else.
    snapshot: ProfileSnapshot
    #: Wall-clock now, supplied by the agent. Nodes have no clock -- see the
    #: note on :meth:`~firstdue.agents.graphs.hazard.HazardCrossCheck.park` --
    #: and "has this partner had ten minutes to answer" is a question about
    #: real time rather than about the graph's own budget.
    now: datetime
    conditions: tuple[Condition, ...] = ()
    #: The head agent's one-line briefing for this agent, if there is one.
    focus_headline: str = Field(default="", max_length=200)
    #: Pointers that named nothing this agent watches.
    unresolved_pointers: int = Field(default=0, ge=0)
    drafts: tuple[NotificationDraft, ...] = ()
    #: Partners already told and not yet due a repeat.
    suppressed: tuple[str, ...] = ()
    #: The call the planner put first, if it chose one that was on offer.
    lead: str = Field(default="", max_length=60)
    #: Nodes that have run. The router is a pure function of this.
    done: tuple[str, ...] = ()

    def checkpoint_payload(self) -> dict[str, Any]:
        """Identifiers, keys, and counts. Never a draft and never a value.

        A checkpoint outlives the incident and is read by a later pass. The
        drafts are messages about a burning building addressed to other
        agencies; the keys and kind ids say everything a resumed pass needs and
        nothing a records request would be embarrassed by.
        """
        payload = super().checkpoint_payload()
        payload.update(
            {
                "incident_id": self.incident_id,
                "condition_keys": [condition.canonical_key for condition in self.conditions],
                "planned_kind_ids": [draft.kind_id for draft in self.drafts],
                "suppressed": list(self.suppressed),
            }
        )
        return payload


# ------------------------------------------------------------ what is on file


def conditions_on_record(snapshot: ProfileSnapshot) -> tuple[Condition, ...]:
    """Every watched key the snapshot carries a known, positive value for.

    Pure, and the floor under everything else here. ``False`` is not a
    condition: ``hazard.solar_array = false`` is the record saying there is no
    array, and a notification about it would be this agent inventing a hazard
    out of a negative filing. Absence -- ``UNKNOWN``, ``UNAVAILABLE``,
    ``WITHHELD`` -- is not a condition either, for the stronger reason that the
    hazard watcher writes those precisely to stop absence reading as presence.
    """
    found: list[Condition] = []
    for key in WATCHED_KEYS:
        fact = snapshot.facts.get(key)
        if fact is None or not fact.value.is_known:
            continue
        raw = fact.value.unwrap()
        if isinstance(raw, bool) and not raw:
            continue
        rendered = (
            fact.value.render()[:60] if isinstance(fact.value, QuantityValue | IntegerValue) else ""
        )
        found.append(Condition(canonical_key=key, fact_id=fact.fact_id, rendered=rendered))
    return tuple(found)


def _clause_for(rule: PartnerRule, condition: Condition) -> str:
    """The rule's fixed sentence, with the record's own rendering in it."""
    if "{value}" not in rule.clause:
        return rule.clause
    return rule.clause.format(value=condition.rendered or "an unstated distance")


def _fit(opener: str, clauses: Sequence[str], fact_ids: Sequence[str], *, repeat: bool) -> str:
    """Assemble a draft that fits, dropping the least urgent clause until it does.

    Truncation is not an option: this text is what a chief reads on an approval
    card, and half a sentence about a pipeline is worse than one fewer sentence
    about a solar array. The clauses arrive most-urgent-first, so what falls off
    the end is what the rule table itself ranked lowest.
    """
    kept = list(clauses[:MAX_CLAUSES_PER_DRAFT])
    kept_ids = list(fact_ids[: len(kept)])
    while kept:
        body = "; ".join(kept)
        text = (
            f"{_REPEAT_PREFIX} " if repeat else ""
        ) + f"{opener} {body}. Records: {', '.join(kept_ids)}. {NOTIFICATION_DISCLAIMER}"
        if len(text) <= NOTIFICATION_DRAFT_MAX_CHARS:
            return text
        kept.pop()
        kept_ids.pop()
    return (f"{_REPEAT_PREFIX} " if repeat else "") + f"{opener} {NOTIFICATION_DISCLAIMER}"


def deterministic_detail(
    draft: NotificationDraft, *, pairs: Sequence[tuple[PartnerRule, Condition]], address_id: str
) -> tuple[str, tuple[str, ...]]:
    """The text this partner gets when no model is involved, and what it cites.

    The floor and the fallback both. It is assembled from the rules' own fixed
    clauses and the fact ids of the observations that fired them, so it is true
    by construction -- and a polished draft that cannot beat it is discarded in
    its favour rather than repaired.
    """
    opener = _OPENERS.get(draft.audience, _DEFAULT_OPENER).format(address_id=address_id)
    ordered = sorted(pairs, key=lambda pair: (-pair[0].urgency, pair[0].rule_id))
    clauses = [_clause_for(rule, condition) for rule, condition in ordered]
    fact_ids = [condition.fact_id for _, condition in ordered]
    text = _fit(opener, clauses, fact_ids, repeat=draft.escalation)
    cited = tuple(fact_id for fact_id in fact_ids if fact_id in text)
    return text, cited


def deterministic_plan(snapshot: ProfileSnapshot) -> NotificationPlan:
    """Today's rule table, and exactly today's rule table.

    Reached three ways, all of them ordinary: no incident log wired, so there is
    no focus and no notification history to reason about; the graph out of time
    or out of steps; a graph that ended anywhere but ``CLOSED``. In all three the
    partners the record calls for are called, in the wording the table has
    always used, and the ones that only a focus pointer would have added are
    not -- because nothing pointed at them.
    """
    conditions = conditions_on_record(snapshot)
    drafts = _match_drafts(conditions, lead="")
    written = tuple(
        _apply_deterministic(draft, conditions=conditions, address_id=snapshot.address_id)
        for draft in drafts
    )
    return NotificationPlan(drafts=written, deterministic=True)


def _rules_for(draft: NotificationDraft) -> tuple[PartnerRule, ...]:
    by_id = {rule.rule_id: rule for rule in PARTNER_RULES}
    return tuple(by_id[rule_id] for rule_id in draft.rule_ids if rule_id in by_id)


def _pairs_for(
    draft: NotificationDraft, conditions: Sequence[Condition]
) -> tuple[tuple[PartnerRule, Condition], ...]:
    by_key = {condition.canonical_key: condition for condition in conditions}
    return tuple(
        (rule, by_key[rule.canonical_key])
        for rule in _rules_for(draft)
        if rule.canonical_key in by_key
    )


def _apply_deterministic(
    draft: NotificationDraft, *, conditions: Sequence[Condition], address_id: str
) -> NotificationDraft:
    text, cited = deterministic_detail(
        draft, pairs=_pairs_for(draft, conditions), address_id=address_id
    )
    return draft.model_copy(update={"detail": text, "cited_fact_ids": cited, "polished": False})


def _focus_bonus(conditions: Sequence[Condition], keys: Sequence[str]) -> int:
    """How far a briefing moves a call up the order. The one place the scale flips.

    A pointer's ``priority`` counts *down* from 1, and urgency here counts *up*.
    Inverting in one function rather than at each use is what keeps a briefing's
    loudest pointer from becoming this graph's quietest call.
    """
    by_key = {condition.canonical_key: condition for condition in conditions}
    weights = [
        6 - by_key[key].focus_priority
        for key in keys
        if key in by_key and by_key[key].focused and by_key[key].focus_priority
    ]
    return max(weights, default=0)


def _match_drafts(conditions: Sequence[Condition], *, lead: str) -> tuple[NotificationDraft, ...]:
    """Group the fired rules by the partner they call, most urgent first.

    One draft per partner rather than one per rule: a county duty officer told
    twice in ninety seconds about the same building stops reading the second
    message, and the two conditions belong in one call anyway.
    """
    focused = {condition.canonical_key for condition in conditions if condition.focused}
    by_key = {condition.canonical_key: condition for condition in conditions}
    grouped: dict[str, list[PartnerRule]] = {}
    for rule in PARTNER_RULES:
        if rule.canonical_key not in by_key:
            continue
        if rule.on_focus_only and rule.canonical_key not in focused:
            continue
        grouped.setdefault(rule.kind_id, []).append(rule)

    drafts: list[NotificationDraft] = []
    for kind_id, rules in grouped.items():
        keys = tuple(dict.fromkeys(rule.canonical_key for rule in rules))
        drafts.append(
            NotificationDraft(
                kind_id=kind_id,
                audience=rules[0].audience,
                condition_keys=keys,
                rule_ids=tuple(rule.rule_id for rule in rules),
                # A pointer raises a call up the order; it can never raise one
                # past the ceiling, so an absurd priority on a pointer cannot
                # push the building department ahead of a gas main.
                urgency=min(
                    9,
                    max(rule.urgency for rule in rules) + _focus_bonus(conditions, keys),
                ),
                from_focus=any(key in focused for key in keys),
            )
        )
    drafts.sort(key=lambda draft: (draft.kind_id != lead, -draft.urgency, draft.kind_id))
    return tuple(drafts)


# ---------------------------------------------------- the head agent's focus


class FocusPointerLike(Protocol):
    """One pointer on the briefing. A ref and a reason, never a value."""

    @property
    def ref(self) -> str: ...

    @property
    def priority(self) -> int: ...


class AgentFocusLike(Protocol):
    """This agent's slice of the interceptor's briefing."""

    @property
    def headline(self) -> str: ...

    @property
    def pointers(self) -> Sequence[FocusPointerLike]: ...


class IncidentReader:
    """The two things the graph reads about an incident already in progress.

    Both come off the append-only incident log, and both are allowed to fail.
    :mod:`firstdue.incident.focus` is resolved by name at call time rather than
    imported at module scope. The interceptor composes the briefing and this
    agent consumes it, so a module-level import would tie the two agents'
    import graphs together for a dependency that is optional at runtime -- and
    a build that has not got the briefing composer must degrade to the rule
    table rather than fail to import. Same reasoning as the ``TYPE_CHECKING``
    note at the top of :mod:`firstdue.incident.intake`, one step further because
    here the module's *absence* is a supported configuration and not just a
    cycle to dodge.
    """

    def __init__(self, log: IncidentLogRepository, *, incident_id: str) -> None:
        self._log = log
        self._incident_id = incident_id

    async def focus(self) -> AgentFocusLike | None:
        """This agent's slice of the briefing, or ``None`` if there is none.

        ``None`` is a first-class answer and the common one: no briefing has
        been composed, the composer is not deployed, the log read failed. All
        three mean the same thing here -- reason from the record alone.
        """
        try:
            module = importlib.import_module("firstdue.incident.focus")
        except ImportError:
            logger.info("notifier_focus_unavailable", extra={"reason": "module_absent"})
            return None
        try:
            composed = await module.read_focus(self._log, self._incident_id)
        except Exception as exc:  # pragma: no cover - a read failure, not a contract
            logger.warning("notifier_focus_read_failed", extra={"error_type": type(exc).__name__})
            return None
        if composed is None:
            return None
        agent_focus: AgentFocusLike | None = composed.for_agent(AGENT_ID)
        return agent_focus

    async def prior_notifications(self) -> dict[str, datetime]:
        """When each partner was last told, from the incident's own log.

        The log is the record of what was actually sent, which is the only
        honest source for "have we already told them": a plan this process
        remembers is a plan that dies with the process, and the partner does
        not get told twice because a pod restarted.
        """
        try:
            log = await self._log.get_log(self._incident_id)
        except Exception as exc:  # pragma: no cover - a read failure, not a contract
            logger.warning("notifier_log_read_failed", extra={"error_type": type(exc).__name__})
            return {}
        sent: dict[str, datetime] = {}
        for entry in log.entries:
            if entry.entry_type is not LogEntryType.NOTIFICATION_SENT:
                continue
            target = str(entry.content.get("target") or "")
            if target:
                sent[target] = max(sent.get(target, entry.occurred_at), entry.occurred_at)
        return sent


# --------------------------------------------------------------- the graph


class PartnerNotification:
    """The nodes of the notification pass, bound to one incident's collaborators.

    A class rather than closures for the reason
    :class:`~firstdue.agents.graphs.hazard.HazardCrossCheck` is one: the router
    has to see the same :class:`~firstdue.agents.graphs.base.BudgetGuard` the
    driver is charging, and keeping them in one object is what stops the two
    from being wired up differently in the two places that build a graph.
    """

    def __init__(
        self,
        *,
        budget: BudgetGuard,
        reader: IncidentReader | None = None,
        planner: ReasoningPlanner | None = None,
        model: ModelClient | None = None,
    ) -> None:
        self._budget = budget
        self._reader = reader
        self._planner = planner or FixedOrderPlanner()
        self._model = model

    # ---------------------------------------------------------------- nodes

    async def assess(self, state: NotifierGraphState) -> NodeResult:
        """Read what is on the record, then read what the head agent is pointing at.

        The order matters and is the whole safety argument for consuming a
        briefing at all. The conditions come from the snapshot first; a pointer
        can then *mark* one of them as focused and raise its priority, and that
        is the entire extent of its authority. A pointer naming a key this
        agent has no partner for, or a key the snapshot carries no value for, is
        counted and dropped -- never resolved into a condition, because a
        pointer carries a reference and the thing it refers to might not be
        there, and inventing it would be authoring a hazard from a citation.
        """
        conditions = conditions_on_record(state.snapshot)
        if self._reader is None:
            return NodeResult(
                decision=f"assess:{len(conditions)}",
                updates={"conditions": conditions, "done": (*state.done, NODE_ASSESS)},
                counts={"conditions": len(conditions), "pointers": 0},
            )

        focus = await self._reader.focus()
        pointers = tuple(focus.pointers) if focus is not None else ()
        priorities: dict[str, int] = {}
        unresolved = 0
        watched = {condition.canonical_key: condition for condition in conditions}
        for pointer in pointers:
            ref = str(pointer.ref)
            key = ref if ref in watched else _key_of_fact(ref, conditions)
            if key is None:
                unresolved += 1
                continue
            # Two pointers at one condition: the *loudest* wins, and loudest is
            # the lowest number on the briefing's scale.
            ranked = min(5, max(1, int(pointer.priority)))
            priorities[key] = min(priorities.get(key, ranked), ranked)
        resolved = tuple(
            condition.model_copy(
                update={
                    "focused": condition.canonical_key in priorities,
                    "focus_priority": priorities.get(condition.canonical_key, 0),
                }
            )
            for condition in conditions
        )
        return NodeResult(
            decision=f"assess:{len(resolved)}/{len(priorities)}",
            updates={
                "conditions": resolved,
                "focus_headline": (focus.headline[:200] if focus is not None else ""),
                "unresolved_pointers": unresolved,
                "done": (*state.done, NODE_ASSESS),
            },
            counts={
                "conditions": len(resolved),
                "pointers": len(pointers),
                "focused": len(priorities),
                "unresolved": unresolved,
            },
        )

    async def match(self, state: NotifierGraphState) -> NodeResult:
        """Decide which partners this record calls for, and which one is called first.

        The planner may reorder and may do nothing else: it is handed the kind
        ids and integer counts, and an answer that is not one of them is
        discarded in favour of urgency order. So the worst a confused model can
        do here is put the county before the utility -- it cannot add a partner
        the rule table did not produce, and it cannot remove one, because the
        list it is choosing from is the output rather than the input.
        """
        drafts = _match_drafts(state.conditions, lead="")
        options = tuple(draft.kind_id for draft in drafts)
        if not options:
            return NodeResult(
                decision="match:0",
                updates={"done": (*state.done, NODE_MATCH)},
                counts={"partners": 0},
            )
        chosen = await self._planner.choose(
            node=NODE_MATCH,
            options=options,
            counts={
                "partners": len(options),
                "conditions": len(state.conditions),
                "focused": sum(1 for condition in state.conditions if condition.focused),
            },
            deadline_ms=PLANNER_DEADLINE_MS,
        )
        lead = chosen if chosen in options else options[0]
        return NodeResult(
            decision=f"match:{lead}",
            updates={
                "drafts": _match_drafts(state.conditions, lead=lead),
                "lead": lead,
                "done": (*state.done, NODE_MATCH),
            },
            counts={
                "partners": len(options),
                "gated": sum(
                    1 for draft in drafts if any(rule.on_focus_only for rule in _rules_for(draft))
                ),
            },
        )

    async def suppress(self, state: NotifierGraphState) -> NodeResult:
        """Drop the partners already told, and escalate the ones who never answered.

        Two different facts about the same log entry. A partner told two minutes
        ago is *informed*, and telling them again is noise that trains them to
        skim. A partner told twenty minutes ago, with the head agent still
        pointing at the condition, has not acted on it -- and that is a repeat
        notice, marked as one, so the duty officer reading it knows this is the
        second time and not a duplicate.

        Runs before the drafting node, so a suppressed partner never costs a
        wording call out of a five-second budget.
        """
        if self._reader is None or not state.drafts:
            return NodeResult(
                decision="suppress:skipped",
                updates={"done": (*state.done, NODE_SUPPRESS)},
                counts={"suppressed": 0},
            )
        prior = await self._reader.prior_notifications()
        kept: list[NotificationDraft] = []
        suppressed: list[str] = []
        for draft in state.drafts:
            last = prior.get(draft.kind_id)
            if last is None:
                kept.append(draft)
                continue
            waited = (state.now - last).total_seconds()
            if draft.from_focus and waited >= ESCALATE_AFTER_SECONDS:
                kept.append(draft.model_copy(update={"escalation": True}))
                continue
            suppressed.append(draft.kind_id)
        return NodeResult(
            decision=f"suppress:{len(suppressed)}",
            updates={
                "drafts": tuple(kept),
                "suppressed": tuple(suppressed),
                "done": (*state.done, NODE_SUPPRESS),
            },
            counts={
                "suppressed": len(suppressed),
                "escalated": sum(1 for draft in kept if draft.escalation),
                "kept": len(kept),
            },
        )

    async def draft(self, state: NotifierGraphState) -> NodeResult:
        """Write each partner's message: the template first, the model second.

        The deterministic text is built for every draft whether or not a model
        is wired, and it is what ships unless a polish passes every check in
        :func:`reject_notification_draft`. That ordering is the guarantee: there
        is no branch here in which a partner receives nothing because the
        wording service was down, and none in which an unvalidated sentence
        reaches an agency.
        """
        written: list[NotificationDraft] = []
        polished = 0
        rejected = 0
        for draft in state.drafts:
            plain = _apply_deterministic(
                draft, conditions=state.conditions, address_id=state.snapshot.address_id
            )
            final, outcome = await self._polish(plain, address_id=state.snapshot.address_id)
            if outcome == "polished":
                polished += 1
            elif outcome != "no_model":
                rejected += 1
            written.append(final)
        return NodeResult(
            decision=f"draft:{len(written)}",
            updates={"drafts": tuple(written), "done": (*state.done, NODE_DRAFT)},
            counts={"drafts": len(written), "polished": polished, "rejected": rejected},
        )

    async def close(self, state: NotifierGraphState) -> NodeResult:
        """Nothing left to decide. The agent takes it from here."""
        return NodeResult(
            decision=f"close:{len(state.drafts)}",
            updates={"stop": GraphStop.CLOSED},
            counts={"drafts": len(state.drafts), "suppressed": len(state.suppressed)},
        )

    async def park(self, state: NotifierGraphState) -> NodeResult:
        """Stop, and say what is unfinished. The agent ships the rule table.

        Persisting is the agent's job rather than a node's, for the reason
        :meth:`~firstdue.agents.graphs.hazard.HazardCrossCheck.park` gives: a
        node has no clock and no memory bank, and it has to stay runnable in a
        process that wired neither.
        """
        stop = self._budget.exhausted() or GraphStop.UNRESOLVED
        return NodeResult(
            decision=f"park:{stop}",
            updates={"stop": stop, "waiting_on": "a partner notification decision"},
            counts={"drafts": len(state.drafts), "conditions": len(state.conditions)},
        )

    # --------------------------------------------------------------- router

    def route(self, state: NotifierGraphState) -> str:
        """Where the graph goes next. Pure, and the only place the budget bites.

        Both ceilings are checked first, so an exhausted pass parks rather than
        starting a wording call it cannot finish -- and so the bound is a
        property of the graph rather than of whichever driver is running it.
        """
        if state.stop is not None:
            return STOP
        if self._budget.exhausted() is not None:
            return NODE_PARK
        for node in (NODE_ASSESS, NODE_MATCH, NODE_SUPPRESS, NODE_DRAFT):
            if node not in state.done:
                return node
        return NODE_CLOSE

    def spec(self) -> GraphSpec[NotifierGraphState]:
        return GraphSpec(
            state_type=NotifierGraphState,
            entry=NODE_ASSESS,
            nodes={
                NODE_ASSESS: self.assess,
                NODE_MATCH: self.match,
                NODE_SUPPRESS: self.suppress,
                NODE_DRAFT: self.draft,
                NODE_CLOSE: self.close,
                NODE_PARK: self.park,
            },
            router=self.route,
        )

    # ------------------------------------------------------------ internals

    async def _polish(
        self, draft: NotificationDraft, *, address_id: str
    ) -> tuple[NotificationDraft, str]:
        """Ask the model to say the same thing better, and check that it did.

        The deterministic draft goes in as a *field*: the model is rewriting a
        message it has been handed, not answering a question about a building it
        has to reconstruct. Nothing about the incident reaches it that is not
        already in that message, and the fact ids go in as a closed list so a
        draft citing anything else is a draft citing something it was not given.
        """
        if self._model is None:
            return draft, "no_model"
        try:
            result = await self._model.compose(
                template_id=NOTIFICATION_TEMPLATE_ID,
                fields={
                    "deterministic_notification": draft.detail,
                    "audience": str(draft.audience),
                    "address_id": address_id,
                    "supporting_fact_ids": list(draft.cited_fact_ids),
                    "repeat_notice": draft.escalation,
                },
                max_chars=NOTIFICATION_DRAFT_MAX_CHARS,
                deadline_ms=NOTIFICATION_DRAFT_DEADLINE_MS,
            )
        except Exception as exc:
            # Deliberately every exception, not a curated list. A partner that
            # goes untold because the wording service timed out is the one
            # outcome this agent exists to prevent.
            logger.info(
                "notification_draft_fell_back",
                extra={
                    "kind_id": draft.kind_id,
                    "reason": "model_unavailable",
                    "error_type": type(exc).__name__,
                },
            )
            return draft, "model_unavailable"

        rejection = reject_notification_draft(result, draft=draft)
        if rejection is not None:
            logger.info(
                "notification_draft_fell_back",
                extra={"kind_id": draft.kind_id, "reason": rejection},
            )
            return draft, rejection
        return draft.model_copy(
            update={"detail": result.text.strip(), "polished": True}
        ), "polished"


def _key_of_fact(ref: str, conditions: Sequence[Condition]) -> str | None:
    """The watched key a pointer's fact id belongs to, if any.

    A pointer carries an id or a canonical key. Both are looked up against what
    the snapshot already holds; neither is ever taken as a statement that the
    thing it names is true.
    """
    for condition in conditions:
        if condition.fact_id == ref:
            return condition.canonical_key
    return None


__all__ = [
    "AGENT_ID",
    "ESCALATE_AFTER_SECONDS",
    "MAX_CLAUSES_PER_DRAFT",
    "NODE_ASSESS",
    "NODE_CLOSE",
    "NODE_DRAFT",
    "NODE_MATCH",
    "NODE_SUPPRESS",
    "NOTIFICATION_DISCLAIMER",
    "NOTIFICATION_DRAFT_MAX_CHARS",
    "NOTIFICATION_TEMPLATE_ID",
    "PARTNER_RULES",
    "WATCHED_KEYS",
    "AgentFocusLike",
    "Condition",
    "FocusPointerLike",
    "IncidentReader",
    "NotificationDraft",
    "NotificationPlan",
    "NotifierGraphState",
    "PartnerNotification",
    "PartnerRule",
    "conditions_on_record",
    "deterministic_detail",
    "deterministic_plan",
    "reject_notification_draft",
]
