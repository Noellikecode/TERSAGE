"""The notifier's graph, and the four things a partner notification may not be.

Everything here runs without LangGraph installed. The nodes and the router are
ordinary code in this repository and the built-in driver runs them; one test
compiles the identical node set into a real ``StateGraph`` and asserts the two
produce the same reasoning chain, and only that one skips when the package is
absent.

The file is organised around the four boundaries the module claims: a hazard
this agent cannot cite is a hazard it does not send, a notification is never an
instruction, a decision to cut a utility is a card on a chief's screen and never
a write, and a graph that runs out of time notifies the partners the rule table
has always notified rather than nobody at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.adapters.memory.repositories import (
    InMemoryApprovalRepository,
    InMemoryIncidentLogRepository,
    InMemoryWriteActionRepository,
)
from firstdue.agents.graphs.base import BudgetGuard, GraphStop, run_graph
from firstdue.agents.graphs.notifier import (
    AGENT_ID,
    ESCALATE_AFTER_SECONDS,
    NODE_ASSESS,
    NOTIFICATION_DISCLAIMER,
    NOTIFICATION_DRAFT_MAX_CHARS,
    PARTNER_RULES,
    Condition,
    IncidentReader,
    NotificationDraft,
    NotifierGraphState,
    PartnerNotification,
    conditions_on_record,
    deterministic_plan,
    reject_notification_draft,
)
from firstdue.domain.enums import (
    Classification,
    Department,
    LogEntryType,
    PolicyAction,
    Scope,
    SourceType,
)
from firstdue.domain.facts import StructuralFact
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.keys import Keys
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import (
    BooleanValue,
    QuantityValue,
    TextValue,
    UnavailableValue,
    UnknownValue,
)
from firstdue.domain.work import WriteAction, WriteReceipt
from firstdue.gateway.engine import PolicyEngine
from firstdue.incident.resources import ALL_KINDS, ResourceAgent
from firstdue.observability.tracing import TRACER
from firstdue.ports.model import ProseResult

INCIDENT = "inc-2026-0822-0141"
ADDRESS = "sf-1550-bryant"
DISTRICT = "sffd-district-03"
SNAPSHOT_ID = "snap-1550-bryant-17"
AGENCY = "sffd"
NOW = datetime(2026, 8, 22, 3, 14, tzinfo=UTC)
OBSERVED = datetime(2026, 3, 1, tzinfo=UTC)

#: The storage location off a Tier II filing. It is a confidential value on a
#: watched key's neighbour, and it must never reach a draft, a span, or a
#: checkpoint -- which is what several tests below actually check.
FILED_LOCATION = "rear ground-floor mechanical room, 2200 kg anhydrous ammonia"


# --------------------------------------------------------------- test doubles


def _fact(key: str, value: Any, *, fact_id: str) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=key,
        value=value,
        source_type=SourceType.EPA_FRS,
        source_ref="epa-frs/row-1",
        source_snapshot_id="epa:snap:1",
        observed_at=OBSERVED,
        ingested_at=OBSERVED,
        confidence=0.9,
        classification=Classification.PUBLIC,
    )


#: Deliberately fact-id shaped: the draft validator rejects a token matching
#: ``fact_[0-9a-f]{8,}`` that the draft was not given, and an id that did not
#: look like one would let the check pass by accident.
SOLAR_ID = "fact_aaaa1111bbbb2222cccc3333"
EV_ID = "fact_bbbb2222cccc3333dddd4444"
PIPE_ID = "fact_cccc3333dddd4444eeee5555"
TIER_II_ID = "fact_dddd4444eeee5555ffff6666"


def _snapshot(*facts: StructuralFact) -> ProfileSnapshot:
    return ProfileSnapshot(
        address_id=ADDRESS,
        district_id=DISTRICT,
        profile_version=17,
        snapshot_id=SNAPSHOT_ID,
        read_at=NOW,
        facts={fact.canonical_key: fact for fact in facts},
    )


def _solar() -> StructuralFact:
    return _fact(Keys.HAZARD_SOLAR_ARRAY, BooleanValue(boolean=True), fact_id=SOLAR_ID)


def _ev() -> StructuralFact:
    return _fact(Keys.HAZARD_EV_CHARGER, BooleanValue(boolean=True), fact_id=EV_ID)


def _pipeline(metres: float = 11.0) -> StructuralFact:
    return _fact(
        Keys.HAZARD_PIPELINE_PROXIMITY_M,
        QuantityValue(magnitude=metres, unit="m"),
        fact_id=PIPE_ID,
    )


def _tier_ii() -> StructuralFact:
    return _fact(Keys.HAZARD_TIER_II_PRESENT, BooleanValue(boolean=True), fact_id=TIER_II_ID)


def _grant() -> IncidentGrant:
    return IncidentGrant(
        grant_id="grant-notifier-1",
        agent_id=AGENT_ID,
        holder_department=Department.FIRE,
        scopes=frozenset(
            {
                Scope.READ_PROFILE,
                Scope.NOTIFY_AGENCY,
                Scope.REQUEST_UTILITY_SHUTOFF,
                Scope.REQUEST_ROAD_CLOSURE,
            }
        ),
        issued_at=NOW,
        incident_id=INCIDENT,
        address_id=ADDRESS,
        alarm_level=2,
        jurisdiction_id="ca-sf",
        responding_agency_id=AGENCY,
        expires_at=NOW + timedelta(hours=12),
    )


class _SpyTarget:
    """The outside world, and a record of every time it was touched.

    The point of this double is negative: several tests assert ``executed`` is
    empty for a kind the graph decided on, which is the only way to state "no
    code path" as something a test can fail on.
    """

    def __init__(self) -> None:
        self.executed: list[WriteAction] = []

    @property
    def target_id(self) -> str:
        return "agency-notifications"

    @property
    def receiving_department(self) -> Department:
        return Department.FIRE

    async def execute(self, action: WriteAction, *, body: Mapping[str, Any]) -> WriteReceipt:
        self.executed.append(action)
        return WriteReceipt(
            receipt_id=f"rcpt-{len(self.executed)}",
            action_id=action.action_id,
            target=self.target_id,
            external_ref=f"EXT-{len(self.executed)}",
            accepted_at=NOW,
        )

    async def compensate(  # pragma: no cover - not exercised
        self, receipt: WriteReceipt, *, reason: str
    ) -> WriteReceipt:
        raise NotImplementedError

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(str(action.action_id).rsplit("_", 1)[-1] for action in self.executed)


class _ScriptedModel:
    """A wording model that returns whatever a test told it to.

    Only ``compose`` is reachable from this agent: the other three verbs on the
    port are not on the notifier's path at all, and a double that implemented
    them would suggest they were.
    """

    def __init__(self, text: str | None = None, *, accepted: bool = True) -> None:
        self.text = text
        self.accepted = accepted
        self.calls: list[Mapping[str, Any]] = []

    async def compose(
        self, *, template_id: str, fields: Mapping[str, Any], max_chars: int, deadline_ms: int
    ) -> ProseResult:
        self.calls.append(fields)
        if self.text is None:
            raise TimeoutError("the wording service is down")
        return ProseResult(text=self.text, accepted=self.accepted, model_ref="scripted")


@dataclass(frozen=True)
class _Pointer:
    """The head agent's ``FocusPointer``, as this agent is entitled to see it.

    ``priority`` counts down from 1, exactly as the real one does. A double that
    got the scale the wrong way round would pass every test in this file and
    hide the one bug that matters about it.
    """

    ref: str
    reason: str = "named on the briefing"
    priority: int = 2
    kind: str = "HAZARD"


@dataclass(frozen=True)
class _AgentFocus:
    agent_id: str
    headline: str
    pointers: tuple[_Pointer, ...]


@dataclass(frozen=True)
class _IncidentFocus:
    per_agent: tuple[_AgentFocus, ...]

    def for_agent(self, agent_id: str) -> _AgentFocus | None:
        for focus in self.per_agent:
            if focus.agent_id == agent_id:
                return focus
        return None


class _FocusHolder:
    """What ``firstdue.incident.focus.read_focus`` will return this test."""

    def __init__(self) -> None:
        self.composed: _IncidentFocus | None = None
        self.reads = 0

    def points_at(self, *refs: str, priority: int = 2) -> None:
        self.composed = _IncidentFocus(
            per_agent=(
                _AgentFocus(
                    agent_id=AGENT_ID,
                    headline="Roof array and a pipeline in the collapse zone.",
                    pointers=tuple(_Pointer(ref=ref, priority=priority) for ref in refs),
                ),
            )
        )


# -------------------------------------------------------------------fixtures


@pytest.fixture
def focus(monkeypatch: pytest.MonkeyPatch) -> _FocusHolder:
    """Stand in for the interceptor's briefing module.

    ``firstdue.incident.focus`` is written by the head agent and consumed here,
    and this agent resolves it by name at call time precisely so that it can be
    absent. Installing a module object rather than a file keeps the two agents'
    tests independent while still exercising the real lookup: the notifier calls
    ``importlib.import_module`` and gets this.
    """
    holder = _FocusHolder()
    module = ModuleType("firstdue.incident.focus")

    async def read_focus(log: Any, incident_id: str) -> _IncidentFocus | None:
        holder.reads += 1
        assert incident_id == INCIDENT
        return holder.composed

    module.read_focus = read_focus  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "firstdue.incident.focus", module)
    return holder


@pytest.fixture
def no_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build where the briefing composer does not exist at all."""
    monkeypatch.delitem(sys.modules, "firstdue.incident.focus", raising=False)


