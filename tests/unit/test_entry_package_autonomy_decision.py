"""The decision to compose unprompted, on its own, with nothing else running.

The integration suite proves the loop composes at the right moments. This
proves the predicate underneath it: which trigger fires, and -- the part that
costs a budget when it is wrong -- when none does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from firstdue.incident.autonomy import (
    COMPOSE_DEADLINE,
    AutonomyState,
    AutonomyTrigger,
    decide,
    readiness_signature,
)
from firstdue.incident.readiness import Criterion, ReadinessAssessment

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)


def _assessment(*failed: str) -> ReadinessAssessment:
    """Six criteria, the named ones failing."""
    names = (
        "geometry.present",
        "thermal.coverage",
        "hazard.resolved",
        "conflicts.load-bearing",
        "snapshot.fresh",
        "intake.access-bound",
    )
    return ReadinessAssessment(
        incident_id="inc-1",
        address_id="sf-0450-hayes",
        assessed_at=NOW,
        assessed_by="incident-interceptor",
        profile_snapshot_id="snap-1",
        criteria=tuple(
            Criterion(
                criterion_id=name,
                title=name,
                passed=name not in failed,
                reason="checked",
            )
            for name in names
        ),
    )


def _state() -> AutonomyState:
    return AutonomyState(opened_at=NOW)


def test_all_six_passing_composes_and_says_so() -> None:
    assert decide(state=_state(), assessment=_assessment(), now=NOW) is AutonomyTrigger.READY


def test_not_ready_with_time_left_and_nothing_terminated_waits() -> None:
    """The case the deadline exists to bound. Waiting is the decision."""
    assert decide(state=_state(), assessment=_assessment("thermal.coverage"), now=NOW) is None


def test_the_sweep_stopping_composes_what_the_record_holds() -> None:
    trigger = decide(
        state=_state(),
        assessment=_assessment("thermal.coverage"),
        now=NOW,
        sweep_terminated=True,
    )
    assert trigger is AutonomyTrigger.SWEEP_TERMINATED


def test_the_deadline_composes_on_the_incidents_own_clock() -> None:
    just_short = decide(
        state=_state(),
        assessment=_assessment("thermal.coverage"),
        now=NOW + COMPOSE_DEADLINE - timedelta(seconds=1),
    )
    assert just_short is None

    past = decide(
        state=_state(),
        assessment=_assessment("thermal.coverage"),
        now=NOW + COMPOSE_DEADLINE,
    )
    assert past is AutonomyTrigger.DEADLINE


def test_a_composition_in_flight_is_never_joined_by_a_second() -> None:
    """Every hook is a coroutine and composing awaits a model.

    Without this a frame landing mid-compose would start a second composition
    against the same record and stage two more approval cards for it.
    """
    state = _state()
    state.composing = True
    assert decide(state=state, assessment=_assessment(), now=NOW) is None


def test_an_unchanged_verdict_does_not_compose_again() -> None:
    """Four faces, one package. The guard is the verdict, not a counter."""
    state = _state()
    state.composed_package_id = "pkg-1"
    state.composed_signature = readiness_signature(_assessment("thermal.coverage"))
    again = decide(
        state=state,
        assessment=_assessment("thermal.coverage"),
        now=NOW + COMPOSE_DEADLINE,
        sweep_terminated=True,
    )
    assert again is None


def test_a_verdict_that_moved_to_ready_composes_again() -> None:
    """The one recomposition an officer wants: the gaps closed."""
    state = _state()
    state.composed_package_id = "pkg-1"
    state.composed_signature = readiness_signature(_assessment("thermal.coverage"))
    assert decide(state=state, assessment=_assessment(), now=NOW) is AutonomyTrigger.READY


def test_the_signature_is_the_verdict_and_not_the_reasons() -> None:
    """Coverage arriving on an already-scanned wall is not a material change.

    A signature over the reasons would carry counts and measured values, which
    makes every frame material and puts the guard back where it started.
    """
    assert readiness_signature(_assessment()) == "ready"
    assert readiness_signature(_assessment("thermal.coverage")) == "failed:thermal.coverage"
    assert readiness_signature(_assessment("thermal.coverage")) == readiness_signature(
        _assessment("thermal.coverage")
    )
    assert readiness_signature(_assessment("thermal.coverage")) != readiness_signature(
        _assessment("thermal.coverage", "intake.access-bound")
    )
