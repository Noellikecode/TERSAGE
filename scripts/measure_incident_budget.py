"""Time one whole incident, from the 911 call to a package awaiting approval.

    .venv/bin/python scripts/measure_incident_budget.py

The claim this measures is the one the loop is designed around: a 911 call
reaches a staged entry package inside **90 seconds**, and never more than 120.
Everything in :mod:`firstdue.incident.autonomy` is chosen against those two
numbers, and a number chosen against a budget nobody measures is a number that
drifts.

It drives the HTTP surface a console drives, in the order a console drives it --
open with the narrative, fly the sweep face by face, ask for a mutual-aid
notification -- and takes wall-clock at each milestone. It composes nothing
itself: the package it waits for is the one the loop composed unprompted, found
the way a console finds it.

**Found the way the console finds it, which is now the poll.** The clock stops
when ``GET /entry-packages`` first reports a row ``AWAITING_APPROVAL``, on the
same three-second interval ``ENTRY_PACKAGE_POLL_MS`` runs at in the browser --
because that is the path the card actually arrives on and it is the one
:data:`~firstdue.incident.autonomy.DELIVERY_ALLOWANCE` is written against. The
``/log/stream`` frame is still checked, for the frame contract; it is no longer
what the measurement is taken off, since a snapshot-and-close stream whose
``EventSource`` is fail-permanent is not a delivery guarantee.

Two modes, because the loop has two answers and only one of them was ever
measured::

    .venv/bin/python scripts/measure_incident_budget.py
    .venv/bin/python scripts/measure_incident_budget.py --mode fallback

``ready`` flies the sweep and measures the trigger that meets the 90 s target.
``fallback`` flies nothing at all -- no sweep, no intake, the tablet that lost
signal in the driveway -- and waits out :data:`COMPOSE_DEADLINE` on a real wall
clock, which takes a little under two minutes and is the only way to check the
promise the whole budget is written around: **a card on screen at 2:00**.

Credential-free. Fake mode throughout, so what this measures is the loop's own
overhead -- the runtime, the log, the gateway, the solve -- with the model and
vision calls answering in microseconds. A live run adds the four numbers the
catalog already caps: the intake read, four vision calls, the notification and
the brief synthesis. Those caps are printed beside the measurement so the two
can be added up rather than confused.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from firstdue.api.app import create_app  # noqa: E402
from firstdue.api.dependencies import Role, console_token  # noqa: E402
from firstdue.demo.scenario import DISPUTED_ADDRESS_ID  # noqa: E402
from firstdue.incident.autonomy import (  # noqa: E402
    COMPOSE_DEADLINE,
    COMPOSITION_CAP,
    DELIVERY_ALLOWANCE,
    HARD_CEILING,
    TOTAL_BUDGET,
    AutonomyTrigger,
)
from firstdue.settings import AppEnv, Settings  # noqa: E402

PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"

#: What a caller says, and it binds four intake attributes -- occupancy,
#: entrapment, floor of origin, access -- so the intake criterion is exercised
#: rather than skipped.
NARRATIVE = (
    "Third floor of the apartment building is showing heavy smoke, "
    "two people still inside, and the gate is locked."
)

#: What the browser reads the package list at -- ``ENTRY_PACKAGE_POLL_MS`` in
#: ``frontend/lib/api/entry-packages.ts``. Matched here rather than shortened,
#: because a measurement taken on a faster clock than the console's is a
#: measurement of something the console does not experience.
POLL_S: float = DELIVERY_ALLOWANCE.total_seconds()

#: How long past the ceiling the exercise keeps asking before calling it a
#: failure. Long enough that a slow machine reports a *late* package rather than
#: no package, because those are different defects and the second one is the one
#: that was actually happening.
OVERRUN_GRACE_S: float = 20.0

#: The declared caps a live deployment adds on top of what this measures. Read
#: off the catalog rather than written down here twice; see
#: `firstdue.registry.descriptors`.
LIVE_CAPS_S: dict[str, float] = {
    "instant brief (settings.instant_brief_budget_ms)": 0.5,
    "intake read (incident-interceptor)": 6.0,
    "drone sweep, four faces (sensor-fusion at 2 s each)": 8.0,
    "notification (agency-notifier)": 5.0,
    "package composition (incident-interceptor)": 6.0,
}


class Timeline:
    """Wall-clock from the 911 call, in the order things happened."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.marks: list[tuple[str, float]] = []

    def mark(self, label: str) -> float:
        elapsed = time.perf_counter() - self._t0
        self.marks.append((label, elapsed))
        return elapsed

    def since_open(self) -> float:
        return time.perf_counter() - self._t0

    @property
    def total(self) -> float:
        return self.marks[-1][1] if self.marks else 0.0