@pytest.fixture
def log() -> InMemoryIncidentLogRepository:
    return InMemoryIncidentLogRepository()


@pytest.fixture
def target() -> _SpyTarget:
    return _SpyTarget()


@pytest.fixture
def make_agent(target: _SpyTarget):
    def _make(**overrides: Any) -> ResourceAgent:
        ids = DeterministicIdGenerator("notifier")
        return ResourceAgent(
            policy=PolicyEngine(ids=ids),
            approvals=InMemoryApprovalRepository(),
            write_actions=InMemoryWriteActionRepository(),
            target=target,
            audit=InMemoryAuditSink(),
            clock=FixedClock(NOW),
            ids=ids,
            use_langgraph=False,
            **overrides,
        )

    return _make


@pytest.fixture
def recording_spans() -> Iterator[None]:
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


async def _notified(log: InMemoryIncidentLogRepository, kind_id: str, *, at: datetime) -> None:
    """Put a notification this incident already sent into its own log."""
    sequence = await log.next_sequence(INCIDENT)
    await log.append(
        IncidentLogEntry(
            entry_id=f"log-{kind_id}-{sequence}",
            incident_id=INCIDENT,
            sequence=sequence,
            entry_type=LogEntryType.NOTIFICATION_SENT,
            occurred_at=at,
            profile_snapshot_id=SNAPSHOT_ID,
            content={"target": kind_id, "external_ref": "EXT-0", "autonomous": True},
        )
    )


