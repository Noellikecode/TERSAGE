"""The runtime is the only way a catalogued agent runs.

Before ``FleetRunner`` existed, ``AgentRuntime.invoke`` was called from nowhere
in production code: the registry described a fleet, the runtime enforced grants
and deadlines, and neither was on the path that did the work. These tests are
what make that statement false and keep it false.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, SteppingClock
from firstdue.adapters.fake.runtime import FakeRuntime
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.adapters.memory.repositories import (
    InMemoryAgentRunRepository,
    InMemoryGrantRepository,
    InMemoryRegistryRepository,
)
from firstdue.agents.fleet import FleetRunner, handler_returning, outcome
from firstdue.domain.enums import AgentRunStatus, Scope
from firstdue.errors import ConfigurationError, NotFoundError
from firstdue.ports.runtime import AgentInput, AgentOutcome
from firstdue.registry.descriptors import FLEET_VERSION
from firstdue.services.grants import GrantService

EPOCH = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _runner(runtime: FakeRuntime | None = None) -> tuple[FleetRunner, FakeRuntime]:
    clock = SteppingClock(EPOCH, step=timedelta(milliseconds=50))
    ids = DeterministicIdGenerator("fleet-test")
    used = runtime or FakeRuntime(clock=clock, ids=ids)
    runner = FleetRunner(
        runtime=used,
        registry=InMemoryRegistryRepository(),
        grants=GrantService(
            grants=InMemoryGrantRepository(),
            clock=clock,
            ids=ids,
            audit=InMemoryAuditSink(),
        ),
        runs=InMemoryAgentRunRepository(),
        clock=clock,
        ids=ids,
    )
    return runner, used


async def test_an_agent_runs_through_the_runtime_not_around_it() -> None:
    runner, runtime = _runner()
    ran = False

    async def work() -> AgentOutcome:
        nonlocal ran
        ran = True
        return outcome(facts=("fact_1",))

    runner.register("records-watcher", handler_returning(work))
    result = await runner.run("records-watcher", correlation_id="corr_1")

    assert ran, "the handler never ran"
    assert result.completed
    # The runtime saw the invocation: it was not bypassed.
    assert [ref for ref, _ in runtime.invocations] == [f"records-watcher@{FLEET_VERSION}"]


async def test_the_run_lands_on_the_record_with_the_pinned_version() -> None:
    """A NIOSH investigation asks which version produced what, two years on."""
    runner, _ = _runner()
    runner.register("records-watcher", handler_returning(_noop))
    run = await runner.run("records-watcher", correlation_id="corr_1")

    assert run.record.agent_id == "records-watcher"
    assert run.record.agent_version == FLEET_VERSION
    assert run.record.status is AgentRunStatus.COMPLETED
    assert run.record.finished_at is not None
    assert run.record.correlation_id == "corr_1"


async def test_written_fact_ids_reach_the_run_record() -> None:
    runner, _ = _runner()

    async def work() -> AgentOutcome:
        return outcome(facts=("fact_a", "fact_b"))

    runner.register("records-watcher", handler_returning(work))
    run = await runner.run("records-watcher", correlation_id="corr_1")
    assert run.record.written_fact_ids == ("fact_a", "fact_b")


@pytest.mark.authorization
async def test_a_grant_without_the_required_scope_denies_before_any_work() -> None:
    """The refusal has to happen before the handler, not inside it."""
    clock = SteppingClock(EPOCH, step=timedelta(milliseconds=50))
    ids = DeterministicIdGenerator("fleet-test")
    grants = InMemoryGrantRepository()
    runtime = FakeRuntime(clock=clock, ids=ids)
    service = GrantService(grants=grants, clock=clock, ids=ids, audit=InMemoryAuditSink())
    runner = FleetRunner(
        runtime=runtime,
        registry=InMemoryRegistryRepository(),
        grants=service,
        runs=InMemoryAgentRunRepository(),
        clock=clock,
        ids=ids,
    )

    # Mint the standing grant first, deliberately missing a scope the
    # descriptor requires. The runner reuses an existing grant, so this is the
    # authority the run will actually carry.
    await service.standing_grant("records-watcher", scopes=frozenset({Scope.READ_PROFILE}))

    ran = False

    async def work() -> AgentOutcome:
        nonlocal ran
        ran = True
        return AgentOutcome()

    runner.register("records-watcher", handler_returning(work))
    run = await runner.run("records-watcher", correlation_id="corr_1")

    assert run.denied
    assert run.result.error_code == "NOT_AUTHORIZED"
    assert not ran, "work ran despite an insufficient grant"
    # A denial is still a run, and a denied run is one an audit asks about.
    assert run.record.status is AgentRunStatus.DENIED
    assert run.record.finished_at is not None


async def test_a_handler_that_raises_reaches_a_terminal_state() -> None:
    """No agent stays running forever, including one that blows up."""
    runner, _ = _runner()

    async def work() -> AgentOutcome:
        raise RuntimeError("record contents that must never be logged")

    runner.register("records-watcher", handler_returning(work))
    run = await runner.run("records-watcher", correlation_id="corr_1")

    assert run.result.status is AgentRunStatus.FAILED
    assert run.record.status is AgentRunStatus.FAILED
    assert run.record.finished_at is not None
    # The message is a stable code; a traceback can carry record contents.
    assert "record contents" not in (run.result.error_message or "")


def test_the_declared_budget_is_what_bounds_a_run() -> None:
    """The descriptor's latency target is a promise, not a decoration."""
    from firstdue.registry.descriptors import descriptor_for
    from firstdue.reliability.budget import budget_seconds

    controller = descriptor_for("incident-controller")
    assert controller.latency_target_ms == 500
    # With no caller deadline, the descriptor's own target bounds the run.
    assert budget_seconds(controller, None, EPOCH) == 0.5
    # A tighter caller deadline wins; a looser one does not widen the promise.
    assert budget_seconds(controller, EPOCH + timedelta(milliseconds=100), EPOCH) == 0.1
    assert budget_seconds(controller, EPOCH + timedelta(seconds=60), EPOCH) == 0.5


