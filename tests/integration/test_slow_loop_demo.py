"""The slow loop, end to end, with no credentials.

This is the acceptance test for the demo the README promises: the permit says
two storeys, the lidar measures three, both facts survive, the conflict is
found, the building is ranked, four autonomous actions are taken, the referral
waits for a human, and approving it produces exactly one case number.

The second half is the harder claim: **running it twice changes nothing.**
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from firstdue.adapters.clock import SteppingClock
from firstdue.adapters.fake.runtime import FakeRuntime
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.agents.actions import ActionFlow
from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, build_agents, run_slow_loop
from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.enums import SourceType
from firstdue.domain.keys import Keys
from firstdue.extraction.extractor import FactExtractor
from firstdue.ports.audit import AuditEvent, AuditEventKind
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The demo epoch every fake-mode clock starts from.
EPOCH: Final[datetime] = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)

#: The five agents one slow-loop pass drives, in the order it drives them.
SLOW_LOOP_AGENTS: Final[frozenset[str]] = frozenset(
    {
        "records-watcher",
        "geometry-watcher",
        "hazard-watcher",
        "structure-watch",
        "referral-clerk",
    }
)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    return build_container(
        Settings(
            app_env=AppEnv.TEST,
            use_fake_agents=True,
            fixtures_dir=REPO_ROOT / "fixtures",
            demo_state_dir=tmp_path / ".demo-state",
            log_json=False,
        )
    )


async def test_the_demo_produces_the_disagreement_it_promises(container: Container) -> None:
    report = await run_slow_loop(container)

    # Two districts' worth of disagreement: the Hayes storey conflict the demo
    # is built around, and the tower's floor-count ambiguity. The assertion that
    # matters is which one a person is sent to first -- severity outranks count.
    assert len(report.conflicts) == 2
    assert report.top_address_id == DISPUTED_ADDRESS_ID

    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None

    stories = profile.fact_sets[Keys.STORIES]
    by_source = {f.source_type: f.value.unwrap() for f in stories.facts}
    # Both facts remain. Neither is corrected, averaged, or dropped.
    assert by_source[SourceType.PERMIT] == 2
    # Five, because 450 Hayes measures 16.29 m. Three storeys at that height is
    # 5.4 m ceilings -- close enough to ordinary that both records could be
    # true, which is not a disagreement. Two storeys needs 8 m ceilings.
    assert by_source[SourceType.LIDAR_DSM] == 5

    conflict = profile.open_conflicts[0]
    assert conflict.rule_id == "permit-vs-lidar-story-count"
    # Severity rises with the gap, and the gap is three storeys now.
    assert conflict.severity == 5
    assert set(conflict.fact_ids) == {
        f.fact_id
        for f in stories.facts
        if f.source_type in (SourceType.PERMIT, SourceType.LIDAR_DSM)
    } & set(conflict.fact_ids)
    assert len(conflict.fact_ids) == 2


async def test_the_ranked_row_says_why_it_is_there(container: Container) -> None:
    report = await run_slow_loop(container)
    queue = await container.queue.list_for_district(container.settings.default_district_id)

    top = queue[0]
    assert top.address_id == DISPUTED_ADDRESS_ID
    assert top.rank == 1
    assert top.reasons
    rules = {reason.rule_id for reason in top.reasons}
    assert "rank.open-conflict-severity" in rules
    # The conflict reason cites the conflict and the facts behind it.
    conflict_reason = next(r for r in top.reasons if r.rule_id == "rank.open-conflict-severity")
    assert conflict_reason.conflict_id is not None
    assert len(conflict_reason.fact_ids) == 2
    assert report.top_score > 0.5


async def test_the_four_autonomous_actions_are_taken(container: Container) -> None:
    report = await run_slow_loop(container)
    dispatch = report.dispatch
    assert dispatch is not None

    assert dispatch.work_order_ref
    assert dispatch.calendar_event_ref
    assert dispatch.notification_ref
    assert dispatch.plan_object_id

    # The pre-plan artifact really exists and is the NFPA 1620 document.
    stored = await container.plan_store.get(dispatch.plan_object_id)
    assert stored is not None
    raw = await container.plan_store.read(dispatch.plan_object_id)
    assert raw is not None
    assert b"NFPA 1620" in raw
    # It prints what nobody has established, rather than omitting it.
    assert b"unknowns" in raw
    assert b"<svg" in raw

    events = await container.calendar.list_events("e-05@sffd.example")
    assert len(events) == 1
    sent = await container.mailer.sent()
    assert len(sent) == 1
    assert DISPUTED_ADDRESS_ID in sent[0].subject
    # The crew notification states the disagreement and no tactics.
    assert "Permit records 2 storeys" in sent[0].body


async def test_the_referral_waits_for_a_human(container: Container) -> None:
    report = await run_slow_loop(container, approve=False)
    assert report.dispatch is not None
    referral_id = report.dispatch.referral_id
    assert referral_id is not None

    referral = await container.referrals.get(referral_id)
    assert referral is not None
    assert referral.status.value == "AWAITING_APPROVAL"
    assert referral.case_number is None

    approval = await container.approvals.get(f"apr_{referral_id}")
    assert approval is not None
    assert approval.status.value == "STAGED"
    assert "450" in approval.prefilled_summary or DISPUTED_ADDRESS_ID in approval.prefilled_summary


async def test_approval_produces_one_case_number_on_the_profile(container: Container) -> None:
    report = await run_slow_loop(container)
    assert report.approval is not None
    case_number = report.approval.case_number
    assert case_number

    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    assert [r.case_number for r in profile.open_referrals] == [case_number]
    assert any(
        event.type.value == "REFERRAL_FILED" and case_number in event.summary
        for event in profile.timeline
    )


@pytest.mark.idempotency
async def test_running_the_demo_twice_produces_no_duplicate_actions(
    container: Container,
) -> None:
    first = await run_slow_loop(container)
    second = await run_slow_loop(container)

    # No new facts: every observation re-derived to an id already stored.
    assert second.facts_written == 0
    assert second.facts_deduped > 0
    # No second conflict for the same disagreement.
    assert second.conflicts == ()

    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    assert len(profile.open_conflicts) == 1
    assert len(profile.fact_sets[Keys.STORIES].facts) == len(
        {f.fact_id for f in profile.fact_sets[Keys.STORIES].facts}
    )

    # One work order, one calendar hold, one notification, one case number.
    assert second.dispatch is not None and first.dispatch is not None
    assert second.dispatch.work_order_ref == first.dispatch.work_order_ref
    assert second.dispatch.calendar_event_ref == first.dispatch.calendar_event_ref
    assert second.dispatch.notification_ref == first.dispatch.notification_ref
    assert second.dispatch.replayed is True

    assert len(await container.calendar.list_events("e-05@sffd.example")) == 1
    assert len(await container.mailer.sent()) == 1

    assert second.approval is not None and first.approval is not None
    assert second.approval.case_number == first.approval.case_number
    assert second.approval.replayed is True
    assert len(profile.open_referrals) == 1


async def test_untrusted_documents_are_screened_before_the_model_sees_them(
    container: Container,
) -> None:
    """A fixture narrative carries an injection on purpose."""
    report = await run_slow_loop(container)
    assert "instruction-override" in report.screen_findings

    # The injection told the system to mark the building sprinklered. It did not.
    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    sprinklered = profile.facts.get(Keys.SUPPRESSION_SPRINKLERED)
    assert sprinklered is None or sprinklered.value.render() != "yes"


async def test_a_negated_phrase_does_not_become_an_assertion(container: Container) -> None:
    """ "No sprinkler system on file" must not extract as "sprinklered: yes".

    The narrative contains the word "sprinkler". An extractor that matches the
    word and ignores the "no" asserts the opposite of the document, on the one
    attribute a crew stakes an interior attack on.
    """
    await run_slow_loop(container)
    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None
    # The attribute stays unestablished rather than being asserted either way:
    # the sentence is about the file, not about the building.
    assert Keys.SUPPRESSION_SPRINKLERED not in profile.facts


async def test_a_structure_with_no_records_stays_a_cold_start(container: Container) -> None:
    """The address with nothing on file must not acquire invented facts."""
    await run_slow_loop(container)
    profile = await container.profiles.get("sf-3120-24th")
    assert profile is None or not profile.facts


async def test_every_fact_carries_its_provenance(container: Container) -> None:
    await run_slow_loop(container)
    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None

    for fact in profile.all_facts():
        assert fact.source_ref
        assert fact.source_snapshot_id
        assert fact.classification is not None
        if fact.extracted_by_model and fact.value.is_known:
            # A value read out of prose names the line it was read from.
            assert fact.source_span is not None


async def test_conflicts_persist_across_a_fresh_read(container: Container) -> None:
    await run_slow_loop(container)
    stored = await container.conflicts.list_open()
    # Both districts' conflicts survive the re-read: the Hayes storey
    # disagreement and the tower's floor-count ambiguity.
    assert len(stored) == 2
    # By membership rather than by position: the repository does not promise an
    # order, and a test that depended on one would fail the day a third
    # disagreement was added rather than the day something broke.
    assert all(conflict.status is ConflictStatus.OPEN for conflict in stored)
    assert DISPUTED_ADDRESS_ID in {conflict.address_id for conflict in stored}


async def test_every_slow_loop_agent_ran_through_the_runtime(container: Container) -> None:
    """The fleet is not a diagram: each agent ran, under a grant, on the record.

    ``AgentRuntime.invoke`` used to be called from nowhere in production code.
    This asserts the opposite -- that a slow-loop pass drives every catalogued
    slow-loop agent through the runtime, and that each run is durable and names
    the pinned version that produced it.
    """
    report = await run_slow_loop(container)

    ran = {run.agent_id for run in report.agent_runs}
    assert ran == {
        "records-watcher",
        "geometry-watcher",
        "hazard-watcher",
        "structure-watch",
        "referral-clerk",
    }
    assert all(run.status == "COMPLETED" for run in report.agent_runs)
    assert all(run.version for run in report.agent_runs)

    # The runtime itself saw every one of them.
    runtime = container.runtime
    invoked = {ref.split("@")[0] for ref, _ in runtime.invocations}  # type: ignore[attr-defined]
    assert ran <= invoked

    # And every run is durable, terminal, and carries its correlation id.
    for summary in report.agent_runs:
        assert summary.duration_ms >= 0.0


async def test_the_watchers_report_the_fact_ids_they_wrote(container: Container) -> None:
    """A run record that names no facts cannot be replayed against them."""
    report = await run_slow_loop(container)
    written = sum(run.facts_written for run in report.agent_runs)
    assert written > 0
    assert written <= report.facts_written


async def test_every_slow_loop_agent_leaves_a_trace_of_its_own(container: Container) -> None:
    """The console's only evidence is what an agent recorded.

    `geometry-watcher` and `hazard-watcher` did all of their work through the
    profile store and the fact log and wrote nothing to the audit log, so the
    fleet panel -- which reads that log and nothing else -- drew both as idle
    while they measured buildings and read federal registries. The test is not
    that the agents work; the rest of this file covers that. It is that working
    is *visible*.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    passes = {e.actor for e in events if e.kind is AuditEventKind.AGENT_PASS}
    assert {"geometry-watcher", "hazard-watcher"} <= passes