def _kinds(drafts: tuple[NotificationDraft, ...]) -> set[str]:
    return {draft.kind_id for draft in drafts}


# ------------------------------------------------------- the table it stands on


@pytest.mark.invariant
def test_every_rule_names_a_partner_this_agent_can_actually_request() -> None:
    """A rule naming a kind that does not exist is a partner nobody calls.

    The rule table and the request catalog are in two files on purpose -- the
    graph must not import the agent that runs it -- so this is the seam where
    they are held together.
    """
    for rule in PARTNER_RULES:
        kind = ALL_KINDS.get(rule.kind_id)
        assert kind is not None, rule.rule_id
        assert kind.receiving_department is rule.audience, rule.rule_id


@pytest.mark.invariant
def test_every_gated_partner_is_reached_only_through_the_briefing() -> None:
    """The record tells a partner what is there; only a focus asks for a resource.

    Stated as a property of the table rather than of any one run: a commitment
    rule that fired on the record alone would let a snapshot on its own put a
    shutoff card on a chief's screen.
    """
    for rule in PARTNER_RULES:
        commits = not ALL_KINDS[rule.kind_id].is_notification
        assert commits == rule.on_focus_only, rule.rule_id


@pytest.mark.invariant
def test_nothing_this_agent_can_say_is_tactical() -> None:
    """Every fixed sentence in the module, checked against its own ban list.

    The validator refuses tactical vocabulary in a model's draft. If the
    deterministic text contained any, the rule would be unenforceable -- every
    polish would be rejected and nobody would notice why.
    """
    snapshot = _snapshot(_solar(), _ev(), _pipeline(), _tier_ii())
    for draft in deterministic_plan(snapshot).drafts:
        rejection = reject_notification_draft(
            ProseResult(text=draft.detail, model_ref="identity"), draft=draft
        )
        assert rejection is None, (draft.kind_id, rejection)


# ------------------------------------------------------------ what is on file


def test_a_filing_that_says_no_is_not_a_condition() -> None:
    """``solar_array = false`` is the record saying there is no array."""
    snapshot = _snapshot(
        _fact(Keys.HAZARD_SOLAR_ARRAY, BooleanValue(boolean=False), fact_id=SOLAR_ID)
    )
    assert conditions_on_record(snapshot) == ()


@pytest.mark.degraded
def test_absence_is_not_a_condition() -> None:
    """An unreachable registry produced no hazard, and no notification either."""
    snapshot = _snapshot(
        _fact(
            Keys.HAZARD_TIER_II_PRESENT,
            UnavailableValue(source_id="tier-ii", reason="the county filing system is down"),
            fact_id="f_1",
        ),
        _fact(Keys.HAZARD_SOLAR_ARRAY, UnknownValue(), fact_id="f_2"),
    )
    assert conditions_on_record(snapshot) == ()


def test_a_measured_condition_carries_the_records_own_rendering() -> None:
    conditions = conditions_on_record(_snapshot(_pipeline(11.0)))
    assert [condition.fact_id for condition in conditions] == [PIPE_ID]
    assert "11" in conditions[0].rendered