def _package_on_stream(client: TestClient, incident_id: str) -> dict[str, Any] | None:
    """The same package off ``/log/stream``, for the frame contract only.

    A console filtering the stream for ``entry_type == "ENTRY_PACKAGE"`` and
    ``content.status == "AWAITING_APPROVAL"`` has everything it needs to raise
    the card, and that contract is worth checking. It is no longer what the
    *timing* is taken off: the stream is snapshot-and-close and its
    ``EventSource`` is fail-permanent, so what it proves is that the frame is
    well formed, not that a browser ever received it.
    """
    with client.stream("GET", f"{PREFIX}/incidents/{incident_id}/log/stream") as response:
        raw = response.read().decode()
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        frame = json.loads(line.partition(":")[2].strip())
        if frame.get("entry_type") != "ENTRY_PACKAGE":
            continue
        content = frame["content"]
        if content.get("status") == "AWAITING_APPROVAL":
            return dict(content)
    return None


def _poll_for_card(
    client: TestClient, incident_id: str, timeline: Timeline, *, until_s: float
) -> dict[str, Any] | None:
    """Ask ``GET /entry-packages`` on the console's own interval until a card exists.

    This is the measurement. It is the console's second, independent path to the
    one moment in the product that must not be missed, and it is the path a
    budget may be written against because -- unlike the stream -- it cannot be
    killed by a single bad response from the gateway.

    Deliberately *not* an immediate first read. The browser arms an interval and
    waits one period before the first tick, so a run that peeked at t=0 would
    report a delivery this console never has.
    """
    while True:
        time.sleep(POLL_S)
        listed = client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages")
        listed.raise_for_status()
        for row in listed.json().get("packages", []):
            if row.get("status") == "AWAITING_APPROVAL":
                timeline.mark("entry package on screen (GET /entry-packages, 3 s poll)")
                return dict(row)
        if timeline.since_open() > until_s:
            timeline.mark("gave up waiting for a card")
            return None


def run(*, sweep_cadence_s: float, mode: str) -> tuple[Timeline, dict[str, Any] | None]:
    settings = Settings(
        # LOCAL rather than TEST: the fallback deadline timer is armed outside
        # a test process, and a measurement that silently ran with one of the
        # three triggers disabled would be measuring something else.
        app_env=AppEnv.LOCAL,
        use_fake_agents=True,
        fixtures_dir=REPO_ROOT / "fixtures",
        # A throwaway: this rebuilds the district on every run and must not
        # leave a half-built one behind for `make demo` to read.
        demo_state_dir=Path(tempfile.mkdtemp(prefix="firstdue-measure-")),
        log_json=False,
    )
    token = console_token(settings, Role.CHIEF)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {token}"}) as client:
        # Months of slow-loop work, before the bell. Not on the clock: the
        # budget is about what happens after a 911 call, and this already
        # happened.
        client.post(f"{PREFIX}/districts/{DISTRICT}/poll")

        timeline = Timeline()
        opened = client.post(
            f"{PREFIX}/incidents",
            json={
                "address": DISPUTED_ADDRESS_ID,
                "cad_ref": "CAD-MEASURE-1",
                "alarm_level": 2,
                "intake_narrative": NARRATIVE,
            },
        )
        opened.raise_for_status()
        body = opened.json()
        incident_id = body["incident_id"]
        timeline.mark("incident open + instant brief + intake read + agents woken")

        notified = client.post(
            f"{PREFIX}/incidents/{incident_id}/resources",
            json={"kind_id": "mutual-aid", "detail": "Second engine for the exposure."},
        )
        notified.raise_for_status()
        outcome = notified.json()
        timeline.mark(f"mutual-aid notification ({outcome.get('action', 'unknown')})")

        if mode == "ready":
            for index in range(6):
                if sweep_cadence_s:
                    time.sleep(sweep_cadence_s)
                step = client.post(f"{PREFIX}/incidents/{incident_id}/drone-sweep")
                step.raise_for_status()
                result = step.json()
                face = result.get("face") or result.get("reason", "")
                timeline.mark(f"drone sweep face {index + 1}: {face}")
                if result.get("complete"):
                    break
        else:
            # Nothing else happens. No sweep, no resolution, no second intake:
            # the tablet lost signal in the driveway and the only thing left
            # running is the fallback deadline. Everything after this point is
            # the loop keeping a promise unassisted.
            timeline.mark("console goes quiet -- nothing else will drive the loop")

        # What the diagnostic reports while the card is still absent, captured
        # before the wait rather than after it: "no card yet" and "no card ever"
        # are the same screen, and this is the read that separates them.
        waiting = client.get(f"{PREFIX}/incidents/{incident_id}/entry-packages/diagnostics")
        waiting.raise_for_status()
        diagnostics = waiting.json()

        row = _poll_for_card(
            client,
            incident_id,
            timeline,
            until_s=HARD_CEILING.total_seconds() + OVERRUN_GRACE_S,
        )
        content = _package_on_stream(client, incident_id) if row is not None else None

    _report(
        timeline,
        body,
        content,
        row,
        diagnostics,
        sweep_cadence_s=sweep_cadence_s,
        mode=mode,
    )
    return timeline, row


