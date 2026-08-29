"""When the incident loop decides for itself that enough is recorded.

Everything the entry package needs already existed -- the six criteria, the A*
solve, the fact-grounded synthesis, the two approval halves -- and none of it
happened until a human pressed a button. That is a strange gap in a system whose
whole argument is that the fleet works while the commander is still driving:
the interceptor holds the judgement (it is the agent that reads the whole
record) and had no moment at which to exercise it.

This module is that moment, and it is a decision rather than a schedule. There
is no polling loop here, because the loop already emits the events that change
the answer: a frame lands, an intake is read, an IC settles a conflict, the
sweep stops. Each of those is a point where readiness can have moved, so each of
them asks -- once, cheaply, over data already in memory -- whether the record
now supports handing a crew a plan.

**The budget is the argument.** The whole incident loop is designed to reach a
staged package in 90 seconds and never more than 120, measured from the 911
call. So there is a second trigger that does not wait for readiness at all, and
its deadline is picked backwards from that ceiling -- see
:data:`COMPOSE_DEADLINE`. A loop that composed only when everything passed would
be a loop that, on the incidents that need it most -- the cold-start address,
the refused sweep, the intake nobody could read -- composed nothing at all and
left a commander with the button it was supposed to have pressed.

The two numbers are not two names for the same promise. The 90 s target belongs
to the triggers that read the record -- readiness passing, the sweep stopping --
and those fire as early as the record allows. The 120 s ceiling belongs to the
deadline, and the deadline is sized to *reach* it rather than to beat it,
because the only thing a commander can be given at 120 s on an incident where
nothing ever passed is a package that says so. Composing that same package at
45 s buys nothing and costs the coverage that was still flying in.

**A fallback composition is never quiet about being one.** It carries the same
:class:`~firstdue.incident.readiness.ReadinessAssessment` the ready path does,
with ``ready`` false and ``failed_ids`` naming every criterion that did not
pass, printed on the PDF and flattened onto the log entry the console reads.
Nothing here fills a gap in: the whole point of composing early is to put the
gaps in front of somebody, and a package that composed early and read as ready
would be worse than no package.

**Composing is not approving.** This decides to stage a document and stage two
approval cards. Both halves are still signed by a human and the send is still a
human's, exactly as they were when the only caller was a button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.incident.readiness import ReadinessAssessment
from firstdue.registry.descriptors import descriptor_for

#: How much of an exception message is kept. Long enough to name a missing
#: snapshot id or a refused scope, short enough that a stack of them cannot turn
#: the diagnostic into a second copy of the record.
MAX_ERROR_CHARS: Final[int] = 200

#: What the incident loop as a whole is designed to. 911 call, incident open,
#: intake read, agents woken, sweep flown, notifications sent, package staged
#: and on screen awaiting approval.
TOTAL_BUDGET: Final[timedelta] = timedelta(seconds=90)

#: And the number it may never exceed. It is not head-room -- it is what
#: :data:`COMPOSE_DEADLINE` is solved against, so the worst path this module can
#: take lands *on* it rather than inside it, and one second more anywhere below
#: is a second over.
HARD_CEILING: Final[timedelta] = timedelta(seconds=120)

#: What a composition costs once it is decided on, worst case. It is the
#: ``incident-interceptor`` descriptor's own ``latency_target_ms``, which the
#: runtime enforces as a hard cap on the run -- not an estimate written here
#: that could drift away from the catalog. Readiness is six predicates over a
#: snapshot already in memory, the A* solve is pure compute over a graph of
#: tens of nodes, and the brief synthesis is one model call; the cap is the
#: model call and the runtime cancels the run if it overruns.
#:
#: Read off the descriptor rather than written here, because the sentence above
#: claimed it already was and it was the literal ``timedelta(seconds=6)`` --
#: exactly the drift it disclaims. When the interceptor's cap moved from 6 s to
#: 12 s this stayed at 6, and the identity below silently stopped being true:
#: the budget said a card arrives by 120 s while the composition it budgeted
#: for could take twice what was set aside. Derived, the arithmetic cannot come
#: apart from the catalog again.
COMPOSITION_CAP: Final[timedelta] = timedelta(
    milliseconds=descriptor_for("incident-interceptor").latency_target_ms
)

#: Staged to on screen, worst case. It used to be 1 s, described as "SSE frame
#: out, console renders the card", and that was the number for a delivery path
#: that no longer exists on its own. ``GET /log/stream`` is snapshot-and-close
#: and its ``EventSource`` is fail-permanent -- one non-SSE answer from the
#: console gateway and the browser never re-opens -- so the console now also
#: reads ``GET /entry-packages`` on a timer, and *that* is the path a budget may
#: be written against because it is the one that cannot be killed by a single
#: bad response. The timer is ``ENTRY_PACKAGE_POLL_MS`` in
#: ``frontend/lib/api/entry-packages.ts``; a package staged just after a tick
#: waits one whole interval, so the interval is the allowance.
#:
#: "Render is inside it" was the part that was wrong, and it was wrong before
#: anything was added to it. The console does not render the card when the
#: package lands -- it draws the entry route first and raises the card after,
#: so the officer sees the walk before being asked to sign for it. That draw is
#: ``ROUTE_DRAW_BUDGET_MS`` (1.6 s) and it was never in this number, so the
#: ceiling arithmetic had been quietly 1.6 s optimistic since the animation
#: was written.
#:
#: It is now counted, together with ``ROUTE_HOLD_MS`` (2 s) -- the beat that
#: leaves the *finished* route alone before the modal covers it, without which
#: the completed path was on screen for zero frames. Both live in
#: ``frontend/components/StructureModel.tsx``.
#:
#:   3 s poll interval + 1.6 s draw + 2 s hold = 6.6 s, taken as 7 s.
#:
#: Rounded up rather than down: every other term here is a cap something
#: enforces, and this one is the only estimate, so it should be the one that
#: errs against us.
DELIVERY_ALLOWANCE: Final[timedelta] = timedelta(seconds=7)

#: The fallback deadline, measured from incident open. Solved backwards from
#: the ceiling rather than picked, because what a commander is promised is an
#: *arrival*, not a start:
#:
#:   93 s (this) + 20 s (:data:`COMPOSITION_CAP`) + 7 s (:data:`DELIVERY_ALLOWANCE`)
#:     = 120 s  =  :data:`HARD_CEILING`
#:
#: It was 111 s against a 6 s cap and a 3 s allowance, and both of those turned
#: out to be measurements nobody had taken. Every time one of them is corrected
#: this term gives back the difference rather than letting the sum drift over
#: the ceiling -- which is the entire reason it is solved and not chosen. The
#: assertion below is what keeps the three numbers honest about each other.
#:
#: Every term on the left is a cap something else already enforces, so the sum
#: is a bound and not a hope: the runtime cancels the composing run at the
#: ``incident-interceptor`` descriptor's ``latency_target_ms``, and the console
#: reads the package list on a fixed interval whether or not anything else is
#: working. 111 s is therefore the *latest* instant a composition can start and
#: still have the card on a screen at two minutes, and starting it any earlier
#: only moves the card earlier.
#:
#: It was 45 s, chosen to land the fallback inside the 90 s
#: :data:`TOTAL_BUDGET`, and that traded the wrong thing away. The target is
#: met by the trigger that is *supposed* to meet it -- readiness passing, which
#: on a healthy incident happens nearer 25 s and composes immediately -- and by
#: the sweep terminating, which composes the moment the record stops changing.
#: The deadline is the trigger for the incidents where neither will ever
#: happen, and on those there is nothing to be gained by composing at 45 s
#: except pre-empting a sweep that was merely slow: a fallback that fires while
#: coverage is still arriving spends the package's thermal claims to buy
#: head-room nobody was going to use. Held at 111 s it fires only when the loop
#: has genuinely stalled, and it still lands on the two-minute mark the whole
#: budget is written against.
#:
#: Nothing here waits *for* the deadline. It is a ceiling on when the card
#: appears, not a schedule for it.
COMPOSE_DEADLINE: Final[timedelta] = HARD_CEILING - COMPOSITION_CAP - DELIVERY_ALLOWANCE

#: The identity above, enforced rather than described.
#:
#: Every term is now derived from something else -- the ceiling is the promise,
#: the cap is the catalog -- so the one way this can break is a descriptor
#: change that leaves no room to compose in. That is a configuration error and
#: it should be loud at import, not a card that quietly arrives late.
if COMPOSE_DEADLINE.total_seconds() <= 0:  # pragma: no cover - a catalog misconfiguration
    raise ValueError(
        "the incident-interceptor's latency cap leaves no time to reach the "
        "composition: HARD_CEILING - COMPOSITION_CAP - DELIVERY_ALLOWANCE is "
        f"{COMPOSE_DEADLINE}"
    )


class AutonomyTrigger(StrEnum):
    """Why the loop composed without being asked.

    Written onto the log entry the console reads, because "the fleet decided
    this was ready" and "the fleet ran out of time and composed what it had"
    are different claims about the same document and a reader must not have to
    infer which one happened from the readiness verdict alone.
    """

    #: All six criteria passed. The first time they did, and only the first.
    READY = "ready"
    #: The sweep stopped -- finished, refused, or out of faces it could read --
    #: and the record is what it is going to be.
    SWEEP_TERMINATED = "sweep-terminated"
    #: Nothing terminated and the clock ran out. See :data:`COMPOSE_DEADLINE`.
    DEADLINE = "deadline"


@dataclass(slots=True)
class AutonomyState:
    """What one incident has already decided, so it does not decide it twice."""

    #: When this incident opened, on the same clock every record here is
    #: stamped with. The deadline is measured from here rather than from
    #: ``dispatched_at``: CAD's dispatch time is a field a caller can set to
    #: anything, and a deadline derived from it would be a deadline a demo
    #: could accidentally start in the past.
    opened_at: datetime
    #: Set for the duration of a composition. Every hook is a coroutine and the
    #: composition awaits a model, so without this a frame landing mid-compose
    #: would start a second one against the same record.
    composing: bool = False
    composed_package_id: str = ""
    #: The readiness the last composition was made against. What "materially
    #: changed" means, and the whole of the re-composition guard.
    composed_signature: str = ""
    #: How the last one was decided. Reported, never re-read as a condition.
    composed_trigger: str = ""

    # ------------------------------------------------ what went wrong, if any
    #
    # Composition is optional and every failure here is a shrug -- see
    # ``IncidentSession._consider_entry_package``. That policy is right and it
    # is also why three separate live failures left nothing behind but a log
    # line reading ``error_type``. A shrug may be silent to the *sweep*; it may
    # not be silent to the person asking why no card appeared. So the shrug
    # writes down what it shrugged at, here, where a read-only endpoint can
    # report it without re-running anything.

    #: Compositions started, successful or not. Zero with a package present
    #: means a human pressed the button and the loop never decided anything.
    attempts: int = 0
    #: Of those, the ones that raised. The number a diagnostic leads with.
    failures: int = 0
    failed_at: datetime | None = None
    #: The trigger the failed attempt was taken under, so "it never decided to
    #: compose" and "it decided and the composition died" are distinguishable.
    failed_trigger: str = ""
    failed_error_type: str = ""
    #: Capped at :data:`MAX_ERROR_CHARS`. Exception messages in this codebase
    #: are stable prose with ids in ``details``; nothing puts a document in one.
    failed_error_message: str = ""
    #: The criteria outstanding when it failed. The other half of "why": a
    #: composition that died is a different problem from one that was never
    #: triggered because the record still had gaps and time left.
    failed_criteria: tuple[str, ...] = field(default_factory=tuple)

    def record_failure(
        self,
        *,
        trigger: str,
        error_type: str,
        message: str,
        failed_ids: tuple[str, ...],
        at: datetime,
    ) -> None:
        """Remember one shrug. Called from the handler that swallows it."""
        self.failures += 1
        self.failed_at = at
        self.failed_trigger = trigger
        self.failed_error_type = error_type
        self.failed_error_message = message[:MAX_ERROR_CHARS]
        self.failed_criteria = failed_ids


class AutonomyDiagnostics(BaseModel):
    """What the loop has decided about one incident, and why there is no card.

    Read-only, and the reason it exists is that the three triggers above are
    the only thing standing between a dispatch and an approval card, and every
    one of them can decline silently and correctly. Autonomy switched off, an
    incident this process did not open, a deadline still sleeping, a
    composition that raised inside a shrug -- four completely different
    situations that all look identical from a console showing nothing.
    Guessing between them from the outside took three failed incidents.

    It reports decisions, never content. Ids, counts, canonical criterion keys,
    an exception type and a capped message; no claim, no prose, no fact value.
    A reader who needs the document reads the package.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    #: ``settings.entry_package_autonomy``. False means nothing below will ever
    #: happen and the console's own button is the only path to a package.
    autonomy_enabled: bool
    #: Whether *this* process holds the incident's autonomy state. False on a
    #: replay or a worker that came up mid-incident: the triggers are declining
    #: correctly and no amount of waiting will change it.
    tracked: bool
    opened_at: datetime | None = None
    #: Seconds since the open, on the same clock the deadline is measured on.
    age_s: float | None = None

    #: The sleeping task exists and has not fired. False *and* no package means
    #: either the incident closed, the deadline already fired, or the timer was
    #: never armed -- which is the ordinary case under ``AppEnv.TEST``.
    deadline_armed: bool
    deadline_at: datetime | None = None
    deadline_in_s: float | None = None

    #: A composition is in flight right now. A card is seconds away.
    composing: bool
    attempts: int
    failures: int
    composed_package_id: str = ""
    composed_trigger: str = ""

    failed_at: datetime | None = None
    failed_trigger: str = ""
    failed_error_type: str = ""
    failed_error_message: str = ""
    #: Criteria outstanding at the last failure.
    failed_criteria: tuple[str, ...] = ()

    #: Criteria outstanding *now*, from a silent re-assessment. The direct
    #: answer to "why has it not composed yet" on an incident that is simply
    #: still waiting: these are the gaps the READY trigger is waiting on.
    outstanding_criteria: tuple[str, ...] = ()
    #: Why the re-assessment above could not be made, when it could not. An
    #: empty tuple of criteria is a claim ("nothing outstanding") and must never
    #: be the way a failed probe renders.
    assessment_error: str = ""

    #: Packages the incident log holds, whoever composed them.
    packages: int = 0