def test_a_free_text_value_never_becomes_a_condition_value() -> None:
    """A Tier II storage location does not travel to a mutual-aid pager.

    The presence flag is the condition; the location is a separate confidential
    key, and no draft is built from a rendered string.
    """
    snapshot = _snapshot(
        _tier_ii(),
        _fact(Keys.HAZARD_TIER_II_LOCATION, TextValue(text=FILED_LOCATION), fact_id="f_loc"),
    )
    conditions = conditions_on_record(snapshot)
    assert [condition.canonical_key for condition in conditions] == [Keys.HAZARD_TIER_II_PRESENT]
    assert conditions[0].rendered == ""
    for draft in deterministic_plan(snapshot).drafts:
        assert "ammonia" not in draft.detail
        assert "mechanical room" not in draft.detail


# ------------------------------------------------------ the rule table alone


def test_the_record_alone_calls_the_partners_it_has_always_called() -> None:
    plan = deterministic_plan(_snapshot(_solar(), _ev()))

    assert _kinds(plan.drafts) == {"utility-conditions", "mutual-aid"}
    assert plan.deterministic is True
    assert all(draft.detail for draft in plan.drafts)


def test_each_audience_is_addressed_in_its_own_terms() -> None:
    """The same incident, said three ways, because three people read it."""
    plan = deterministic_plan(_snapshot(_solar(), _ev(), _pipeline()))
    by_kind = {draft.kind_id: draft for draft in plan.drafts}

    assert by_kind["utility-conditions"].audience is Department.UTILITY
    assert by_kind["mutual-aid"].audience is Department.FIRE
    assert by_kind["county-oem"].audience is Department.COUNTY_OEM
    openers = {draft.detail.split(".")[0] for draft in plan.drafts}
    assert len(openers) == len(plan.drafts)


def test_every_draft_cites_the_records_it_quotes_and_fits_on_a_card() -> None:
    plan = deterministic_plan(_snapshot(_solar(), _ev(), _pipeline(), _tier_ii()))

    for draft in plan.drafts:
        assert draft.cited_fact_ids
        assert all(fact_id in draft.detail for fact_id in draft.cited_fact_ids)
        assert NOTIFICATION_DISCLAIMER in draft.detail
        assert len(draft.detail) <= NOTIFICATION_DRAFT_MAX_CHARS


def test_a_record_with_nothing_on_it_calls_nobody() -> None:
    assert deterministic_plan(_snapshot()).drafts == ()


# ------------------------------------------------------- the head agent's focus


async def test_a_focus_pointer_changes_who_is_notified(focus, log, make_agent) -> None:
    """The record says there is an array; the briefing is why a chief is asked.

    Same snapshot, twice. Without a pointer the utility is *told*. With one, the
    same fact also stages a shutoff request -- and the difference is the head
    agent's judgement about this fire, not a new belief about the building.
    """
    agent = make_agent(log=log)
    snapshot = _snapshot(_solar())

    unpointed = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)
    assert _kinds(unpointed.plan.drafts) == {"utility-conditions"}

    focus.points_at(Keys.HAZARD_SOLAR_ARRAY)
    pointed = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert _kinds(pointed.plan.drafts) == {"utility-conditions", "electric-shutoff"}
    staged = next(d for d in pointed.plan.drafts if d.kind_id == "electric-shutoff")
    assert staged.from_focus is True
    assert SOLAR_ID in staged.detail


async def test_a_pointer_may_name_a_fact_id_as_well_as_a_key(focus, log, make_agent) -> None:
    """A pointer carries an id or a canonical key. Both are looked up here."""
    focus.points_at(PIPE_ID)
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_pipeline()))

    assert _kinds(run.plan.drafts) == {"county-oem", "gas-shutoff"}


async def test_a_pointer_at_something_the_record_does_not_carry_is_dropped(
    focus, log, make_agent
) -> None:
    """The pointer names a reference; it does not assert the thing is there.

    A pointer at a key this snapshot has no fact for is counted and ignored. It
    can never manufacture a condition, because a condition is read off the
    record and a pointer only ever marks one that is already there.
    """
    focus.points_at(Keys.HAZARD_TIER_II_PRESENT, "fact_ffff9999ffff9999ffff9999")
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_solar()))

    assert run.plan.unresolved_pointers == 2
    assert _kinds(run.plan.drafts) == {"utility-conditions"}


@pytest.mark.degraded
async def test_a_missing_focus_degrades_to_todays_rule_table(no_focus, log, make_agent) -> None:
    """No briefing composer in this build. The partners still get told."""
    agent = make_agent(log=log)
    snapshot = _snapshot(_solar(), _pipeline())

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert run.plan.stop is GraphStop.CLOSED
    assert _kinds(run.plan.drafts) == _kinds(deterministic_plan(snapshot).drafts)


@pytest.mark.degraded
async def test_a_briefing_that_says_nothing_about_this_agent_changes_nothing(
    focus, log, make_agent
) -> None:
    agent = make_agent(log=log)
    snapshot = _snapshot(_solar())

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert focus.reads == 1
    assert _kinds(run.plan.drafts) == _kinds(deterministic_plan(snapshot).drafts)


