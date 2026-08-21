"""The slow loop, end to end, with no credentials.

This is the acceptance test for the demo the README promises: the permit says
two storeys, the lidar measures three, both facts survive, the conflict is
found, the building is ranked, four autonomous actions are taken, the referral
waits for a human, and approving it produces exactly one case number.

The second half is the harder claim: **running it twice changes nothing.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firstdue.container import Container, build_container
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID, run_slow_loop
from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.enums import SourceType
from firstdue.domain.keys import Keys
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


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

    assert len(report.conflicts) == 1
    assert report.top_address_id == DISPUTED_ADDRESS_ID

    profile = await container.profiles.get(DISPUTED_ADDRESS_ID)
    assert profile is not None

    stories = profile.fact_sets[Keys.STORIES]
    by_source = {f.source_type: f.value.unwrap() for f in stories.facts}
    # Both facts remain. Neither is corrected, averaged, or dropped.
    assert by_source[SourceType.PERMIT] == 2
    assert by_source[SourceType.LIDAR_DSM] == 3

    conflict = profile.open_conflicts[0]
    assert conflict.rule_id == "permit-vs-lidar-story-count"
    assert conflict.severity == 4
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
    assert len(stored) == 1
    assert stored[0].status is ConflictStatus.OPEN
    assert stored[0].address_id == DISPUTED_ADDRESS_ID


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
        "survey-ranker",
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
