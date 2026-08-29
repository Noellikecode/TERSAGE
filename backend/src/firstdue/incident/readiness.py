"""Is what the fleet has recorded good enough to hand a crew an entry plan?

Six named criteria, each evaluated against data this incident actually holds,
each citing the fact ids and canonical keys it looked at. The whole point is
that the answer is *legible*: an officer reading "not ready" can see which
criterion failed, what it checked, and what would settle it.

**Not ready is the first-class outcome.** There is no default pass, no criterion
that abstains, and no path that returns ready because a check could not run --
a criterion with nothing to read fails and says so, because "we could not
check" and "we checked and it is fine" are the two things this whole codebase
exists to keep apart. The assessment is rendered either way and recorded either
way; a readiness evaluation that vanished when it was negative would be worse
than none, because its absence would read as an assessment nobody ran.

**It gates nothing on its own.** Readiness is a statement about the record, not
a permission. A commander may dispatch a package this says is not ready --
knowingly, with the verdict printed on the package -- and that is the correct
distribution of authority: this module reports what is missing, and a human
decides whether to go without it. What it must never do is let the gap go
unstated.

Nothing here recommends a tactic, and no model participates. Every criterion is
a predicate over recorded values, so the same record produces the same verdict
on every run and an officer can check the arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field

from firstdue.domain.conflicts import Conflict, ConflictStatus
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import CanonicalKey, IntakeKeys, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.incident.documents import RecordedDocument
from firstdue.incident.entrypath import LOAD_BEARING_KEYS
from firstdue.incident.fusion import FaceCoverage

#: How old the profile snapshot this incident opened against may be before the
#: freshness criterion fails. Fifteen minutes is long enough that no ordinary
#: incident trips it and short enough that a session left open overnight does.
#: The snapshot is frozen at dispatch by design -- this criterion is about a
#: *stale process*, not a stale building.
MAX_SNAPSHOT_AGE: Final[timedelta] = timedelta(minutes=15)

#: Hazard attributes an entry plan is answerable for. Each is either resolved --
#: a known value, whichever way it went -- or explicitly outstanding.
HAZARD_KEYS: Final[tuple[CanonicalKey, ...]] = (
    Keys.LIGHTWEIGHT_TRUSS,
    Keys.HAZARD_TIER_II_PRESENT,
    Keys.HAZARD_SOLAR_ARRAY,
    Keys.HAZARD_EV_CHARGER,
    Keys.EGRESS_OBSTRUCTION,
)

#: Intake attributes that change how a crew goes in. A narrative that bound none
#: of them told the fleet nothing about getting inside, which is a gap whether
#: or not a narrative was read at all.
ACCESS_INTAKE_KEYS: Final[frozenset[str]] = frozenset(
    {
        IntakeKeys.REPORTED_OCCUPANCY,
        IntakeKeys.ACCESS_NOTE,
        IntakeKeys.ENTRAPMENT_REPORTED,
        IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,
    }
)


class Criterion(BaseModel):
    """One named check, its verdict, and what it looked at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(min_length=1, max_length=60)
    #: What is being asked, in the words an officer reads on the card.
    title: str = Field(min_length=1, max_length=120)
    passed: bool
    #: Why it passed or failed. States what was checked, not what to do.
    reason: str = Field(min_length=1, max_length=400)
    #: Fact ids, canonical keys, face labels, conflict ids, snapshot ids. Never
    #: a value -- the same rule the incident log and the focus keep.
    refs: tuple[str, ...] = ()