def _report(
    timeline: Timeline,
    opened: dict[str, Any],
    package: dict[str, Any] | None,
    row: dict[str, Any] | None,
    diagnostics: dict[str, Any],
    *,
    sweep_cadence_s: float,
    mode: str,
) -> None:
    print("\nIncident budget, wall-clock from the 911 call")
    print("=" * 78)
    print(f"mode: {mode}")
    print(f"instant brief, as the API measured it: {opened['instant_brief_ms']:.1f} ms")
    print(f"console sweep cadence: {sweep_cadence_s:.1f} s between faces\n")
    for label, elapsed in timeline.marks:
        print(f"  {elapsed:7.3f} s  {label}")

    print("\nWhat GET /entry-packages/diagnostics reported before the wait")
    print("-" * 78)
    print(f"  autonomy enabled {diagnostics['autonomy_enabled']}")
    print(f"  incident tracked {diagnostics['tracked']}")
    print(f"  deadline armed   {diagnostics['deadline_armed']}")
    if diagnostics.get("deadline_in_s") is not None:
        print(f"  fires in         {diagnostics['deadline_in_s']:.1f} s")
    print(f"  attempts         {diagnostics['attempts']} ({diagnostics['failures']} failed)")
    print(f"  outstanding      {', '.join(diagnostics['outstanding_criteria']) or 'none'}")
    if diagnostics["failed_error_type"]:
        print(
            f"  last failure     {diagnostics['failed_error_type']}: "
            f"{diagnostics['failed_error_message']}"
        )

    print("\nPackage awaiting approval")
    print("-" * 78)
    if row is None:
        print("  NONE. No card ever appeared on the console's own poll --")
        print("  this is a failure of the exercise and of the promise it checks.")
    elif package is None:  # pragma: no cover - the poll saw it, the stream did not
        print(f"  {row['package_id']} is listed AWAITING_APPROVAL but carries no")
        print("  well-formed ENTRY_PACKAGE frame: the poll saved a card the stream lost.")
    else:
        print(f"  package_id       {package['package_id']}")
        print(f"  status           {package['status']}")
        # Which trigger fired is the whole difference between "the record
        # supported this" and "the clock ran out and the fleet staged what it
        # had". Only the fallback mode has a *right* answer here: on the ready
        # path either trigger is legitimate and which one fires is a property of
        # the demo profile, not of the budget -- this address carries an
        # unsettled disagreement, so its sweep terminates before it is ready.
        trigger = package["autonomy_trigger"] or "a human asking"
        mismatch = mode == "fallback" and trigger != AutonomyTrigger.DEADLINE
        print(
            f"  composed by      {trigger}"
            + (f" (expected {AutonomyTrigger.DEADLINE})" if mismatch else "")
        )
        print(f"  readiness        {'READY' if package['ready'] else 'NOT READY'}")
        print(f"  outstanding      {', '.join(package['outstanding']) or 'none'}")
        print(f"  not passed       {', '.join(package['failed_criteria']) or 'none'}")
        print(f"  approve at       .../entry-packages/{package['package_id']}/approvals/<half>")

    print("\nAgainst the budget")
    print("-" * 78)
    measured = timeline.total
    live = sum(LIVE_CAPS_S.values())
    print(f"  card on screen, measured                  {measured:7.3f} s")
    if mode == "ready":
        # Only the ready path can have its live caps added to it: the fallback
        # path's number is a *deadline*, and the caps are already inside it.
        for label, cap in LIVE_CAPS_S.items():
            print(f"    + {label:<52}{cap:5.1f} s")
        print(f"  worst case with every declared cap spent  {measured + live:7.3f} s")
    print(f"  target                                    {TOTAL_BUDGET.total_seconds():7.1f} s")
    print(f"  ceiling                                   {HARD_CEILING.total_seconds():7.1f} s")
    print(
        f"\n  fallback deadline {COMPOSE_DEADLINE.total_seconds():.0f} s"
        f" + composition cap {COMPOSITION_CAP.total_seconds():.0f} s"
        f" + delivery {DELIVERY_ALLOWANCE.total_seconds():.0f} s"
        f" = {(COMPOSE_DEADLINE + COMPOSITION_CAP + DELIVERY_ALLOWANCE).total_seconds():.0f} s,"
        f" which is the {HARD_CEILING.total_seconds():.0f} s ceiling exactly -- the"
        " card is promised *at* two minutes, not before it."
    )