async def test_the_notifier_reads_a_briefing_the_head_agent_actually_composed(
    log, make_agent
) -> None:
    """The contract, against the real module rather than a double.

    Every other focus test here installs a stand-in, which keeps this agent's
    tests independent of the interceptor's. This one does not: it composes a
    focus with the sanctioned constructor, writes the real log entry, and reads
    it back through the real ``read_focus``. If the two agents ever disagree
    about what a pointer is, this is where it shows.
    """
    from firstdue.incident.focus import (
        AgentFocus,
        FocusKind,
        FocusPointer,
        compose_focus,
        focus_log_entry,
        focus_scope,
    )

    snapshot = _snapshot(_solar())
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=focus_scope(snapshot),
        per_agent=(
            AgentFocus(
                agent_id=AGENT_ID,
                headline="Roof array on file for the fire address.",
                pointers=(
                    FocusPointer(
                        kind=FocusKind.HAZARD,
                        ref=Keys.HAZARD_SOLAR_ARRAY,
                        reason=f"filed under {SOLAR_ID}",
                        priority=1,
                    ),
                ),
            ),
        ),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    await log.append(
        focus_log_entry(
            composed,
            sequence=await log.next_sequence(INCIDENT),
            now=NOW,
            profile_snapshot_id=SNAPSHOT_ID,
        )
    )
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert _kinds(run.plan.drafts) == {"utility-conditions", "electric-shutoff"}
    assert run.plan.unresolved_pointers == 0


async def test_the_loudest_pointer_is_the_call_that_goes_first(focus, log, make_agent) -> None:
    """A briefing's priority counts down from 1; this graph's urgency counts up.

    The inversion happens in one function, and this is the test that would fail
    if it stopped happening: the condition the head agent shouted about leads
    the plan, ahead of the one it merely mentioned.
    """
    agent = make_agent(log=log)
    focus.points_at(Keys.HAZARD_EV_CHARGER, priority=1)
    loud = await agent.plan_notifications(
        incident_id=INCIDENT, snapshot=_snapshot(_ev(), _pipeline())
    )

    focus.points_at(Keys.HAZARD_EV_CHARGER, priority=5)
    quiet = await agent.plan_notifications(
        incident_id=INCIDENT, snapshot=_snapshot(_ev(), _pipeline())
    )

    assert loud.plan.drafts[0].kind_id == "mutual-aid"
    assert quiet.plan.drafts[0].kind_id == "county-oem"


async def test_no_log_wired_is_the_rule_table_and_nothing_else(make_agent) -> None:
    """The default configuration, and the one 1204 tests run in."""
    agent = make_agent()
    snapshot = _snapshot(_solar(), _ev())

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert agent.reasons is False
    assert run.graph_steps == 0
    assert run.plan.deterministic is True
    assert _kinds(run.plan.drafts) == _kinds(deterministic_plan(snapshot).drafts)


# --------------------------------------------------------- what is already known


async def test_a_partner_already_told_is_not_told_again(focus, log, make_agent) -> None:
    await _notified(log, "mutual-aid", at=NOW - timedelta(minutes=2))
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_ev(), _solar()))

    assert run.plan.suppressed == ("mutual-aid",)
    assert _kinds(run.plan.drafts) == {"utility-conditions"}


async def test_a_partner_who_never_answered_is_told_again_and_told_so(
    focus, log, make_agent
) -> None:
    """Told and ignored is not the same as told, and reads differently."""
    await _notified(log, "county-oem", at=NOW - timedelta(seconds=ESCALATE_AFTER_SECONDS + 60))
    focus.points_at(Keys.HAZARD_TIER_II_PRESENT)
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_tier_ii()))

    repeat = next(draft for draft in run.plan.drafts if draft.kind_id == "county-oem")
    assert repeat.escalation is True
    assert "Repeat notice" in repeat.detail
    assert run.plan.suppressed == ()


async def test_a_partner_told_two_minutes_ago_is_not_escalated_at(focus, log, make_agent) -> None:
    """Ten minutes of silence is a repeat; two minutes is noise."""
    await _notified(log, "county-oem", at=NOW - timedelta(minutes=2))
    focus.points_at(Keys.HAZARD_TIER_II_PRESENT)
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_tier_ii()))

    assert run.plan.suppressed == ("county-oem",)
    assert _kinds(run.plan.drafts) == {"hazmat-team"}


# ------------------------------------------------------------------- the gate