def readiness_signature(assessment: ReadinessAssessment) -> str:
    """The state of the record, as far as composing another package cares.

    Two assessments with the same signature describe a record that has not
    moved in any way an entry package would be different about, so composing
    again would produce the same three documents, two more approval cards for a
    commander to triage, and another entry in a log somebody has to read under
    load. Coverage arriving on a face that was already scanned changes a
    temperature and does not change this; the last UNSCANNED face being flown
    changes it, because that is the criterion flipping.

    Deliberately coarse. It is the *verdict* per criterion, not the reasons --
    the reasons carry counts and measured values, which would make every frame
    a material change and put the guard back where it started.
    """
    if assessment.ready:
        return "ready"
    return "failed:" + ",".join(assessment.failed_ids)


def decide(
    *,
    state: AutonomyState,
    assessment: ReadinessAssessment,
    now: datetime,
    sweep_terminated: bool = False,
    deadline_elapsed: bool = False,
) -> AutonomyTrigger | None:
    """Compose now, and if so why. Pure, total, and the same on every replay.

    The order is the safety argument. Re-entrancy is refused first, because a
    composition in flight is already answering this question. Then the
    material-change guard, which is what stops four faces producing four
    packages. Only then the three triggers, readiness first: a package composed
    because the record supports one is the outcome this whole feature is for,
    and the two fallbacks exist to make sure *something* is staged on the
    incidents where it never will.
    """
    if state.composing:
        return None
    signature = readiness_signature(assessment)
    if state.composed_package_id and signature == state.composed_signature:
        # Composed once already against exactly this verdict. Anything that has
        # happened since changed a number, not an answer.
        return None
    if assessment.ready:
        return AutonomyTrigger.READY
    if sweep_terminated:
        return AutonomyTrigger.SWEEP_TERMINATED
    if deadline_elapsed or now - state.opened_at >= COMPOSE_DEADLINE:
        return AutonomyTrigger.DEADLINE
    # Not ready, nothing has stopped, and there is still time. Waiting is the
    # decision: a package composed now would be missing coverage that is on its
    # way, and the deadline is what guarantees the wait is bounded.
    return None


__all__ = [
    "COMPOSE_DEADLINE",
    "COMPOSITION_CAP",
    "DELIVERY_ALLOWANCE",
    "HARD_CEILING",
    "MAX_ERROR_CHARS",
    "TOTAL_BUDGET",
    "AutonomyDiagnostics",
    "AutonomyState",
    "AutonomyTrigger",
    "decide",
    "readiness_signature",
]
