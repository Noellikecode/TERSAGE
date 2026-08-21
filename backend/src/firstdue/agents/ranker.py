"""Delta Ranker -- which building a company should survey next, and why.

A district has 3,800 structures and a company has one morning. Ranking is
therefore the product's actual output for most of its life, and two properties
make it usable rather than merely present:

* **It is deterministic.** Four signals, fixed weights, arithmetic in this file
  and nowhere else. The same profiles produce the same order on every run, so a
  chief who disagrees with row three can be shown exactly what produced it.
* **Every row cites its reasons.** A queue entry cannot be constructed without
  at least one :class:`RankReason`, and each reason names the rule that fired,
  the facts behind it, and its weight. "Because the model said so" is not
  expressible here.

The four signals, and why each is on the list:

| Signal | Weight | Why it means "go look" |
|---|---|---|
| Open conflict severity | 0.40 | Two sources disagree; only a person can settle it |
| Confidence decay | 0.25 | What is on file has aged past the point of being relied on |
| Source churn | 0.20 | Permits and violations filed since anyone last looked |
| Survey age | 0.15 | Nobody has stood in the building in a long time |

No model participates. Nothing here predicts anything about a fire.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.materialize import recompute_decay
from firstdue.domain.profiles import BuildingProfile, ProfileEventType
from firstdue.domain.work import QueueEntryStatus, RankReason, SurveyQueueEntry
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.repositories import ProfileRepository, QueueRepository

logger = get_logger(__name__)

AGENT_ID: Final[str] = "survey-ranker"

WEIGHT_CONFLICT: Final[float] = 0.40
WEIGHT_DECAY: Final[float] = 0.25
WEIGHT_CHURN: Final[float] = 0.20
WEIGHT_SURVEY_AGE: Final[float] = 0.15

#: A survey older than this counts as fully aged. Two years is the interval
#: most departments target for a company-level survey.
SURVEY_AGE_CEILING_DAYS: Final[float] = 730.0
#: Filings since the last survey, beyond which churn is already maximal.
CHURN_CEILING: Final[int] = 5
#: Rows below this score are not worth a company's morning.
MIN_SCORE: Final[float] = 0.05

RULE_CONFLICT: Final[str] = "rank.open-conflict-severity"
RULE_DECAY: Final[str] = "rank.confidence-decay"
RULE_CHURN: Final[str] = "rank.source-churn"
RULE_SURVEY_AGE: Final[str] = "rank.survey-age"
RULE_NEVER_SURVEYED: Final[str] = "rank.never-surveyed"


class RankedQueue(BaseModel):
    """One district's ranking, as produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    entries: tuple[SurveyQueueEntry, ...] = ()
    #: Profiles considered but scored below the floor.
    skipped: int = Field(default=0, ge=0)

    @property
    def top_address_id(self) -> str | None:
        return self.entries[0].address_id if self.entries else None


def _conflict_signal(profile: BuildingProfile) -> tuple[float, list[RankReason]]:
    """Severity of the worst open conflict, normalised to [0, 1]."""
    open_conflicts = [c for c in profile.conflicts if c.status is ConflictStatus.OPEN]
    if not open_conflicts:
        return 0.0, []
    worst = max(open_conflicts, key=lambda c: c.severity)
    score = worst.severity / 5.0
    return score, [
        RankReason(
            rule_id=RULE_CONFLICT,
            canonical_key=worst.canonical_key,
            detail=f"Severity {worst.severity} conflict open: {worst.summary}",
            weight=round(score, 4),
            fact_ids=worst.fact_ids,
            conflict_id=worst.conflict_id,
        )
    ]


def _decay_signal(profile: BuildingProfile, *, now: datetime) -> tuple[float, list[RankReason]]:
    """How stale the profile's resolved facts have become."""
    decay = profile.confidence_decay or recompute_decay(profile, now=now)
    if not decay:
        return 0.0, []
    worst_key = min(decay, key=lambda key: decay[key])
    staleness = 1.0 - decay[worst_key]
    if staleness <= 0.0:
        return 0.0, []
    return staleness, [
        RankReason(
            rule_id=RULE_DECAY,
            canonical_key=worst_key,
            detail=(
                f"Confidence in {worst_key} has decayed to "
                f"{decay[worst_key]:.2f} of its filed value"
            ),
            weight=round(staleness, 4),
        )
    ]