async def test_a_pass_is_recorded_step_by_step_not_only_at_the_end(
    container: Container,
) -> None:
    """A pass runs for minutes; a single closing line is not work in progress.

    Each step names one address and the sources that answered for it, which is
    what makes the console's terminal fill while the pass is still running
    rather than all at once when it finishes.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    steps = [e for e in events if e.kind is AuditEventKind.AGENT_STEP]

    geometry = [e for e in steps if e.actor == "geometry-watcher"]
    assert len(geometry) > 1
    # The address, not the district: a step is one building.
    assert all(e.target and e.target.startswith("sf-") for e in geometry)
    assert all("sources" in e.detail and "facts_written" in e.detail for e in geometry)

    # And the pass's own correlation id, so a step can be grouped under the pass
    # that produced it rather than floating loose in the log.
    assert len({e.correlation_id for e in geometry}) == 1


async def test_the_watcher_and_the_clerk_are_visible_too(container: Container) -> None:
    """The same idleness, for a slightly different reason.

    `records-watcher` and `referral-clerk` did write to the audit log -- but
    only when something went wrong or a human intervened: a blocked injection,
    a rejected draft, an approval somebody granted. A district that ingested
    cleanly and a captain who had not tapped yet therefore left no evidence at
    all that either agent had run, and the fleet panel reads exactly that log.
    The ordinary pass has to be on the record beside the exceptional one.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    passes = {e.actor for e in events if e.kind is AuditEventKind.AGENT_PASS}
    assert {"records-watcher", "referral-clerk"} <= passes


