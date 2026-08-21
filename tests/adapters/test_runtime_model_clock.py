"""FakeRuntime enforces authorization; the fake model does real work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import (
    DeterministicIdGenerator,
    FixedClock,
    SteppingClock,
    SystemClock,
)
from firstdue.adapters.fake.model import FakeModelClient
from firstdue.adapters.fake.runtime import FakeRuntime
from firstdue.domain.enums import (
    Capability,
    Classification,
    Department,
    Loop,
    Scope,
)
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.keys import Keys
from firstdue.domain.registry import AgentDescriptor
from firstdue.errors import UpstreamTimeoutError
from firstdue.ports.runtime import AgentInput, AgentRunStatus

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _descriptor(**overrides) -> AgentDescriptor:
    payload = {
        "agent_id": "reconciler",
        "version": "1.0.0",
        "publisher_department": Department.FIRE,
        "loop": Loop.INCIDENT,
        "role_summary": "Streams the tactical brief",
        "capabilities": frozenset({Capability.READ}),
        "required_scopes": frozenset({Scope.READ_PROFILE}),
        "classifications_accessed": frozenset({Classification.PUBLIC}),
        "input_schema_ref": "in.json",
        "output_schema_ref": "out.json",
        "latency_target_ms": 500,
        "published_at": NOW,
    }
    payload.update(overrides)
    return AgentDescriptor(**payload)  # type: ignore[arg-type]


def _grant(scopes: frozenset[Scope], **overrides) -> IncidentGrant:
    payload = {
        "grant_id": "ig-1",
        "agent_id": "reconciler",
        "holder_department": Department.FIRE,
        "scopes": scopes,
        "issued_at": NOW,
        "incident_id": "inc-1",
        "address_id": "sf-0450-hayes",
        "alarm_level": 2,
        "jurisdiction_id": "sf-city-county",
        "responding_agency_id": "sffd",
        "expires_at": NOW + timedelta(hours=6),
    }
    payload.update(overrides)
    return IncidentGrant(**payload)  # type: ignore[arg-type]


def _runtime(**overrides) -> FakeRuntime:
    return FakeRuntime(clock=SteppingClock(NOW), ids=DeterministicIdGenerator("test"), **overrides)


def _input() -> AgentInput:
    return AgentInput(correlation_id="corr-1", ids={"address_id": "sf-0450-hayes"})


@pytest.mark.authorization
async def test_an_agent_missing_a_scope_is_denied_not_run() -> None:
    result = await _runtime().invoke(
        _descriptor(), _input(), _grant(frozenset({Scope.READ_GEOMETRY}))
    )
    assert result.status is AgentRunStatus.DENIED
    assert result.error_code == "NOT_AUTHORIZED"


@pytest.mark.authorization
async def test_an_expired_grant_is_denied() -> None:
    expired = _grant(
        frozenset({Scope.READ_PROFILE}),
        issued_at=NOW - timedelta(hours=8),
        expires_at=NOW - timedelta(hours=1),
    )
    result = await _runtime().invoke(_descriptor(), _input(), expired)
    assert result.status is AgentRunStatus.DENIED
    assert result.error_code == "GRANT_EXPIRED"


@pytest.mark.authorization
async def test_a_revoked_grant_is_denied() -> None:
    revoked = _grant(frozenset({Scope.READ_PROFILE})).revoke(at=NOW - timedelta(minutes=1))
    result = await _runtime().invoke(_descriptor(), _input(), revoked)
    assert result.status is AgentRunStatus.DENIED


async def test_an_authorized_agent_completes() -> None:
    result = await _runtime().invoke(
        _descriptor(), _input(), _grant(frozenset({Scope.READ_PROFILE}))
    )
    assert result.status is AgentRunStatus.COMPLETED
    assert result.agent_ref == "reconciler@1.0.0"
    assert result.duration_ms >= 0


async def test_a_past_deadline_times_out() -> None:
    result = await _runtime().invoke(
        _descriptor(),
        _input(),
        _grant(frozenset({Scope.READ_PROFILE})),
        deadline=NOW - timedelta(seconds=1),
    )
    assert result.status is AgentRunStatus.TIMED_OUT


@pytest.mark.degraded
async def test_a_failing_agent_still_reaches_a_terminal_state() -> None:
    runtime = _runtime(scripted_failures={"reconciler": "source unreachable"})
    result = await runtime.invoke(_descriptor(), _input(), _grant(frozenset({Scope.READ_PROFILE})))
    assert result.status is AgentRunStatus.FAILED
    assert result.finished_at >= result.started_at


# ------------------------------------------------------------------ model ---

DOC = (
    "Permit 2018-04871: convert attic of this 2-story wood-frame dwelling "
    "built in 1911. Stairwell partially obstructed at time of inspection."
)


async def test_extraction_binds_every_value_to_a_real_span() -> None:
    client = FakeModelClient()
    result = await client.extract(
        document_text=DOC,
        schema_keys=(Keys.STORIES, Keys.YEAR_BUILT, Keys.EGRESS_OBSTRUCTION),
        source_ref="permit/2018-04871",
        deadline_ms=2000,
    )
    assert result.accepted is True
    for value in result.values:
        quoted = DOC[value.span.start_offset : value.span.end_offset]
        assert quoted == value.span.quoted_text
        assert quoted == value.raw_value


async def test_extraction_reports_what_it_could_not_determine() -> None:
    """A model that must name its unknowns cannot quietly fill one in."""
    client = FakeModelClient()
    result = await client.extract(
        document_text=DOC,
        schema_keys=(Keys.STORIES, Keys.SUPPRESSION_SPRINKLERED),
        source_ref="permit/2018-04871",
        deadline_ms=2000,
    )
    assert Keys.SUPPRESSION_SPRINKLERED in result.unknowns
    assert Keys.STORIES not in result.unknowns


async def test_extraction_is_deterministic() -> None:
    client = FakeModelClient()
    first = await client.extract(
        document_text=DOC, schema_keys=(), source_ref="r", deadline_ms=1000
    )
    second = await client.extract(
        document_text=DOC, schema_keys=(), source_ref="r", deadline_ms=1000
    )
    assert first.values == second.values


async def test_rejected_output_is_marked_not_silently_accepted() -> None:
    client = FakeModelClient(reject_output=True)
    result = await client.extract(
        document_text=DOC, schema_keys=(Keys.STORIES,), source_ref="r", deadline_ms=1000
    )
    assert result.accepted is False
    assert result.values == ()
    assert result.rejection_reason


@pytest.mark.degraded
async def test_an_unavailable_model_raises_rather_than_returning_prose() -> None:
    client = FakeModelClient(unavailable=True)
    with pytest.raises(UpstreamTimeoutError):
        await client.compose(template_id="brief", fields={}, max_chars=100, deadline_ms=100)


async def test_compose_respects_its_character_budget() -> None:
    client = FakeModelClient()
    result = await client.compose(
        template_id="brief",
        fields={"construction": "wood-frame", "stories": "2 (disputed: 3)"},
        max_chars=20,
        deadline_ms=1000,
    )
    assert len(result.text) <= 20
    assert result.truncated is True


# ------------------------------------------------------------------ clock ---


def test_deterministic_ids_are_reproducible() -> None:
    a = DeterministicIdGenerator("seed-1")
    b = DeterministicIdGenerator("seed-1")
    assert [a.new_id("fact") for _ in range(3)] == [b.new_id("fact") for _ in range(3)]


def test_different_seeds_diverge() -> None:
    assert DeterministicIdGenerator("a").new_id("fact") != DeterministicIdGenerator("b").new_id(
        "fact"
    )


def test_idempotency_keys_are_derived_not_random() -> None:
    generator = DeterministicIdGenerator("seed")
    first = generator.idempotency_key("referral", "sf-0450-hayes", "conf-1")
    second = generator.idempotency_key("referral", "sf-0450-hayes", "conf-1")
    assert first == second
    assert first != generator.idempotency_key("referral", "sf-1215-fell", "conf-1")


def test_clocks_are_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None
    assert FixedClock(NOW).now() == NOW


def test_naive_clock_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 - that is the point


def test_stepping_clock_advances_monotonically() -> None:
    clock = SteppingClock(NOW, step=timedelta(seconds=1))
    first, second = clock.now(), clock.now()
    assert second > first