def _churn_signal(profile: BuildingProfile) -> tuple[float, list[RankReason]]:
    """Filings recorded since the last human survey."""
    since = profile.last_human_survey
    filings = [
        event
        for event in profile.timeline
        if event.type is ProfileEventType.FACT_WRITTEN
        and (since is None or event.occurred_at > since)
    ]
    if not filings:
        return 0.0, []
    score = min(1.0, len(filings) / CHURN_CEILING)
    return score, [
        RankReason(
            rule_id=RULE_CHURN,
            detail=f"{len(filings)} source changes recorded since the last survey",
            weight=round(score, 4),
        )
    ]


def _survey_age_signal(
    profile: BuildingProfile, *, now: datetime
) -> tuple[float, list[RankReason]]:
    """How long since a person last stood in the building."""
    if profile.last_human_survey is None:
        return 1.0, [
            RankReason(
                rule_id=RULE_NEVER_SURVEYED,
                detail="No company survey on record for this structure",
                weight=1.0,
            )
        ]
    age_days = max(0.0, (now - profile.last_human_survey).total_seconds() / 86_400.0)
    score = min(1.0, age_days / SURVEY_AGE_CEILING_DAYS)
    if score <= 0.0:
        return 0.0, []
    return score, [
        RankReason(
            rule_id=RULE_SURVEY_AGE,
            detail=f"Last surveyed {int(age_days)} days ago",
            weight=round(score, 4),
        )
    ]


def score_profile(
    profile: BuildingProfile, *, now: datetime
) -> tuple[float, tuple[RankReason, ...]]:
    """The deterministic score for one structure, with its reasons.

    Weighted sum of four signals, each already normalised to ``[0, 1]``. The
    weights sum to one, so the score is directly comparable across districts.
    """
    conflict, conflict_reasons = _conflict_signal(profile)
    decay, decay_reasons = _decay_signal(profile, now=now)
    churn, churn_reasons = _churn_signal(profile)
    survey_age, age_reasons = _survey_age_signal(profile, now=now)

    score = (
        WEIGHT_CONFLICT * conflict
        + WEIGHT_DECAY * decay
        + WEIGHT_CHURN * churn
        + WEIGHT_SURVEY_AGE * survey_age
    )
    reasons = tuple(conflict_reasons + decay_reasons + churn_reasons + age_reasons)
    return round(min(1.0, max(0.0, score)), 6), reasons


class DeltaRanker:
    """Ranks a district's structures for physical survey."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        queue: QueueRepository,
        clock: Clock,
        agent_version: str = "1.0.0",
        min_score: float = MIN_SCORE,
    ) -> None:
        self._profiles = profiles
        self._queue = queue
        self._clock = clock
        self._agent_version = agent_version
        self._min_score = min_score

    async def rank(self, district_id: str) -> RankedQueue:
        """Recompute a district's queue wholesale.

        Ranking is recomputed rather than patched: a queue that was partly
        updated would be a ranking of two different moments, and the order is
        the whole product.
        """
        now = self._clock.now()
        profiles = await self._profiles.list_by_district(district_id)

        scored: list[tuple[float, BuildingProfile, tuple[RankReason, ...]]] = []
        skipped = 0
        for profile in profiles:
            score, reasons = score_profile(profile, now=now)
            if score < self._min_score or not reasons:
                skipped += 1
                continue
            scored.append((score, profile, reasons))

        # Descending score; address id breaks ties so two processes agree.
        scored.sort(key=lambda item: (-item[0], item[1].address_id))

        entries = tuple(
            SurveyQueueEntry(
                # Derived from the district and address: re-ranking replaces the
                # row rather than accumulating a second one for one building.
                entry_id=f"queue_{district_id}_{profile.address_id}",
                address_id=profile.address_id,
                district_id=district_id,
                rank=index,
                score=score,
                reasons=reasons,
                status=QueueEntryStatus.RANKED,
                created_at=now,
                ranked_by_version=self._agent_version,
            )
            for index, (score, profile, reasons) in enumerate(scored, start=1)
        )

        stored = await self._queue.replace_district_queue(district_id, entries)
        logger.info(
            "district_ranked",
            extra={"district_id": district_id, "ranked": len(stored), "skipped": skipped},
        )
        return RankedQueue(district_id=district_id, entries=tuple(stored), skipped=skipped)


def survey_interval_days(entries: Sequence[SurveyQueueEntry]) -> timedelta:
    """How far apart surveys would be if the whole queue were worked evenly."""
    return timedelta(days=max(1, len(entries)))