@pytest.mark.authorization
async def test_a_decided_shutoff_is_staged_and_writes_nothing(
    focus, log, make_agent, target
) -> None:
    """The whole point of the agent, stated as something that can fail.

    The graph decided a shutoff was warranted, drafted it, and staged it. No
    gas main moved, because ``notify`` reaches ``request`` with no approval on
    it and the gateway answers ``REQUIRE_APPROVAL``. What a chief sees is a
    card; what the utility's write target saw is nothing.
    """
    focus.points_at(Keys.HAZARD_SOLAR_ARRAY, Keys.HAZARD_PIPELINE_PROXIMITY_M)
    agent = make_agent(log=log)

    run = await agent.notify(
        grant=_grant(), incident_id=INCIDENT, snapshot=_snapshot(_solar(), _pipeline())
    )

    assert set(run.awaiting_chief) == {"electric-shutoff", "gas-shutoff"}
    assert set(run.sent) == {"utility-conditions", "county-oem"}
    assert "electric-shutoff" not in target.kinds
    assert "gas-shutoff" not in target.kinds
    for outcome in run.outcomes:
        if outcome.kind_id.endswith("shutoff"):
            assert outcome.action is PolicyAction.REQUIRE_APPROVAL
            assert outcome.external_ref is None
            assert outcome.approval_id is not None


@pytest.mark.authorization
async def test_planning_alone_touches_nothing_at_all(focus, log, make_agent, target) -> None:
    """A plan is inert. Deciding and acting are two calls, and this is the first."""
    focus.points_at(Keys.HAZARD_PIPELINE_PROXIMITY_M)
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_pipeline()))

    assert "gas-shutoff" in _kinds(run.plan.drafts)
    assert target.executed == []


@pytest.mark.authorization
def test_the_agent_has_no_way_to_approve_its_own_request() -> None:
    """Structural, not behavioural: the parameter does not exist.

    ``request`` takes an ``approval_id`` because a chief's tap has to be able to
    carry one. ``notify`` does not, so there is no argument a caller, a graph, or
    a future edit inside this agent could pass to make a commitment execute.
    """
    import inspect

    assert "approval_id" in inspect.signature(ResourceAgent.request).parameters
    assert "approval_id" not in inspect.signature(ResourceAgent.notify).parameters
    assert "approval_id" not in inspect.signature(ResourceAgent.plan_notifications).parameters


@pytest.mark.authorization
async def test_staging_twice_stages_once(focus, log, make_agent, target) -> None:
    """A second pass over the same incident must not open a second card."""
    focus.points_at(Keys.HAZARD_SOLAR_ARRAY)
    agent = make_agent(log=log)
    snapshot = _snapshot(_solar())

    first = await agent.notify(grant=_grant(), incident_id=INCIDENT, snapshot=snapshot)
    second = await agent.notify(grant=_grant(), incident_id=INCIDENT, snapshot=snapshot)

    assert first.awaiting_chief == second.awaiting_chief == ("electric-shutoff",)
    assert "electric-shutoff" not in target.kinds


# ---------------------------------------------------------------- the budget


def _budget(seconds: float = 5.0, *, max_steps: int = 24) -> BudgetGuard:
    ticks = iter(range(0, 10_000))
    return BudgetGuard(seconds=seconds, max_steps=max_steps, monotonic=lambda: float(next(ticks)))


@pytest.mark.degraded
async def test_a_graph_out_of_time_parks_before_it_starts_a_node() -> None:
    """The ceiling is checked in the router, so it holds under either driver."""
    notification = PartnerNotification(budget=_budget(seconds=0.5))
    state = NotifierGraphState(
        district_id=DISTRICT, incident_id=INCIDENT, snapshot=_snapshot(_solar()), now=NOW
    )

    run = await run_graph(
        notification.spec(),
        state,
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=notification._budget,
        request_digest="digest-1",
        use_langgraph=False,
    )

    assert run.trace.stop is GraphStop.OUT_OF_TIME
    assert run.trace.node_sequence[-1] == "park"


@pytest.mark.degraded
async def test_budget_exhaustion_falls_back_to_the_deterministic_table(
    focus, log, make_agent
) -> None:
    """A partner notified late by the table beats a partner not notified.

    The step bound stops this pass before it has decided anything, so what
    ships is exactly what shipped before the graph existed -- including *not*
    shipping the shutoff the focus would have staged, because nothing decided
    it.
    """
    focus.points_at(Keys.HAZARD_SOLAR_ARRAY)
    agent = make_agent(log=log, max_graph_steps=1)
    snapshot = _snapshot(_solar(), _ev())

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=snapshot)

    assert run.graph_stop == str(GraphStop.OUT_OF_STEPS)
    assert run.plan.deterministic is True
    assert _kinds(run.plan.drafts) == _kinds(deterministic_plan(snapshot).drafts)
    assert "electric-shutoff" not in _kinds(run.plan.drafts)


@pytest.mark.degraded
async def test_an_exhausted_pass_still_notifies_every_partner_the_table_names(
    focus, log, make_agent, target
) -> None:
    agent = make_agent(log=log, max_graph_steps=1)

    run = await agent.notify(
        grant=_grant(), incident_id=INCIDENT, snapshot=_snapshot(_solar(), _ev())
    )

    assert set(run.sent) == {"utility-conditions", "mutual-aid"}