class ReadinessAssessment(RecordedDocument):
    """The whole verdict. Renderable whichever way it went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    assessed_at: datetime
    assessed_by: str = Field(min_length=1, max_length=120)
    assessed_by_version: str = Field(default="1.0.0", max_length=40)
    profile_snapshot_id: str = Field(min_length=1, max_length=120)

    criteria: tuple[Criterion, ...] = Field(min_length=1)

    # Computed rather than stored, and exposed on the wire rather than left to
    # a reader to derive: a console that had to recompute "is this ready" from
    # six booleans is a console that can disagree with the assessment it is
    # rendering.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        """Every criterion passes. There is no partial readiness."""
        return all(criterion.passed for criterion in self.criteria)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_ids(self) -> tuple[str, ...]:
        return tuple(c.criterion_id for c in self.criteria if not c.passed)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """One line, and it never rounds a failure to a pass."""
        passed = sum(1 for c in self.criteria if c.passed)
        if self.ready:
            return f"READY - all {len(self.criteria)} criteria pass"
        return (
            f"NOT READY - {passed} of {len(self.criteria)} criteria pass; "
            f"outstanding: {', '.join(self.failed_ids)}"
        )


def _geometry_criterion(snapshot: ProfileSnapshot) -> Criterion:
    spec = snapshot.geometry
    if spec is None:
        return Criterion(
            criterion_id="geometry.present",
            title="Pre-incident geometry exists",
            passed=False,
            reason=(
                "the slow loop never measured this address, so there is no footprint, "
                "no storey count and nothing a route could be computed over"
            ),
            refs=(snapshot.address_id, snapshot.snapshot_id),
        )
    if not spec.levels:
        return Criterion(
            criterion_id="geometry.present",
            title="Pre-incident geometry exists",
            passed=False,
            reason="a footprint was measured but no storeys were derived from it",
            refs=(snapshot.address_id, spec.address_id),
        )
    disputed = [
        f"level-{index}"
        for index, level in enumerate(spec.levels)
        if str(level.status) == "DISPUTED"
    ]
    return Criterion(
        criterion_id="geometry.present",
        title="Pre-incident geometry exists",
        passed=True,
        reason=(
            f"{len(spec.footprint)} footprint vertices and {len(spec.levels)} storey(s), "
            f"collapse zone {spec.collapse_zone_radius_m:g} m"
            + (f"; {len(disputed)} storey(s) DISPUTED" if disputed else "")
        ),
        refs=(snapshot.address_id, snapshot.snapshot_id, *disputed),
    )


def _thermal_criterion(coverage: Sequence[FaceCoverage]) -> Criterion:
    if not coverage:
        return Criterion(
            criterion_id="thermal.coverage",
            title="Every face has current thermal coverage",
            passed=False,
            reason=(
                "no coverage report was produced at all, so no face is measured and "
                "none may be read as cool"
            ),
        )
    missing = [report for report in coverage if not report.scanned]
    if missing:
        return Criterion(
            criterion_id="thermal.coverage",
            title="Every face has current thermal coverage",
            passed=False,
            reason=(
                f"{len(missing)} of {len(coverage)} face(s) UNSCANNED or lapsed. "
                "UNSCANNED is unknown, never safe: nobody has flown those walls, and a "
                "route across one is priced as unknown rather than treated as clear"
            ),
            refs=tuple(str(report.face) for report in missing),
        )
    hottest = max(report.peak_c or 0.0 for report in coverage)
    return Criterion(
        criterion_id="thermal.coverage",
        title="Every face has current thermal coverage",
        passed=True,
        reason=(
            f"all {len(coverage)} face(s) carry a current frame; hottest measured peak "
            f"{hottest:.0f} C surface temperature"
        ),
        refs=tuple(str(report.face) for report in coverage),
    )


def _hazard_criterion(facts: Mapping[CanonicalKey, StructuralFact]) -> Criterion:
    resolved: list[str] = []
    outstanding: list[str] = []
    for key in HAZARD_KEYS:
        fact = facts.get(key)
        if fact is not None and fact.value.is_known:
            resolved.append(fact.fact_id)
        else:
            outstanding.append(key)
    if outstanding:
        return Criterion(
            criterion_id="hazard.resolved",
            title="Hazard attributes are resolved or stated outstanding",
            passed=False,
            reason=(
                f"{len(outstanding)} of {len(HAZARD_KEYS)} hazard attribute(s) have no known "
                "value on this profile; they are outstanding rather than absent, and an "
                "entry plan cannot answer for them"
            ),
            refs=tuple(outstanding),
        )
    return Criterion(
        criterion_id="hazard.resolved",
        title="Hazard attributes are resolved or stated outstanding",
        passed=True,
        reason=f"all {len(HAZARD_KEYS)} hazard attribute(s) carry a known value on the profile",
        refs=tuple(resolved),
    )


def _conflict_criterion(conflicts: Sequence[Conflict]) -> Criterion:
    open_load_bearing = [
        c
        for c in conflicts
        if c.status is ConflictStatus.OPEN and c.canonical_key in LOAD_BEARING_KEYS
    ]
    if open_load_bearing:
        ordered = sorted(open_load_bearing, key=lambda c: (-c.severity, c.conflict_id))
        return Criterion(
            criterion_id="conflicts.load-bearing",
            title="No open conflict on a load-bearing attribute",
            passed=False,
            reason=(
                f"{len(ordered)} open disagreement(s) on attributes that change what an "
                "entry looks like; only somebody standing at the building can settle one, "
                "and both original records stay whichever way it goes"
            ),
            refs=tuple(ref for c in ordered for ref in (c.conflict_id, c.canonical_key))[:12],
        )
    return Criterion(
        criterion_id="conflicts.load-bearing",
        title="No open conflict on a load-bearing attribute",
        passed=True,
        reason=(
            f"{len(conflicts)} conflict(s) on file for this address, none of them open on a "
            "load-bearing attribute"
        ),
        refs=tuple(c.conflict_id for c in conflicts)[:12],
    )


def _freshness_criterion(snapshot: ProfileSnapshot, now: datetime) -> Criterion:
    age = now - snapshot.read_at
    seconds = age.total_seconds()
    if seconds < 0.0 or age > MAX_SNAPSHOT_AGE:
        return Criterion(
            criterion_id="snapshot.fresh",
            title="The profile snapshot is current",
            passed=False,
            reason=(
                f"the snapshot this incident opened against was read {seconds:.0f} s ago, "
                f"outside the {MAX_SNAPSHOT_AGE.total_seconds():.0f} s window. The snapshot "
                "is frozen at dispatch on purpose; an old one means this session has been "
                "open a long time, not that the building changed"
            ),
            refs=(snapshot.snapshot_id, f"profile-version-{snapshot.profile_version}"),
        )
    return Criterion(
        criterion_id="snapshot.fresh",
        title="The profile snapshot is current",
        passed=True,
        reason=(
            f"read {seconds:.0f} s ago at profile version {snapshot.profile_version}, "
            f"inside the {MAX_SNAPSHOT_AGE.total_seconds():.0f} s window"
        ),
        refs=(snapshot.snapshot_id, f"profile-version-{snapshot.profile_version}"),
    )


def _intake_criterion(reported_keys: Sequence[str], narratives_read: int) -> Criterion:
    bound = sorted(set(reported_keys) & ACCESS_INTAKE_KEYS)
    if not narratives_read:
        return Criterion(
            criterion_id="intake.access-bound",
            title="A narrative bound an occupancy or access attribute",
            passed=False,
            reason=(
                "no 911 or CAD narrative has been read on this incident, so nothing a "
                "caller said about who is inside or how to get in has reached the record"
            ),
        )
    if not bound:
        return Criterion(
            criterion_id="intake.access-bound",
            title="A narrative bound an occupancy or access attribute",
            passed=False,
            reason=(
                f"{narratives_read} narrative(s) were read and none of them bound an "
                "occupancy or access attribute to a span in the transcript"
            ),
            refs=tuple(sorted(set(reported_keys)))[:12],
        )
    return Criterion(
        criterion_id="intake.access-bound",
        title="A narrative bound an occupancy or access attribute",
        passed=True,
        reason=(
            f"{narratives_read} narrative(s) read; {len(bound)} occupancy or access "
            "attribute(s) bound to spans. Reported, never observed: nothing here is a "
            "structural fact and none of it sorts against a filed record"
        ),
        refs=tuple(bound),
    )


def assess(
    *,
    incident_id: str,
    snapshot: ProfileSnapshot,
    coverage: Sequence[FaceCoverage],
    now: datetime,
    reported_keys: Sequence[str] = (),
    narratives_read: int = 0,
    assessed_by: str,
    assessed_by_version: str = "1.0.0",
) -> ReadinessAssessment:
    """Evaluate every criterion. Total, deterministic, and never partial.

    Order is fixed so two assessments of the same record are byte-identical, and
    so the card reads outward: the structure first, then what was measured on
    it, then what disagrees about it, then how old the read is, then what the
    caller said.
    """
    return ReadinessAssessment(
        incident_id=incident_id,
        address_id=snapshot.address_id,
        assessed_at=now,
        assessed_by=assessed_by,
        assessed_by_version=assessed_by_version,
        profile_snapshot_id=snapshot.snapshot_id,
        criteria=(
            _geometry_criterion(snapshot),
            _thermal_criterion(coverage),
            _hazard_criterion(snapshot.facts),
            _conflict_criterion(snapshot.conflicts),
            _freshness_criterion(snapshot, now),
            _intake_criterion(reported_keys, narratives_read),
        ),
    )


__all__ = [
    "ACCESS_INTAKE_KEYS",
    "HAZARD_KEYS",
    "MAX_SNAPSHOT_AGE",
    "Criterion",
    "ReadinessAssessment",
    "assess",
]
