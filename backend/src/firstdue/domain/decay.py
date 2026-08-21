"""Deterministic confidence decay.

A structure surveyed 14 months ago with two permits pulled since is stale, and
the profile must say so rather than presenting old certainty as current fact.

Every input is explicit and every output is reproducible: no clock reads inside,
no randomness, no model. Same inputs, same number, forever -- which is what a
NIOSH replay two years later requires.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from firstdue.domain.enums import SourceTier
from firstdue.domain.facts import StructuralFact

#: Half-life in days per tier. A live observation decays fast because it
#: describes a moment; an assessor record decays slowly because it describes
#: a filing that stays true until amended.
HALF_LIFE_DAYS: Final[dict[SourceTier, float]] = {
    SourceTier.LIVE_OBSERVATION: 0.25,
    SourceTier.HUMAN_VERIFIED: 540.0,
    SourceTier.AUTHORITATIVE_RECORD: 1825.0,
    SourceTier.REMOTE_MEASUREMENT: 730.0,
    SourceTier.DERIVED_INFERENCE: 365.0,
}

#: Authority weight per tier -- a ceiling on confidence regardless of age.
AUTHORITY_WEIGHT: Final[dict[SourceTier, float]] = {
    SourceTier.LIVE_OBSERVATION: 1.00,
    SourceTier.HUMAN_VERIFIED: 1.00,
    SourceTier.AUTHORITATIVE_RECORD: 0.95,
    SourceTier.REMOTE_MEASUREMENT: 0.85,
    SourceTier.DERIVED_INFERENCE: 0.70,
}

#: Each intervening event (permit pulled, violation filed) since observation
#: costs this much confidence, floored so it never reaches zero.
EVENT_PENALTY: Final[float] = 0.15
EVENT_PENALTY_FLOOR: Final[float] = 0.40

SECONDS_PER_DAY: Final[float] = 86_400.0


def decayed_confidence(
    fact: StructuralFact,
    *,
    now: datetime,
    events_since_observation: int = 0,
) -> float:
    """Return the fact's confidence discounted for age, authority, and churn.

    Args:
        fact: the fact being aged.
        now: evaluation time (passed in, never read from a global clock).
        events_since_observation: count of profile events recorded after
            ``fact.observed_at`` -- permits, violations, surveys.

    Returns:
        A value in ``[0.0, 1.0]``.
    """
    if events_since_observation < 0:
        raise ValueError("events_since_observation cannot be negative")

    tier = fact.tier
    age_days = fact.age_seconds(now) / SECONDS_PER_DAY
    half_life = HALF_LIFE_DAYS[tier]
    age_factor = 0.5 ** (age_days / half_life)
    event_factor = max(EVENT_PENALTY_FLOOR, 1.0 - (EVENT_PENALTY * float(events_since_observation)))
    raw: float = fact.confidence * AUTHORITY_WEIGHT[tier] * age_factor * event_factor
    return max(0.0, min(1.0, raw))


def staleness(
    fact: StructuralFact,
    *,
    now: datetime,
    events_since_observation: int = 0,
) -> float:
    """``1 - decayed_confidence``. Higher means more in need of a survey."""
    return 1.0 - decayed_confidence(
        fact, now=now, events_since_observation=events_since_observation
    )