async def test_the_records_watcher_reports_each_building_it_ingests(
    container: Container,
) -> None:
    """Four feeds and an extraction per record is minutes, not a moment."""
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    steps = [
        e for e in events if e.kind is AuditEventKind.AGENT_STEP and e.actor == "records-watcher"
    ]
    assert len(steps) > 1
    # One building per step, and the canonical keys its filings produced.
    assert all(e.target and e.target.startswith("sf-") for e in steps)
    assert all({"facts_written", "facts_deduped", "keys"} <= set(e.detail) for e in steps)
    # Grouped under the pass that produced them.
    assert len({e.correlation_id for e in steps}) == 1


async def test_the_clerk_records_the_referral_and_the_case_number(container: Container) -> None:
    """Staging is the clerk's work; the case number is the other half of it.

    Neither was on the record before. The approval already was -- but it is the
    captain's act, recorded under the captain's name, and an agent whose only
    trace is somebody else's decision cannot be told from one that never ran.
    """
    report = await run_slow_loop(container, approve=True)
    assert report.dispatch is not None and report.approval is not None

    events = await container.audit.list_events(limit=500)
    steps = [
        e for e in events if e.kind is AuditEventKind.AGENT_STEP and e.actor == "referral-clerk"
    ]
    staged = [e for e in steps if e.detail.get("status") == "awaiting_approval"]
    filed = [e for e in steps if e.detail.get("status") == "filed"]
    assert len(staged) == 1
    assert staged[0].detail["referral_id"] == report.dispatch.referral_id

    # Which text a captain will actually read, checked against the text that
    # was stored rather than against whether a model happened to be wired: a
    # draft that was asked for and rejected ships the template, and a record
    # claiming otherwise sends a reviewer looking for prose nobody used.
    referral = await container.referrals.get(report.dispatch.referral_id or "")
    assert referral is not None
    profile = await container.profiles.get(referral.address_id)
    assert profile is not None
    conflict = next(c for c in profile.conflicts if c.conflict_id == referral.conflict_id)
    deterministic = ActionFlow._referral_narrative(profile, conflict)
    shipped_template = referral.narrative == deterministic
    assert staged[0].detail["drafted_by"] == ("template" if shipped_template else "model")

    assert len(filed) == 1
    assert filed[0].detail["case_number"] == report.approval.case_number