# ------------------------------------------------------------ the model's part


def _polished(draft: NotificationDraft, text: str) -> str | None:
    return reject_notification_draft(ProseResult(text=text, model_ref="scripted"), draft=draft)


def _solar_draft() -> NotificationDraft:
    plan = deterministic_plan(_snapshot(_solar()))
    return plan.drafts[0]


@pytest.mark.invariant
def test_a_draft_that_names_an_unsourced_hazard_is_rejected() -> None:
    """The failure a wording model actually has, and the check that catches it.

    "Utility" and "gas main" travel together in a model's training data. This
    incident's record carries a photovoltaic array and no pipeline, so a draft
    that mentions one has authored a hazard -- which is the one thing the model
    may never do, whatever else it improved about the sentence.
    """
    draft = _solar_draft()
    invented = (
        f"SFFD, incident at {ADDRESS}. A photovoltaic array and a gas main are on file. "
        f"Records: {SOLAR_ID}. {NOTIFICATION_DISCLAIMER}"
    )

    assert _polished(draft, invented) == "unsourced_hazard"


@pytest.mark.invariant
def test_a_draft_that_tells_anyone_what_to_do_is_rejected() -> None:
    """This project does not recommend tactics, and does not let a partner either."""
    draft = _solar_draft()
    tactical = (
        f"SFFD, incident at {ADDRESS}. A photovoltaic array is on file; crews should "
        f"evacuate the block. Records: {SOLAR_ID}. {NOTIFICATION_DISCLAIMER}"
    )

    assert _polished(draft, tactical) == "tactical_language"


@pytest.mark.invariant
def test_a_draft_that_drops_the_evidence_or_invents_some_is_rejected() -> None:
    draft = _solar_draft()
    dropped = f"SFFD, incident at {ADDRESS}. An array is on file. {NOTIFICATION_DISCLAIMER}"
    invented = (
        f"SFFD, incident at {ADDRESS}. An array is on file. "
        f"Records: {SOLAR_ID}, fact_9999999999999999abcdabcd. {NOTIFICATION_DISCLAIMER}"
    )

    assert _polished(draft, dropped) == "fact_id_dropped"
    assert _polished(draft, invented) == "fact_id_introduced"


@pytest.mark.invariant
def test_a_draft_that_stops_saying_it_is_not_an_instruction_is_rejected() -> None:
    draft = _solar_draft()
    text = f"SFFD, incident at {ADDRESS}. An array is on file. Records: {SOLAR_ID}."

    assert _polished(draft, text) == "disclaimer_dropped"


@pytest.mark.invariant
def test_a_rejected_model_output_is_rejected() -> None:
    draft = _solar_draft()
    result = ProseResult(text=draft.detail, accepted=False, model_ref="scripted")

    assert reject_notification_draft(result, draft=draft) == "not_accepted"


async def test_a_rejected_draft_ships_the_deterministic_text(focus, log, make_agent) -> None:
    """A partner gets a message that is worse-written and exactly as true."""
    model = _ScriptedModel(
        f"SFFD, incident at {ADDRESS}. A pipeline is on file. "
        f"Records: {SOLAR_ID}. {NOTIFICATION_DISCLAIMER}"
    )
    agent = make_agent(log=log, model=model)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_solar()))

    draft = run.plan.drafts[0]
    assert draft.polished is False
    assert draft.detail == deterministic_plan(_snapshot(_solar())).drafts[0].detail
    assert model.calls


@pytest.mark.degraded
async def test_a_wording_service_that_is_down_costs_nothing(focus, log, make_agent) -> None:
    """A partner untold because a model timed out is the outcome to prevent."""
    agent = make_agent(log=log, model=_ScriptedModel(None))

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_solar()))

    assert run.plan.drafts[0].detail == deterministic_plan(_snapshot(_solar())).drafts[0].detail


async def test_a_validated_polish_is_what_ships(focus, log, make_agent) -> None:
    """Rewriting the sentence is allowed; that is the whole job."""
    better = (
        f"San Francisco Fire is working an incident at {ADDRESS}. A photovoltaic array "
        f"is on file for the address, and its DC side stays live after the service is "
        f"cut. Records: {SOLAR_ID}. {NOTIFICATION_DISCLAIMER}"
    )
    agent = make_agent(log=log, model=_ScriptedModel(better))

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_solar()))

    assert run.plan.drafts[0].polished is True
    assert run.plan.drafts[0].detail == better