@pytest.mark.authorization
async def test_an_incident_agent_will_not_run_on_a_standing_grant() -> None:
    """Incident authority is bound to one incident and dies at its close.

    Minting a standing grant for an incident agent would turn a temporary
    authority into a permanent one -- and for the agents that reach EMS-derived
    facts, the grant model refuses to construct it at all.
    """
    runner, _ = _runner()
    runner.register("incident-controller", handler_returning(_noop))
    with pytest.raises(ConfigurationError):
        await runner.run("incident-controller", correlation_id="corr_1")


async def test_a_caller_deadline_tighter_than_the_descriptor_wins() -> None:
    runner, _ = _runner()

    async def work() -> AgentOutcome:
        await asyncio.sleep(2.0)
        return AgentOutcome()  # pragma: no cover

    runner.register("records-watcher", handler_returning(work))
    run = await runner.run(
        "records-watcher",
        correlation_id="corr_1",
        deadline=EPOCH + timedelta(milliseconds=1),
    )
    assert run.result.status is AgentRunStatus.TIMED_OUT


async def test_registering_a_second_implementation_is_refused() -> None:
    """Two implementations of one catalogued agent breaks the catalog."""
    runner, _ = _runner()
    runner.register("records-watcher", handler_returning(_noop))
    with pytest.raises(ConfigurationError):
        runner.register("records-watcher", handler_returning(_other))


async def test_registering_the_same_handler_twice_is_a_no_op() -> None:
    """A process that runs two passes registers the same handlers twice."""
    runner, _ = _runner()
    handler = handler_returning(_noop)
    runner.register("records-watcher", handler)
    runner.register("records-watcher", handler)
    run = await runner.run("records-watcher", correlation_id="corr_1")
    assert run.completed


async def test_an_unknown_agent_is_not_run() -> None:
    runner, _ = _runner()
    with pytest.raises(NotFoundError):
        await runner.run("no-such-agent", correlation_id="corr_1")


async def test_a_catalogued_agent_with_no_handler_completes_having_done_nothing() -> None:
    """Publishing a descriptor and wiring its work are separate acts."""
    runner, _ = _runner()
    run = await runner.run("conflict-detector", correlation_id="corr_1")
    assert run.completed
    assert run.record.written_fact_ids == ()


async def test_the_payload_carries_identifiers_the_handler_reads() -> None:
    """Events and inputs carry ids, never payloads -- handlers read them."""
    runner, _ = _runner()
    seen: list[AgentInput] = []

    async def handler(payload: AgentInput, _grant: object) -> AgentOutcome:
        seen.append(payload)
        return AgentOutcome()

    runner.register("records-watcher", handler)
    await runner.run(
        "records-watcher",
        correlation_id="corr_1",
        parameters={"district_id": "sffd-district-03"},
    )
    assert seen[0].parameters["district_id"] == "sffd-district-03"
    assert seen[0].correlation_id == "corr_1"


@pytest.mark.idempotency
async def test_the_same_work_derives_the_same_idempotency_key() -> None:
    """Re-dispatching one tick must be recognisable as the same work."""
    runner, _ = _runner()
    runner.register("records-watcher", handler_returning(_noop))
    first = await runner.run(
        "records-watcher", correlation_id="corr_1", parameters={"district_id": "d1"}
    )
    second = await runner.run(
        "records-watcher", correlation_id="corr_1", parameters={"district_id": "d1"}
    )
    assert first.record.idempotency_key == second.record.idempotency_key

    different = await runner.run(
        "records-watcher", correlation_id="corr_1", parameters={"district_id": "d2"}
    )
    assert different.record.idempotency_key != first.record.idempotency_key


async def _noop() -> AgentOutcome:
    return AgentOutcome()


async def _other() -> AgentOutcome:
    return AgentOutcome()