async def test_an_audit_record_carries_no_text_out_of_a_document(
    container: Container,
) -> None:
    """Counts, ids and canonical keys only.

    These agents read filings and registry rows. A summary line quoting one
    would put unattributed document text in the log, where it would read as the
    agent's own finding and could not be traced back to a source.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    written = [
        e for e in events if e.kind in (AuditEventKind.AGENT_PASS, AuditEventKind.AGENT_STEP)
    ]
    assert written
    for event in written:
        for key, value in event.detail.items():
            assert isinstance(value, str), key
            # Nothing prose-shaped: every value is a count, an id list, or a
            # canonical key, and none of those carry a space.
            assert " " not in value, (event.actor, key, value)


async def test_structure_watch_is_visible_too(container: Container) -> None:
    """The last agent whose work reached everything except the console.

    `structure-watch` decides which building a company is sent to next. It
    detects the district's disagreements, scores every structure against four
    weighted signals, and replaces the queue an officer works from -- and every
    one of those landed on a profile, in the conflict log, in the queue store or
    on the bus. The fleet panel reads none of them. Its only evidence is the
    audit log, where this agent's whole trace was the work order the action flow
    happens to write in its name, so a pass that ranked a district and cut no
    work order left nothing at all.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)

    passes = [e for e in events if e.kind is AuditEventKind.AGENT_PASS]
    assert "structure-watch" in {e.actor for e in passes}

    summary = next(e for e in passes if e.actor == "structure-watch")
    # The district, and a slow-loop pass belongs to no fire.
    assert summary.target == container.settings.default_district_id
    assert summary.incident_id is None
    # Both halves of the merge: what it found and what it ranked.
    assert {"profiles", "conflicts_detected", "ranked", "skipped"} <= set(summary.detail)
    assert int(summary.detail["ranked"]) > 0