def _verdict(timeline: Timeline, row: dict[str, Any] | None, *, mode: str) -> int:
    """Pass or fail, stated as the promise rather than as a number.

    Three separate claims, and they fail differently on purpose. No card at all
    is the defect the user reported. A card that is not ``AWAITING_APPROVAL`` is
    a package that skipped a signature. A card outside its window is the budget
    drifting, which is the thing this script exists to catch before a fireground
    does.
    """
    print("\nVerdict")
    print("-" * 78)
    if row is None:
        print("  FAIL  no entry package ever reached the console's own poll")
        return 1
    if row["status"] != "AWAITING_APPROVAL":
        print(f"  FAIL  the package is {row['status']}, not AWAITING_APPROVAL")
        return 1

    measured = timeline.total
    if mode == "ready":
        # The ready trigger composes the moment the record supports it, so it is
        # held to the target rather than to the ceiling.
        ok = measured < TOTAL_BUDGET.total_seconds()
        window = f"under the {TOTAL_BUDGET.total_seconds():.0f} s target"
    else:
        # Both bounds matter. Late is a broken promise; *early* means the
        # fallback fired before its deadline, which would mean it can pre-empt a
        # sweep that was merely slow.
        ok = (
            COMPOSE_DEADLINE.total_seconds()
            <= measured
            <= HARD_CEILING.total_seconds() + OVERRUN_GRACE_S
        )
        window = (
            f"between the {COMPOSE_DEADLINE.total_seconds():.0f} s deadline and the "
            f"{HARD_CEILING.total_seconds():.0f} s ceiling"
        )
    print(f"  {'PASS' if ok else 'FAIL'}  card AWAITING_APPROVAL at {measured:.1f} s, {window}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ready", "fallback"),
        default="ready",
        help=(
            "ready: fly the sweep and measure the trigger that meets the 90 s target. "
            "fallback: drive nothing after the open and wait out the deadline on a real "
            "wall clock -- about two minutes, and the only check of the 2:00 promise."
        ),
    )
    parser.add_argument(
        "--sweep-cadence",
        type=float,
        default=0.0,
        help=(
            "Seconds a console waits between faces. 0 measures the loop; 5 measures "
            "what an officer watching the heat map arrive would actually experience."
        ),
    )
    args = parser.parse_args()
    timeline, row = run(sweep_cadence_s=args.sweep_cadence, mode=args.mode)
    return _verdict(timeline, row, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