async def test_the_model_is_handed_a_message_and_not_a_building(focus, log, make_agent) -> None:
    """Nothing reaches the prompt that is not already in the deterministic draft."""
    model = _ScriptedModel(None)
    agent = make_agent(log=log, model=model)

    await agent.plan_notifications(
        incident_id=INCIDENT,
        snapshot=_snapshot(
            _solar(),
            _fact(Keys.HAZARD_TIER_II_LOCATION, TextValue(text=FILED_LOCATION), fact_id="f_loc"),
        ),
    )

    rendered = " ".join(str(value) for fields in model.calls for value in fields.values())
    assert "ammonia" not in rendered
    assert "mechanical room" not in rendered


# ------------------------------------------------------------------ the trace


async def test_every_node_leaves_a_span_that_says_what_it_decided(
    recording_spans, focus, log, make_agent
) -> None:
    agent = make_agent(log=log)

    run = await agent.plan_notifications(incident_id=INCIDENT, snapshot=_snapshot(_solar()))

    spans = [span for span in TRACER.spans if span.name == f"agent.{AGENT_ID}"]
    assert len(spans) == run.graph_steps
    assert all("graph.decision" in span.attributes for span in spans)
    assert {str(span.attributes["graph_node"]) for span in spans} >= {"assess", "match", "draft"}


async def test_no_span_carries_a_word_of_a_notification(
    recording_spans, focus, log, make_agent
) -> None:
    """A span that never held a message cannot leak one -- asserted, not hoped."""
    focus.points_at(Keys.HAZARD_SOLAR_ARRAY)
    agent = make_agent(log=log)

    await agent.notify(
        grant=_grant(),
        incident_id=INCIDENT,
        snapshot=_snapshot(
            _solar(),
            _fact(Keys.HAZARD_TIER_II_LOCATION, TextValue(text=FILED_LOCATION), fact_id="f_loc"),
        ),
    )

    rendered = " ".join(str(value) for span in TRACER.spans for value in span.attributes.values())
    for word in ("photovoltaic", "ammonia", "mechanical room", NOTIFICATION_DISCLAIMER):
        assert word not in rendered


def test_a_checkpoint_carries_kinds_and_not_drafts() -> None:
    """A checkpoint outlives the incident, so it holds ids and counts only."""
    state = NotifierGraphState(
        district_id=DISTRICT,
        incident_id=INCIDENT,
        snapshot=_snapshot(_solar()),
        now=NOW,
        conditions=(Condition(canonical_key=Keys.HAZARD_SOLAR_ARRAY, fact_id=SOLAR_ID),),
        drafts=deterministic_plan(_snapshot(_solar())).drafts,
        waiting_on="a partner notification decision",
    )

    payload = state.checkpoint_payload()

    assert payload["planned_kind_ids"] == ["utility-conditions"]
    assert payload["condition_keys"] == [Keys.HAZARD_SOLAR_ARRAY]
    rendered = " ".join(str(value) for value in payload.values())
    assert "photovoltaic" not in rendered
    assert NOTIFICATION_DISCLAIMER not in rendered


# ---------------------------------------------------------------- fake mode


def test_fake_mode_never_imports_langgraph() -> None:
    """The notifier and the agent that runs it, imported, with the package alone.

    A subprocess rather than an assertion, because another test in this session
    imports LangGraph deliberately. What has to hold is that importing the
    incident loop does not: fake mode is the credential-free, dependency-free
    path, and an import at module scope here would quietly make the extra
    mandatory for every incident.
    """
    probe = (
        "import sys, firstdue.incident.resources, firstdue.agents.graphs.notifier;"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] "
        "in {'langgraph', 'langchain_core', 'langchain_google_vertexai'});"
        "print(leaked)"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "USE_FAKE_AGENTS": "true"},
    )

    assert completed.stdout.strip() == "[]"


# ------------------------------------------------------------- with LangGraph


async def test_langgraph_runs_the_same_nodes_to_the_same_chain(focus, log) -> None:
    """The framework is an executor. Swapping it in must change nothing."""
    pytest.importorskip("langgraph")
    focus.points_at(Keys.HAZARD_SOLAR_ARRAY)

    def build() -> PartnerNotification:
        return PartnerNotification(
            budget=_budget(), reader=IncidentReader(log, incident_id=INCIDENT)
        )

    def state() -> NotifierGraphState:
        return NotifierGraphState(
            district_id=DISTRICT, incident_id=INCIDENT, snapshot=_snapshot(_solar()), now=NOW
        )

    builtin = build()
    compiled = build()
    first = await run_graph(
        builtin.spec(),
        state(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=builtin._budget,
        request_digest="digest-parity",
        use_langgraph=False,
    )
    second = await run_graph(
        compiled.spec(),
        state(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=compiled._budget,
        request_digest="digest-parity",
        use_langgraph=True,
    )

    assert first.trace.decisions == second.trace.decisions
    assert first.state.drafts == second.state.drafts
    assert first.trace.node_sequence[0] == NODE_ASSESS