async def test_structure_watch_reports_each_structure_it_ranks(container: Container) -> None:
    """One step per building put in front of a company, not one line per pass.

    Ranking is the half that reliably has something to say. By the time a
    district reaches this agent the watchers' own materialization has usually
    already recorded its disagreements, so detection re-derives ids that are
    all stored and finds nothing new -- while the ordering it produces is the
    decision the whole slow loop exists to make.
    """
    report = await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    steps = [
        e for e in events if e.kind is AuditEventKind.AGENT_STEP and e.actor == "structure-watch"
    ]
    assert len(steps) == report.queue_size > 1

    # One building per step, cited by the rules that surfaced it.
    assert all(e.target and e.target.startswith("sf-") for e in steps)
    assert all({"entry_id", "rank", "rules"} <= set(e.detail) for e in steps)
    assert {e.detail["rank"] for e in steps} == {str(n) for n in range(1, len(steps) + 1)}
    # Grouped under the pass that produced them.
    assert len({e.correlation_id for e in steps}) == 1

    top = next(e for e in steps if e.detail["rank"] == "1")
    assert top.target == report.top_address_id == DISPUTED_ADDRESS_ID


async def test_a_pass_that_runs_out_of_budget_still_says_it_ran(container: Container) -> None:
    """The record has to survive the deadline, because that is when it matters.

    A pass whose budget is gone is the pass an operator most needs to see: it
    is the difference between an agent that found nothing and an agent that
    never got to look, and the console cannot tell those apart from an absence.
    Both watchers stop themselves short of the runtime's kill and record what
    they reached, including nothing -- an early return that skipped the record
    would leave the agent looking idle in exactly the condition where knowing it
    ran is worth the most.
    """
    # A district that has already been polled once, so there is something for
    # the truncated pass to have run out of budget *on*.
    await run_slow_loop(container, approve=False)

    records, geometry, _hazards, _watch, _actions = build_agents(container)
    district = container.settings.default_district_id
    sources = list(container.source_adapters)
    # Already gone: the runtime hands an agent the deadline it will enforce, and
    # this is what an agent sees when the pass before it overran.
    spent = container.clock.now() - timedelta(seconds=1)

    watched = await records.poll(
        district_id=district, sources=sources, correlation_id="corr_spent", deadline=spent
    )
    measured = await geometry.poll(
        district_id=district, sources=sources, correlation_id="corr_spent", deadline=spent
    )
    assert watched.facts_written == 0
    assert measured.facts_written == 0

    events = await container.audit.list_events(limit=500)
    passes = {e.actor for e in events if e.kind is AuditEventKind.AGENT_PASS}
    assert {"records-watcher", "geometry-watcher"} <= passes

    truncated = next(
        e for e in events if e.kind is AuditEventKind.AGENT_PASS and e.actor == "records-watcher"
    )
    # What it did not reach, counted rather than silently dropped: a zero here
    # beside zero facts would say the district had nothing to read.
    assert int(truncated.detail["deferred"]) > 0
    assert truncated.detail["facts_written"] == "0"

    stalled = next(
        e for e in events if e.kind is AuditEventKind.AGENT_PASS and e.actor == "geometry-watcher"
    )
    # Nothing measured -- and the reason, which is that the budget went before
    # this pass had even worked out which structures were stale. Without that
    # count the line is identical to a district with no stale geometry in it.
    assert stalled.detail["measured"] == "0"
    assert int(stalled.detail["unexamined"]) > 0


