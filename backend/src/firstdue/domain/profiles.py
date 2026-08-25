"""Building profiles -- the memory that makes the instant brief possible.

A profile accumulates over years. Two properties matter more than anything else
in this module:

* the **timeline is append-only** -- events are added with a gapless, monotonic
  sequence and nothing is ever edited or removed;
* profiles use **optimistic concurrency** on ``profile_version`` -- a write
  against a stale version is rejected, and the API renders that as HTTP 409.

The profile is also the entire interface between the slow loop and the incident
loop: the incident loop reads one :class:`ProfileSnapshot` and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.conflicts import Conflict
from firstdue.domain.facts import StructuralFact
from firstdue.domain.factsets import FactSet
from firstdue.domain.geometry import GeometrySpec
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.work import ReferralRecord
from firstdue.errors import AppendOnlyViolationError, StaleVersionError, ValidationError


class ProfileEventType(StrEnum):
    FACT_WRITTEN = "FACT_WRITTEN"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
    GEOMETRY_UPDATED = "GEOMETRY_UPDATED"
    SURVEY_COMPLETED = "SURVEY_COMPLETED"
    REFERRAL_DRAFTED = "REFERRAL_DRAFTED"
    REFERRAL_FILED = "REFERRAL_FILED"
    WORK_ORDER_DISPATCHED = "WORK_ORDER_DISPATCHED"
    INCIDENT_OPENED = "INCIDENT_OPENED"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"


class ProfileEvent(BaseModel):
    """One immutable entry on a profile's timeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=0, description="gapless, monotonic within a profile")
    occurred_at: datetime
    type: ProfileEventType

    #: Who did it -- an agent id or a human identifier. Never empty.
    actor: str = Field(min_length=1, max_length=120)
    actor_version: str | None = Field(default=None, max_length=40)

    summary: str = Field(min_length=1, max_length=500)
    canonical_keys: tuple[CanonicalKey, ...] = ()
    fact_ids: tuple[str, ...] = ()
    conflict_id: str | None = Field(default=None, max_length=120)
    correlation_id: str | None = Field(default=None, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)


