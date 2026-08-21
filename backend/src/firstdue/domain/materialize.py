"""Materializing a profile from the facts it already holds.

Everything a profile *derives* -- its conflicts, its decayed confidence -- is a
pure function of its facts and one timestamp. This module is that function.

Keeping it pure buys the property the whole event architecture rests on:
**replaying identical events produces equivalent materialized state.** Events
carry identifiers, consumers re-read the store and call this, and because
conflict ids are derived and decay is deterministic, the second pass over the
same facts changes nothing at all -- not the conflicts, not the decay map, not
the version. Idempotency is a consequence of the maths rather than a flag
somebody has to remember to check.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflict_engine import RuleRegistry, detect, new_conflicts
from firstdue.domain.conflicts import Conflict
from firstdue.domain.decay import decayed_confidence
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType

#: Decay is rounded so that two runs cannot differ in the sixteenth decimal
#: place and call that a change.
DECAY_PRECISION: Final[int] = 6


def timeline_event_id(address_id: str, discriminator: str) -> str:
    """A derived timeline-event id.

    Derived rather than minted so a replay produces the same event id, which is
    what lets a second delivery be recognised as a duplicate instead of
    appearing as a second thing that happened.
    """
    digest = hashlib.sha256(f"{address_id}|{discriminator}".encode()).hexdigest()[:16]
    return f"pevt_{digest}"


class Materialization(BaseModel):
    """The result of one materialization pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: BuildingProfile
    #: Conflicts detected on this pass that were not already recorded.
    new_conflicts: tuple[Conflict, ...] = ()
    decay: dict[CanonicalKey, float] = Field(default_factory=dict)
    #: False when the pass was a no-op -- the normal outcome of a replay.
    changed: bool = False

    @property
    def content_hash(self) -> str:
        return self.profile.content_hash


def recompute_decay(profile: BuildingProfile, *, now: datetime) -> dict[CanonicalKey, float]:
    """Decayed confidence for every resolved attribute.

    Age, source authority, and intervening churn, applied by
    :mod:`firstdue.domain.decay`. Computed over the *resolved* fact per
    attribute, because that is the one the brief renders -- a decayed number
    attached to a fact nobody displays would mislead.
    """
    return {
        key: round(
            decayed_confidence(
                fact,
                now=now,
                events_since_observation=profile.events_after(fact.observed_at),
            ),
            DECAY_PRECISION,
        )
        for key, fact in profile.facts.items()
    }


def materialize(
    profile: BuildingProfile,
    *,
    now: datetime,
    actor: str,
    actor_version: str | None = None,
    registry: RuleRegistry | None = None,
) -> Materialization:
    """Recompute conflicts and decay for one profile.

    Args:
        profile: the profile as stored.
        now: evaluation time. Passed in, never read from a global clock.
        actor: the agent recording the derived findings on the timeline.
        actor_version: the pinned version of that agent, recorded for replay.
        registry: the rule set to run. Defaults to the process registry.

    Returns:
        The updated profile plus what changed. ``changed=False`` means the pass
        was a no-op, which is the expected result of a redelivered event.
    """
    updated = profile
    findings = detect(profile.address_id, profile.all_facts(), now=now, registry=registry)
    fresh = new_conflicts(findings, profile.conflicts, detected_at=now)

    for conflict in fresh:
        updated = updated.with_conflict(
            conflict,
            event=ProfileEvent(
                event_id=timeline_event_id(profile.address_id, conflict.conflict_id),
                sequence=updated.next_sequence,
                occurred_at=now,
                type=ProfileEventType.CONFLICT_DETECTED,
                actor=actor,
                actor_version=actor_version,
                summary=conflict.summary,
                canonical_keys=(conflict.canonical_key,),
                fact_ids=conflict.fact_ids,
                conflict_id=conflict.conflict_id,
            ),
        )

    decay = recompute_decay(updated, now=now)
    if decay != updated.confidence_decay:
        updated = updated.with_decay(decay)

    return Materialization(
        profile=updated,
        new_conflicts=fresh,
        decay=decay,
        changed=updated.profile_version != profile.profile_version,
    )