async def test_a_step_is_recorded_before_the_pass_has_finished_reading(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "As the work happens" is a claim about ordering, so it is tested as one.

    `records-watcher` grouped every filing by building, extracted all of them,
    and only then committed and recorded -- so every step for a district landed
    in the same instant, after the minutes of model calls that earned them. The
    property that fixes it is not "steps exist"; it is that a building's step is
    written *before the next building is extracted*, which is the only thing
    that makes a console fill while a pass is still running.
    """
    timeline: list[str] = []

    original_extract = FactExtractor.extract

    async def watched_extract(self: FactExtractor, *args: object, **kwargs: object) -> object:
        timeline.append("extract")
        return await original_extract(self, *args, **kwargs)  # type: ignore[arg-type]

    original_record = InMemoryAuditSink.record_event

    async def watched_record(self: InMemoryAuditSink, event: AuditEvent) -> None:
        if event.kind is AuditEventKind.AGENT_STEP and event.actor == "records-watcher":
            timeline.append("step")
        await original_record(self, event)

    monkeypatch.setattr(FactExtractor, "extract", watched_extract)
    monkeypatch.setattr(InMemoryAuditSink, "record_event", watched_record)

    await run_slow_loop(container, approve=False)

    assert timeline.count("step") > 1
    # An extraction after the first step: the pass was still reading filings
    # when the console was already told about the first building.
    assert timeline.index("step") < len(timeline) - 1 - timeline[::-1].index("extract")


async def test_the_clerk_is_on_the_record_on_every_pass_not_just_the_first(
    container: Container,
) -> None:
    """The console's complaint, as a test: "referral-clerk, 0 recorded, idle".

    The first pass over a district stages a referral and says so. The second
    re-derives the same one -- a referral is derived from a conflict and a
    conflict is stable -- and used to write a single bare pass line for it, so
    an officer watching the fleet saw a clerk that had done something once and
    then stopped. Both passes have to be legible as work.
    """
    await run_slow_loop(container, approve=False)
    first = await container.audit.list_events(limit=1000)
    await run_slow_loop(container, approve=False)
    both = await container.audit.list_events(limit=1000)

    seen = {e.audit_id for e in first}
    second = [e for e in both if e.audit_id not in seen]

    def clerk(events: list[AuditEvent]) -> list[AuditEvent]:
        return [
            e
            for e in events
            if e.actor == "referral-clerk"
            and e.kind in (AuditEventKind.AGENT_PASS, AuditEventKind.AGENT_STEP)
        ]

    assert len(clerk(first)) >= 2
    # The point of the test: the second pass is not quieter than the first.
    assert len(clerk(second)) >= 2

    outcomes = [e.detail["referral"] for e in clerk(second) if e.kind is AuditEventKind.AGENT_PASS]
    assert outcomes == ["already_staged"]


async def test_a_pass_line_groups_with_the_steps_it_closes(container: Container) -> None:
    """One correlation id is one pass, for the closing line too.

    `records-watcher`, `geometry-watcher` and `hazard-watcher` each minted a
    fresh correlation for their pass record, so the line that closed a pass
    could not be grouped with the steps it closed -- and a console scoping a
    column to the pass in flight has nothing else to key on.
    """
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=1000)
    written = [
        e for e in events if e.kind in (AuditEventKind.AGENT_PASS, AuditEventKind.AGENT_STEP)
    ]
    assert written
    # One pass, one correlation, across every agent that ran in it.
    assert len({e.correlation_id for e in written}) == 1


async def test_a_second_pass_is_countable_on_its_own(container: Container) -> None:
    """What lets the console open a counter at zero and watch it climb.

    The console accumulates the audit log all session and has no endpoint that
    names the pass in flight, so it reads the pass out of the log: the newest
    `AGENT_PASS` or `AGENT_STEP` a slow-loop agent wrote names the correlation,
    and everything from that correlation's first event onward is the pass. That
    only works if the two passes do not share one.
    """
    await run_slow_loop(container, approve=False)
    first = {e.audit_id for e in await container.audit.list_events(limit=1000)}
    await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=1000)

    def pass_correlation(subset: list[AuditEvent]) -> str:
        marked = [
            e for e in subset if e.kind in (AuditEventKind.AGENT_PASS, AuditEventKind.AGENT_STEP)
        ]
        return max(marked, key=lambda e: e.occurred_at).correlation_id

    earlier = pass_correlation([e for e in events if e.audit_id in first])
    later = pass_correlation([e for e in events if e.audit_id not in first])
    assert earlier != later

    # And the window that correlation opens holds only the second pass's work.
    since = min(e.occurred_at for e in events if e.correlation_id == later)
    windowed = {e.audit_id for e in events if e.occurred_at >= since}
    assert windowed.isdisjoint(first)


# ------------------------------------------------ a pass that ran out of budget


#: A clock that burns twenty seconds a reading.
#:
#: The five slow-loop budgets are forty to three hundred seconds, so a pass on
#: this clock is past its allowance within a handful of reads and every agent
#: takes its truncation path. That is the point: the deadline arithmetic reads
#: the *injected* clock everywhere, so a coarse ``SteppingClock`` reproduces a
#: district too big for its budget without a district too big for its budget.
#:
#: Ten seconds was tuned to a 60 s `structure-watch`. That agent now has 120 s
#: -- it was landing one second inside the old cap on a live district, and an
#: overrun there silently skips `referral-clerk` -- so at ten seconds a read it
#: no longer runs out of budget and this test stopped truncating the thing it
#: exists to truncate. The step tracks the budget it is meant to exhaust.
TRUNCATING_STEP: Final[timedelta] = timedelta(seconds=20)


async def test_a_pass_truncated_by_its_deadline_still_records_every_agent(
    container: Container,
) -> None:
    """The failure this whole mechanism exists to prevent, stated as a test.

    An agent that stops inside its budget and an agent the runtime kills at its
    budget do exactly the same amount of work from the district's point of view
    -- what separates them is that only the first one gets to say so, and the
    console's only evidence is what an agent said. So a pass with almost no
    budget left must still put an ``AGENT_PASS`` in the log for all five agents,
    or the fleet panel reads "0 recorded / idle" for a fleet that just ran.

    Fake mode could not have caught this on its own. Its ``SteppingClock``
    advances fifty milliseconds a reading against budgets measured in minutes,
    so no fixture pass has ever come close to a deadline, while live mode's
    ``SystemClock`` advances by however long a real municipal endpoint took --
    which is why every one of these agents was found idle on a live console and
    busy in every test. The coarse step below is what puts the two on the same
    footing.
    """
    # One ordinary pass first, so the district has profiles, conflicts and a
    # queue: a truncated pass over an empty district would truncate nothing.
    await run_slow_loop(container, approve=False)
    seeded = await container.audit.list_events(limit=500)

    container.clock = SteppingClock(EPOCH + timedelta(hours=1), step=TRUNCATING_STEP)
    report = await run_slow_loop(container, approve=False)

    events = await container.audit.list_events(limit=1000)
    fresh = [e for e in events if e.audit_id not in {s.audit_id for s in seeded}]
    recorded = {
        e.actor
        for e in fresh
        if e.kind is AuditEventKind.AGENT_PASS and e.correlation_id == report.correlation_id
    }
    assert recorded == {
        "records-watcher",
        "geometry-watcher",
        "hazard-watcher",
        "structure-watch",
        "referral-clerk",
    }, f"a truncated pass left no trace of: {SLOW_LOOP_AGENTS - recorded}"

    # And the pass says it was truncated rather than reporting a quiet district.
    # A pass that found nothing and a pass that ran out of time before looking
    # are different statements, which is the reason these agents exist at all.
    structure = next(
        e for e in fresh if e.actor == "structure-watch" and e.kind is AuditEventKind.AGENT_PASS
    )
    assert "unpersisted" in structure.detail


async def test_a_run_the_runtime_killed_is_on_the_record_rather_than_silent(
    container: Container,
) -> None:
    """The guards are a policy; this is the net under them.

    Both runtimes enforce the deadline from outside, by cancelling the
    coroutine -- so the pass that most needs a line in the log is the one whose
    own closing line cannot execute. The fleet runner already writes a durable
    run record for it; this asserts that record also reaches the log the console
    reads, naming the terminal state rather than leaving the agent looking as
    though it never ran.
    """
    container.runtime = FakeRuntime(
        clock=container.clock,
        ids=container.ids,
        scripted_timeouts=frozenset({"hazard-watcher"}),
    )
    report = await run_slow_loop(container, approve=False)

    assert [r.status for r in report.agent_runs if r.agent_id == "hazard-watcher"] == ["TIMED_OUT"]

    events = await container.audit.list_events(limit=500)
    killed = [
        e for e in events if e.actor == "hazard-watcher" and e.kind is AuditEventKind.AGENT_PASS
    ]
    assert len(killed) == 1
    assert killed[0].detail["status"] == "TIMED_OUT"
    assert killed[0].detail["error_code"] == "UPSTREAM_TIMEOUT"
    # Under the pass's own correlation id, so it groups with the four agents
    # that did finish rather than floating loose beside them.
    assert killed[0].correlation_id == report.correlation_id


async def test_a_completed_agent_records_its_pass_exactly_once(
    container: Container,
) -> None:
    """The net must not double-count the ordinary case.

    An agent that finished has already written its own pass line, and a second
    one from the runner would put one act on the record twice under one name --
    which is a counter that climbs twice as fast as the work.
    """
    report = await run_slow_loop(container, approve=False)
    events = await container.audit.list_events(limit=500)
    for agent_id in SLOW_LOOP_AGENTS:
        passes = [
            e
            for e in events
            if e.actor == agent_id
            and e.kind is AuditEventKind.AGENT_PASS
            and e.correlation_id == report.correlation_id
        ]
        assert len(passes) == 1, f"{agent_id} recorded {len(passes)} passes for one run"


async def test_two_callers_at_once_run_one_pass(container: Container) -> None:
    """A district has one pass at a time, and the second caller joins it.

    This is the shape a live console produces without meaning to: a
    choreography timer, a second tab, a reload that abandoned the request and
    not the work behind it. Each one used to start its own pass, and five
    running at once against one district is what made ``structure-watch``
    spend its whole budget losing version checks, made the incident loop's
    own writes lose those races, and left the console's slow-loop column
    anchored on whichever pass had written most recently -- every other agent
    ``0 recorded`` through work it was doing.
    """
    first, second, third = await asyncio.gather(
        run_slow_loop(container, approve=False),
        run_slow_loop(container, approve=False),
        run_slow_loop(container, approve=False),
    )

    # One pass, reported three times. Not three passes.
    assert first.correlation_id == second.correlation_id == third.correlation_id

    events = await container.audit.list_events(limit=500)
    for agent_id in SLOW_LOOP_AGENTS:
        passes = [e for e in events if e.actor == agent_id and e.kind is AuditEventKind.AGENT_PASS]
        assert len(passes) == 1, f"{agent_id} ran {len(passes)} times for three simultaneous calls"


async def test_a_later_caller_runs_a_new_pass(container: Container) -> None:
    """Joining is for passes in flight. A finished one is not rejoined."""
    first = await run_slow_loop(container, approve=False)
    second = await run_slow_loop(container, approve=False)
    assert first.correlation_id != second.correlation_id