class BuildingProfile(BaseModel):
    """Everything FIRST DUE knows about one address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    #: Optimistic concurrency token. Every accepted write increments it by one.
    profile_version: int = Field(default=0, ge=0)

    #: Every fact ever written, grouped by attribute. Conflicting facts both stay.
    fact_sets: dict[CanonicalKey, FactSet] = Field(default_factory=dict)
    conflicts: tuple[Conflict, ...] = ()
    timeline: tuple[ProfileEvent, ...] = ()

    geometry: GeometrySpec | None = None
    hydrant_ids: tuple[str, ...] = ()
    exposure_address_ids: tuple[str, ...] = ()
    open_referrals: tuple[ReferralRecord, ...] = ()
    incident_history: tuple[str, ...] = ()

    last_human_survey: datetime | None = None
    #: Deterministic decay values, recomputed by the slow loop, keyed by attribute.
    confidence_decay: dict[CanonicalKey, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_timeline_is_append_only(self) -> Self:
        """The timeline must be gapless and monotonic from zero."""
        for index, event in enumerate(self.timeline):
            if event.sequence != index:
                raise AppendOnlyViolationError(
                    "profile timeline sequence must be gapless and monotonic",
                    details={"expected": index, "found": event.sequence},
                )
        for key, fact_set in self.fact_sets.items():
            if fact_set.canonical_key != key:
                raise ValidationError(
                    "fact set is filed under the wrong canonical key",
                    details={"key": key, "fact_set_key": fact_set.canonical_key},
                )
            if fact_set.address_id != self.address_id:
                raise ValidationError(
                    "fact set belongs to a different address",
                    details={"key": key},
                )
        return self

    # ------------------------------------------------------------- read side

    @property
    def facts(self) -> dict[CanonicalKey, StructuralFact]:
        """The resolved view: one winning fact per attribute.

        This is what the brief renders. The losing facts are still in
        ``fact_sets`` -- resolution is a display decision, not a deletion.
        """
        resolved: dict[CanonicalKey, StructuralFact] = {}
        for key, fact_set in self.fact_sets.items():
            winner = fact_set.resolved
            if winner is not None:
                resolved[key] = winner
        return resolved

    @property
    def open_conflicts(self) -> tuple[Conflict, ...]:
        """Every finding still unresolved. The record, not the reading."""
        from firstdue.domain.conflicts import ConflictStatus

        return tuple(c for c in self.conflicts if c.status is ConflictStatus.OPEN)

    @property
    def current_conflicts(self) -> tuple[Conflict, ...]:
        """The open findings that describe the disagreement as it stands now.

        A rule re-fires on every pass, and a conflict's id is derived from the
        facts it cited -- so an amended permit or a fresh flight mints a *new*
        finding while the earlier one, about a pairing nothing compares any
        more, stays OPEN. Three findings then describe one disagreement, and a
        console listing all three tells an officer this building has three
        problems. It has one, seen three times.

        Nothing is closed or deleted here. Only a human observation may RESOLVE
        a conflict and none of these were resolved, so every finding stays in
        :attr:`conflicts` where an investigator can still read it. This is which
        one is *live*, per rule and per attribute: the most recently detected,
        with the id breaking a tie so two passes landing on the same instant
        cannot reorder the answer between reads.

        Severity first, then id -- the order a crew would work them.
        """
        newest: dict[tuple[str, str], Conflict] = {}
        for conflict in self.open_conflicts:
            key = (conflict.rule_id, str(conflict.canonical_key))
            seen = newest.get(key)
            if seen is None or (conflict.detected_at, conflict.conflict_id) > (
                seen.detected_at,
                seen.conflict_id,
            ):
                newest[key] = conflict
        return tuple(sorted(newest.values(), key=lambda c: (-c.severity, c.conflict_id)))

    @property
    def next_sequence(self) -> int:
        return len(self.timeline)

    def all_facts(self) -> tuple[StructuralFact, ...]:
        return tuple(f for fs in self.fact_sets.values() for f in fs.facts)

    def events_after(self, moment: datetime) -> int:
        """How many timeline events happened after ``moment`` -- feeds decay."""
        return sum(1 for e in self.timeline if e.occurred_at > moment)

    # ------------------------------------------------------------ write side

    def check_version(self, expected_version: int) -> None:
        """Raise :class:`StaleVersionError` if ``expected_version`` is not current."""
        if expected_version != self.profile_version:
            raise StaleVersionError(
                expected=expected_version,
                actual=self.profile_version,
                entity=f"profile {self.address_id}",
            )

    def append_event(self, event: ProfileEvent) -> BuildingProfile:
        """Append one timeline event and bump the version. Never edits history."""
        if event.sequence != self.next_sequence:
            raise AppendOnlyViolationError(
                "profile timeline events must be appended in sequence",
                details={"expected": self.next_sequence, "found": event.sequence},
            )
        return self.model_copy(
            update={
                "timeline": (*self.timeline, event),
                "profile_version": self.profile_version + 1,
            }
        )

    def with_fact(self, fact: StructuralFact, *, event: ProfileEvent) -> BuildingProfile:
        """Record a fact and its timeline event as one versioned change."""
        if fact.address_id != self.address_id:
            raise ValidationError(
                "fact belongs to a different address", details={"fact_id": fact.fact_id}
            )
        key = fact.canonical_key
        existing = self.fact_sets.get(key)
        updated = existing.append(fact) if existing is not None else FactSet.of(fact)
        next_sets = {**self.fact_sets, key: updated}
        return self.model_copy(update={"fact_sets": next_sets}).append_event(event)

    def with_conflict(self, conflict: Conflict, *, event: ProfileEvent) -> BuildingProfile:
        if any(c.conflict_id == conflict.conflict_id for c in self.conflicts):
            raise AppendOnlyViolationError(
                "conflict already recorded on this profile",
                details={"conflict_id": conflict.conflict_id},
            )
        return self.model_copy(update={"conflicts": (*self.conflicts, conflict)}).append_event(
            event
        )

    def with_geometry(self, geometry: GeometrySpec, *, event: ProfileEvent) -> BuildingProfile:
        if geometry.address_id != self.address_id:
            raise ValidationError("geometry spec belongs to a different address")
        return self.model_copy(update={"geometry": geometry}).append_event(event)

    # ------------------------------------------------------------- snapshot

    @property
    def content_hash(self) -> str:
        """Canonical hash of everything this profile asserts."""
        from firstdue.domain.snapshots import profile_content_hash

        return profile_content_hash(self)

    def with_decay(self, decay: dict[CanonicalKey, float]) -> BuildingProfile:
        """Replace the recomputed decay map.

        No timeline event: decay is a pure function of the facts and the clock,
        so recording each recomputation would fill the timeline with entries
        that assert nothing new about the building. The version *does* advance,
        because the version is the optimistic-concurrency token for the stored
        document and this is a write to it -- an unversioned write is a write
        two instances can lose.
        """
        return self.model_copy(
            update={
                "confidence_decay": dict(decay),
                "profile_version": self.profile_version + 1,
            }
        )

    def snapshot(self, *, read_at: datetime, snapshot_id: str | None = None) -> ProfileSnapshot:
        """Freeze the profile for one incident.

        The snapshot id is recorded on the incident so the brief replays
        byte-identically during an investigation. Omitting ``snapshot_id``
        derives the stable one for this profile version, which is what makes
        re-snapshotting idempotent rather than a second, subtly different read.
        """
        from firstdue.domain.decay import staleness
        from firstdue.domain.snapshots import snapshot_id_for

        resolved_id = snapshot_id if snapshot_id is not None else snapshot_id_for(self)
        staleness_by_key: dict[CanonicalKey, float] = {}
        for key, fact in self.facts.items():
            staleness_by_key[key] = round(
                staleness(
                    fact,
                    now=read_at,
                    events_since_observation=self.events_after(fact.observed_at),
                ),
                6,
            )

        return ProfileSnapshot(
            address_id=self.address_id,
            district_id=self.district_id,
            profile_version=self.profile_version,
            snapshot_id=resolved_id,
            read_at=read_at,
            facts=self.facts,
            conflicts=self.current_conflicts,
            geometry=self.geometry,
            hydrant_ids=self.hydrant_ids,
            exposure_address_ids=self.exposure_address_ids,
            open_referral_ids=tuple(r.referral_id for r in self.open_referrals),
            staleness=staleness_by_key,
            last_human_survey=self.last_human_survey,
        )


class ProfileSnapshot(BaseModel):
    """The entire interface between the slow loop and the incident loop.

    Read once at incident start. If no profile exists -- new construction,
    outside district -- the incident loop degrades to live queries and the brief
    states that no pre-incident profile exists. That is the honest failure mode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    profile_version: int = Field(ge=0)
    #: Recorded on the incident for exact replay.
    snapshot_id: str = Field(min_length=1, max_length=120)
    read_at: datetime

    facts: dict[CanonicalKey, StructuralFact] = Field(default_factory=dict)
    conflicts: tuple[Conflict, ...] = ()
    geometry: GeometrySpec | None = None
    hydrant_ids: tuple[str, ...] = ()
    exposure_address_ids: tuple[str, ...] = ()
    open_referral_ids: tuple[str, ...] = ()
    staleness: dict[CanonicalKey, float] = Field(default_factory=dict)
    last_human_survey: datetime | None = None

    @property
    def is_cold_start(self) -> bool:
        """True when there is no accumulated knowledge to brief from."""
        return not self.facts and self.geometry is None
