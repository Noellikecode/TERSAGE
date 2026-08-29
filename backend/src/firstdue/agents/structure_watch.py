"""Structure Watch -- one reading of a district, one set of conclusions.

This agent supersedes ``conflict-detector`` and ``survey-ranker``. They were
split because detecting a disagreement and deciding whose morning it is worth
are different *kinds* of work. That turned out to be a distinction without a
boundary: ranking read the conflicts detection had just written, on the same
profiles, in the same pass, and neither was ever useful without the other.

**What the merge actually buys is a single reading.** Before, the detector read
a district's profiles, wrote conflicts, and the ranker then re-read the same
profiles to score them. Between those two reads a watcher could append a fact,
so the severity an officer saw and the rank that put the building in front of
him could be answers about two different corpora -- and the queue would say
"severity 4 conflict open" beside a score computed before that conflict existed.
Worse, the ranker's decay signal preferred the ``confidence_decay`` map stored on
the profile, which was whatever an *earlier* pass had computed, while detection
recomputed decay at ``now``. Two numbers, one attribute, different clocks.

Here there is one read and one instant:

* :func:`read_profile` is the only way to obtain a :class:`ProfileReading`, and
  a reading carries the profile, the conflicts detected on it, and the decay map
  computed for it -- all from one call, at one ``now``.
* Every scoring function in this module takes a :class:`ProfileReading` and
  **takes no ``now`` of its own**. There is no signature that lets a caller rank
  a profile that this pass did not just detect against, or rank it as of a
  different moment.
* :class:`ProfileReading` refuses to exist if its decay map is not the one
  stored on its profile, and :class:`DistrictReading` refuses to exist if its
  readings were not all taken at the same instant. Getting either wrong would
  put two structures on one rank scale while their confidence numbers are a
  year apart.

Two properties survive the merge unchanged, because they are the reason anyone
trusts the output:

* **No model participates in detection or ranking.** Conflict rules and rank
  arithmetic are deterministic Python. Gemini may narrate a conflict the engine
  already found, or explain why a row surfaced; it may not create either. A
  model that could invent a conflict could invent its absence.
* **Every row cites its reasons.** A :class:`SurveyQueueEntry` cannot be built
  without at least one :class:`RankReason` naming the rule that fired, the facts
  behind it, and its weight. "Because the model said so" is not expressible.

The four structure signals, and why each means "go look":

| Signal | Weight | Why |
|---|---|---|
| Open conflict severity | 0.40 | Two sources disagree; only a person can settle it |
| Confidence decay | 0.25 | What is on file has aged past being relied on |
| Source churn | 0.20 | Permits and violations filed since anyone last looked |
| Survey age | 0.15 | Nobody has stood in the building in a long time |

The conflict signals are new -- see :func:`score_conflict`. Nothing here
predicts anything about a fire.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflict_engine import LIFE_SAFETY_KEYS, RuleRegistry
from firstdue.domain.conflicts import Conflict
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.materialize import materialize
from firstdue.domain.profiles import BuildingProfile, ProfileEventType
from firstdue.domain.work import QueueEntryStatus, RankReason, SurveyQueueEntry
from firstdue.errors import AppendOnlyViolationError, StaleVersionError, ValidationError
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.bus import EventBus
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import ConflictRepository, ProfileRepository, QueueRepository

logger = get_logger(__name__)

AGENT_ID: Final[str] = "structure-watch"

#: What detection keeps back so the ranking -- this agent's actual decision --
#: is still reachable when a large district has eaten the budget.
#:
#: Both runtimes wrap the handler in ``asyncio.timeout``, so overrunning the
#: catalogued sixty seconds is a *cancellation*: ``watch`` never returns, the
#: pass record at the end of it never executes, and the caller is handed no
#: queue -- which is how one agent running long drew two agents idle. Sized to
#: cover the rank sweep, the wholesale queue replacement, the bus announcement
#: and the record.
_RANK_TAIL_MS: Final[int] = 20_000

#: What the ranked-row lines keep back for the record of the pass itself. One
#: audit append and nothing else, and the last thing standing between a pass
#: that happened and a console that can see it.
_RECORD_TAIL_MS: Final[int] = 5_000

# ------------------------------------------------- structure rank weights

#: Carried over from the Delta Ranker unchanged. They sum to one, so a score is
#: comparable across districts, and changing one silently re-orders every queue
#: in the department -- which is why they live here as named constants and not
#: as literals inside the arithmetic.
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

# ------------------------------------------------ conflict rank weights

#: The engine's own severity dominates, because it is the only one of the four
#: derived from *what the sources actually said*. The other three describe the
#: conflict's situation rather than its content, and a situation should never
#: outvote the finding.
WEIGHT_SEVERITY: Final[float] = 0.50
#: Whether the attribute in dispute is one a crew acts on inside the building.
WEIGHT_LIFE_SAFETY: Final[float] = 0.20
#: How long it has stood open with nobody settling it.
WEIGHT_UNRESOLVED_AGE: Final[float] = 0.20
#: How far confidence in the disputed attribute has decayed.
WEIGHT_ATTRIBUTE_DECAY: Final[float] = 0.10

#: A conflict open longer than this is as stale as this ranking can express.
#: Six months is roughly one budget cycle: past it, "we will get to it" has
#: stopped being true and the disagreement needs a crew rather than a reminder.
UNRESOLVED_AGE_CEILING_DAYS: Final[float] = 180.0

RULE_SEVERITY: Final[str] = "conflict-rank.engine-severity"
RULE_LIFE_SAFETY: Final[str] = "conflict-rank.life-safety-attribute"
RULE_UNRESOLVED_AGE: Final[str] = "conflict-rank.unresolved-age"
RULE_ATTRIBUTE_DECAY: Final[str] = "conflict-rank.attribute-decay"


# -------------------------------------------------------------- the reading


@dataclass(frozen=True, slots=True)
class ProfileReading:
    """One profile as this pass read it, plus everything derived from that read.

    Obtain one from :func:`read_profile` and from nowhere else. Every scoring
    function in this module takes a reading rather than a profile and a clock,
    which is what makes "the severity and the rank describe the same corpus" a
    property of the type signatures instead of a claim in a docstring.
    """

    #: The profile *after* materialization: conflicts appended, decay refreshed.
    profile: BuildingProfile
    #: The version the profile carried when it was read, for the version check.
    base_version: int
    #: The single instant this whole reading is as of.
    read_at: datetime
    #: Conflicts this reading found that were not already recorded.
    new_conflicts: tuple[Conflict, ...]
    #: Decayed confidence per attribute, computed at ``read_at``.
    decay: Mapping[CanonicalKey, float]

    def __post_init__(self) -> None:
        # If the decay map handed to the ranker is not the one materialization
        # stored, the queue can say an attribute is 0.4 stale while the profile
        # an officer opens says 0.9. That is the exact disagreement the merge
        # exists to make impossible, so it is a refusal rather than a warning.
        if dict(self.decay) != self.profile.confidence_decay:
            raise ValidationError(
                "a reading's decay map must be the one stored on its profile",
                details={"address_id": self.profile.address_id},
            )

    @property
    def address_id(self) -> str:
        return self.profile.address_id

    @property
    def open_conflicts(self) -> tuple[Conflict, ...]:
        # The live disagreement per rule and attribute, not every historical
        # finding about it. Ranking all of them ranks one problem three times
        # and puts three identical rows in front of a captain.
        return self.profile.current_conflicts

    @property
    def changed(self) -> bool:
        """Whether this reading derived anything the stored profile lacks."""
        return self.profile.profile_version != self.base_version


@dataclass(frozen=True, slots=True)
class DistrictReading:
    """Every profile in one district, as one pass read them.

    The ordering is by address id, so two workers handed the same district in a
    different repository order produce the same reading.
    """

    district_id: str
    read_at: datetime
    readings: tuple[ProfileReading, ...]

    def __post_init__(self) -> None:
        # Readings taken at different instants would put two structures on one
        # rank scale while their decay numbers are hours or days apart -- the
        # 0.25-weighted signal would then be measuring the clock rather than
        # the building.
        stray = tuple(r.address_id for r in self.readings if r.read_at != self.read_at)
        if stray:
            raise ValidationError(
                "every reading in a district pass must be as of the same instant",
                details={"district_id": self.district_id, "address_ids": stray},
            )


def read_profile(
    profile: BuildingProfile,
    *,
    now: datetime,
    registry: RuleRegistry | None = None,
    agent_version: str | None = None,
) -> ProfileReading:
    """Read one profile once: detect its conflicts and recompute its decay.

    Pure. No clock, no repository, no model -- ``now`` arrives as an argument,
    which is what lets a replay two years later reproduce this exact reading.
    Conflict ids are derived from the rule, the address, the attribute, and the
    participating fact ids, so a second pass over unchanged facts re-derives
    identical ids and :attr:`ProfileReading.new_conflicts` comes back empty.
    """
    result = materialize(
        profile,
        now=now,
        actor=AGENT_ID,
        actor_version=agent_version,
        registry=registry,
    )
    return ProfileReading(
        profile=result.profile,
        base_version=profile.profile_version,
        read_at=now,
        new_conflicts=result.new_conflicts,
        decay=result.decay,
    )


def read_district(
    district_id: str,
    profiles: Sequence[BuildingProfile],
    *,
    now: datetime,
    registry: RuleRegistry | None = None,
    agent_version: str | None = None,
) -> DistrictReading:
    """Read a whole district at one instant. The only entry point to a pass."""
    return DistrictReading(
        district_id=district_id,
        read_at=now,
        readings=tuple(
            read_profile(profile, now=now, registry=registry, agent_version=agent_version)
            for profile in sorted(profiles, key=lambda p: p.address_id)
        ),
    )


# ------------------------------------------------------ ranking conflicts


class RankedConflict(BaseModel):
    """One open conflict, ranked against every other open conflict nearby.

    A district's queue answers "which building next". This answers "which
    disagreement next", which is a different question with a different consumer:
    the referral clerk works this list, and a captain reading it is deciding
    which accusation is worth their signature. Ranking structures alone left
    that ordering to whoever happened to scroll first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey
    #: The deterministic rule that produced the conflict, carried through.
    rule_id: str = Field(min_length=1, max_length=120)
    severity: int = Field(ge=1, le=5)

    rank: int = Field(ge=1)
    importance: float = Field(ge=0.0, le=1.0)
    #: At least one reason. A ranked conflict with no reason cannot exist, for
    #: the same reason a queue row with no reason cannot.
    reasons: tuple[RankReason, ...] = Field(min_length=1)
    #: The engine's deterministic template text. Never a model's.
    summary: str = Field(min_length=1, max_length=500)


def _severity_signal(conflict: Conflict) -> tuple[float, list[RankReason]]:
    """The engine's own reading of how bad the disagreement is."""
    score = conflict.severity / 5.0
    return score, [
        RankReason(
            rule_id=RULE_SEVERITY,
            canonical_key=conflict.canonical_key,
            detail=f"Rule {conflict.rule_id} scored this disagreement severity {conflict.severity}",
            weight=round(score, 4),
            fact_ids=conflict.fact_ids,
            conflict_id=conflict.conflict_id,
        )
    ]


def _life_safety_signal(conflict: Conflict) -> tuple[float, list[RankReason]]:
    """Whether a crew acts on this attribute inside the building.

    A disagreement about the number of stairwells is settled at 03:00 by someone
    who has to find the second one. A disagreement about the year of
    construction is settled by a clerk. Both are conflicts; only one of them
    changes what happens on the fireground.
    """
    if conflict.canonical_key not in LIFE_SAFETY_KEYS:
        return 0.0, []
    return 1.0, [
        RankReason(
            rule_id=RULE_LIFE_SAFETY,
            canonical_key=conflict.canonical_key,
            detail=f"{conflict.canonical_key} is an attribute a crew acts on inside the building",
            weight=1.0,
            fact_ids=conflict.fact_ids,
            conflict_id=conflict.conflict_id,
        )
    ]


def _unresolved_age_signal(conflict: Conflict, *, now: datetime) -> tuple[float, list[RankReason]]:
    """How long this has stood open with nobody settling it.

    Only a human observation closes a conflict, so an old open conflict is not
    a backlog entry that will clear itself -- it is a survey that keeps not
    happening. The age is what turns that from a fact into a priority.
    """
    age_days = max(0.0, (now - conflict.detected_at).total_seconds() / 86_400.0)
    score = min(1.0, age_days / UNRESOLVED_AGE_CEILING_DAYS)
    if score <= 0.0:
        return 0.0, []
    return score, [
        RankReason(
            rule_id=RULE_UNRESOLVED_AGE,
            canonical_key=conflict.canonical_key,
            detail=f"Open and unsettled for {int(age_days)} days",
            weight=round(score, 4),
            conflict_id=conflict.conflict_id,
        )
    ]


def _attribute_decay_signal(
    conflict: Conflict, *, decay: Mapping[CanonicalKey, float]
) -> tuple[float, list[RankReason]]:
    """How far confidence in the disputed attribute has decayed.

    This is the signal that turns a paperwork dispute into a site visit. While
    the file is still trusted, a clerk can arbitrate a disagreement by reading
    it. Once confidence in the attribute has decayed, nothing on file can settle
    it any more and the only remaining arbitrator is a person in the building.

    The map is the one this pass computed, not whatever a previous pass left on
    the profile -- see :class:`ProfileReading`.
    """
    remaining = decay.get(conflict.canonical_key)
    if remaining is None:
        return 0.0, []
    staleness = 1.0 - remaining
    if staleness <= 0.0:
        return 0.0, []
    return staleness, [
        RankReason(
            rule_id=RULE_ATTRIBUTE_DECAY,
            canonical_key=conflict.canonical_key,
            detail=(
                f"Confidence in the disputed {conflict.canonical_key} has decayed to "
                f"{remaining:.2f}; the file can no longer settle it"
            ),
            weight=round(staleness, 4),
            conflict_id=conflict.conflict_id,
        )
    ]


def score_conflict(
    conflict: Conflict, reading: ProfileReading
) -> tuple[float, tuple[RankReason, ...]]:
    """How much one open conflict matters, with its reasons.

    Weighted sum of four signals, each normalised to ``[0, 1]``, weights summing
    to one. The conflict is scored against ``reading`` rather than against a
    repository lookup so its decay term comes from the same pass that computed
    its severity: importance and severity cannot describe different corpora.

    A model has no input here. Importance decides whose disagreement a captain
    looks at first, which is an allocation of a person's attention, and the
    reasons a person is given for that had better be re-derivable by hand.
    """
    severity, severity_reasons = _severity_signal(conflict)
    life_safety, life_safety_reasons = _life_safety_signal(conflict)
    age, age_reasons = _unresolved_age_signal(conflict, now=reading.read_at)
    decay, decay_reasons = _attribute_decay_signal(conflict, decay=reading.decay)

    importance = (
        WEIGHT_SEVERITY * severity
        + WEIGHT_LIFE_SAFETY * life_safety
        + WEIGHT_UNRESOLVED_AGE * age
        + WEIGHT_ATTRIBUTE_DECAY * decay
    )
    reasons = tuple(severity_reasons + life_safety_reasons + age_reasons + decay_reasons)
    return round(min(1.0, max(0.0, importance)), 6), reasons


def rank_conflicts(district: DistrictReading) -> tuple[RankedConflict, ...]:
    """Every open conflict in the district, most important first.

    Ties break on ``conflict_id``, which is itself derived from the rule, the
    address, the attribute, and the fact ids -- so two workers ranking the same
    district produce the same order rather than merely the same scores.
    """
    scored: list[tuple[float, Conflict, ProfileReading, tuple[RankReason, ...]]] = []
    for reading in district.readings:
        for conflict in reading.open_conflicts:
            importance, reasons = score_conflict(conflict, reading)
            scored.append((importance, conflict, reading, reasons))
    scored.sort(key=lambda item: (-item[0], item[1].conflict_id))

    return tuple(
        RankedConflict(
            conflict_id=conflict.conflict_id,
            address_id=conflict.address_id,
            district_id=district.district_id,
            canonical_key=conflict.canonical_key,
            rule_id=conflict.rule_id,
            severity=conflict.severity,
            rank=index,
            importance=importance,
            reasons=reasons,
            summary=conflict.summary,
        )
        for index, (importance, conflict, _reading, reasons) in enumerate(scored, start=1)
    )


# ----------------------------------------------------- ranking structures


def _conflict_signal(reading: ProfileReading) -> tuple[float, list[RankReason]]:
    """Severity of the worst open conflict, normalised to [0, 1].

    The *value* is the worst severity present, not the importance of the worst
    conflict: a structure with a severity-5 disagreement deserves that weight
    whether or not the disagreement is also old. Importance only breaks ties
    over *which* conflict the row cites, so the queue names the one a captain
    would open first.

    The tiebreak matters more than it looks. ``profile.conflicts`` is in append
    order, so picking the first maximum by iteration order made the cited
    conflict depend on how many passes had run -- two deployments holding the
    same facts could print different reasons for the same score.
    """
    open_conflicts = reading.open_conflicts
    if not open_conflicts:
        return 0.0, []
    worst = min(
        open_conflicts,
        key=lambda c: (-c.severity, -score_conflict(c, reading)[0], c.conflict_id),
    )
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


def _decay_signal(reading: ProfileReading) -> tuple[float, list[RankReason]]:
    """How stale the profile's resolved facts have become.

    The decay map is the one this pass computed. The Delta Ranker preferred the
    map stored on the profile and only recomputed when it was empty, so a queue
    could rank a district on decay numbers an earlier pass had left behind --
    while the conflict engine, in the same second, was working from fresh ones.
    """
    decay = reading.decay
    if not decay:
        return 0.0, []
    # Sorted on (value, key): two attributes that decayed to the same number
    # must not have the cited one chosen by dict insertion order.
    worst_key = min(decay, key=lambda key: (decay[key], key))
    staleness = 1.0 - decay[worst_key]
    if staleness <= 0.0:
        return 0.0, []
    resolved = reading.profile.facts.get(worst_key)
    return staleness, [
        RankReason(
            rule_id=RULE_DECAY,
            canonical_key=worst_key,
            detail=(
                f"Confidence in {worst_key} has decayed to "
                f"{decay[worst_key]:.2f} of its filed value"
            ),
            weight=round(staleness, 4),
            fact_ids=(resolved.fact_id,) if resolved is not None else (),
        )
    ]


def _churn_signal(reading: ProfileReading) -> tuple[float, list[RankReason]]:
    """Filings recorded since the last human survey."""
    profile = reading.profile
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
            fact_ids=tuple(sorted({fid for event in filings for fid in event.fact_ids})),
        )
    ]


def _survey_age_signal(reading: ProfileReading) -> tuple[float, list[RankReason]]:
    """How long since a person last stood in the building."""
    profile = reading.profile
    if profile.last_human_survey is None:
        return 1.0, [
            RankReason(
                rule_id=RULE_NEVER_SURVEYED,
                detail="No company survey on record for this structure",
                weight=1.0,
            )
        ]
    age_days = max(0.0, (reading.read_at - profile.last_human_survey).total_seconds() / 86_400.0)
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


def score_reading(reading: ProfileReading) -> tuple[float, tuple[RankReason, ...]]:
    """The deterministic score for one structure, with its reasons.

    Weighted sum of four signals, each already normalised to ``[0, 1]``. The
    weights sum to one, so the score is directly comparable across districts.

    There is no ``now`` parameter, and that is deliberate: the instant is the
    reading's, so the score cannot be computed as of a moment the conflicts were
    not detected as of.
    """
    conflict, conflict_reasons = _conflict_signal(reading)
    decay, decay_reasons = _decay_signal(reading)
    churn, churn_reasons = _churn_signal(reading)
    survey_age, age_reasons = _survey_age_signal(reading)

    score = (
        WEIGHT_CONFLICT * conflict
        + WEIGHT_DECAY * decay
        + WEIGHT_CHURN * churn
        + WEIGHT_SURVEY_AGE * survey_age
    )
    reasons = tuple(conflict_reasons + decay_reasons + churn_reasons + age_reasons)
    return round(min(1.0, max(0.0, score)), 6), reasons


def rank_structures(
    district: DistrictReading, *, agent_version: str, min_score: float = MIN_SCORE
) -> tuple[tuple[SurveyQueueEntry, ...], int]:
    """The district's queue and how many structures fell below the floor."""
    scored: list[tuple[float, ProfileReading, tuple[RankReason, ...]]] = []
    skipped = 0
    for reading in district.readings:
        score, reasons = score_reading(reading)
        if score < min_score or not reasons:
            skipped += 1
            continue
        scored.append((score, reading, reasons))

    # Descending score; address id breaks ties so two processes agree.
    scored.sort(key=lambda item: (-item[0], item[1].address_id))

    entries = tuple(
        SurveyQueueEntry(
            # Derived from the district and address: re-ranking replaces the row
            # rather than accumulating a second one for one building.
            entry_id=f"queue_{district.district_id}_{reading.address_id}",
            address_id=reading.address_id,
            district_id=district.district_id,
            rank=index,
            score=score,
            reasons=reasons,
            status=QueueEntryStatus.RANKED,
            created_at=district.read_at,
            ranked_by_version=agent_version,
        )
        for index, (score, reading, reasons) in enumerate(scored, start=1)
    )
    return entries, skipped


# ----------------------------------------------------------------- result


class StructureWatchResult(BaseModel):
    """Everything one pass over a district derived, from its one reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    #: The single instant the whole pass is as of.
    read_at: datetime | None = None

    entries: tuple[SurveyQueueEntry, ...] = ()
    #: Open conflicts across the district, most important first.
    conflicts: tuple[RankedConflict, ...] = ()
    #: Structures considered but scored below the floor.
    skipped: int = Field(default=0, ge=0)

    #: Conflicts this pass recorded that were not already stored.
    new_conflict_ids: tuple[str, ...] = ()
    published_event_ids: tuple[str, ...] = ()
    #: Addresses whose profile write lost the version check. Not an error: the
    #: other writer ran the same deterministic engine over the same facts.
    contended: tuple[str, ...] = ()

    @property
    def top_address_id(self) -> str | None:
        return self.entries[0].address_id if self.entries else None

    @property
    def top_conflict_id(self) -> str | None:
        return self.conflicts[0].conflict_id if self.conflicts else None


class StructureWatch:
    """Watches a district's profiles, detects, and ranks -- in one pass."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        conflicts: ConflictRepository,
        queue: QueueRepository,
        clock: Clock,
        ids: IdGenerator | None = None,
        bus: EventBus | None = None,
        audit: AuditSink | None = None,
        registry: RuleRegistry | None = None,
        agent_version: str = "1.0.0",
        min_score: float = MIN_SCORE,
    ) -> None:
        self._profiles = profiles
        self._conflicts = conflicts
        self._queue = queue
        self._clock = clock
        self._ids = ids
        self._bus = bus
        #: Optional, like ``bus`` and ``ids`` above, so every existing caller
        #: and test constructs unchanged. With no sink this agent does exactly
        #: what it always did -- detect, rank, store, publish -- and leaves no
        #: trace of having done it, which is why the fleet panel drew it idle
        #: while it was ranking a district. Its work reached the profile store,
        #: the conflict log, the queue and the bus; the console reads none of
        #: those. It reads this one.
        self._audit = audit
        self._registry = registry
        self._agent_version = agent_version
        self._min_score = min_score

    async def watch(
        self,
        district_id: str,
        *,
        correlation_id: str = "",
        deadline: datetime | None = None,
    ) -> StructureWatchResult:
        """Read the district once, then detect, rank, and store what it found.

        The queue is recomputed wholesale rather than patched: a partly updated
        queue would be a ranking of two different moments, and the order is the
        whole product.

        There is exactly one ``clock.now()`` and one ``list_by_district`` in
        this method, and every number below is derived from them. Adding a
        second read -- to "refresh" a profile before scoring it, say -- would
        reintroduce the split this agent was merged to close.

        ``deadline`` is the caller's, and the runtime that invoked this agent is
        the caller that has one. Persisting detection is a versioned write per
        structure and over a real district that is minutes against a catalogued
        sixty seconds, so without a deadline this method did not return: the
        runtime cancelled it, no pass was recorded, and the caller got no queue
        to hand the referral clerk. Ranking is deliberately *not* skipped when
        the budget runs short -- deciding which building a company is sent to is
        the decision this agent exists to make, it is arithmetic over a reading
        already in hand, and a conflict not persisted this pass is re-derived
        next pass from facts that are already stored.
        """
        now = self._clock.now()
        profiles = await self._profiles.list_by_district(district_id)
        district = read_district(
            district_id,
            profiles,
            now=now,
            registry=self._registry,
            agent_version=self._agent_version,
        )

        new_conflicts: list[Conflict] = []
        contended: list[str] = []
        unpersisted = 0
        for position, reading in enumerate(district.readings):
            if self._past(deadline, margin_ms=_RANK_TAIL_MS):
                # Stop detecting, not stop working. What is left of the budget
                # buys the ranking and the pass record, which are the two things
                # this pass cannot leave behind if it is cancelled here.
                unpersisted = len(district.readings) - position
                break
            stored_cleanly = await self._persist(reading)
            new_conflicts.extend(reading.new_conflicts)
            if not stored_cleanly:
                contended.append(reading.address_id)
            # Recorded here, inside the loop, rather than accumulated and
            # flushed after it. Detection is a profile read, a rule sweep and a
            # versioned write per structure, which over a real district is
            # minutes; a console whose only evidence is this log would show
            # nothing at all until the last building was committed and then
            # show everything at once, which is a report rather than work in
            # progress.
            await self._record_step(reading, correlation_id, contended=not stored_cleanly)

        entries, skipped = rank_structures(
            district, agent_version=self._agent_version, min_score=self._min_score
        )
        ranked_conflicts = rank_conflicts(district)
        stored = await self._queue.replace_district_queue(district_id, entries)
        for entry in stored:
            if self._past(deadline, margin_ms=_RECORD_TAIL_MS):
                # The rows are already in the queue; these are the lines that
                # narrate them. Dropping the narration to keep the pass record
                # is the right way round -- the record is what says the agent
                # ran, and the rows are readable from the queue either way.
                break
            # As soon as the row exists, not once the whole pass is over. This
            # is the half of the merge that usually has something to say: by
            # the time a district reaches this agent the watchers' own
            # materialization has normally already recorded the disagreements,
            # so detection re-derives ids that are all already stored and the
            # loop above records nothing -- while the ranking still decides
            # which building a company is sent to, which is the decision this
            # agent exists to make.
            await self._record_ranked(entry, correlation_id)

        published = await self._announce(
            district,
            new_conflicts=tuple(new_conflicts),
            entries=tuple(stored),
            correlation_id=correlation_id,
        )
        new_conflict_ids = [c.conflict_id for c in new_conflicts]

        logger.info(
            "district_watched",
            extra={
                "district_id": district_id,
                "ranked": len(stored),
                "skipped": skipped,
                "open_conflicts": len(ranked_conflicts),
                "new_conflicts": len(new_conflict_ids),
                "contended": len(contended),
            },
        )
        await self._record_pass(
            district_id,
            correlation_id,
            profiles=len(district.readings),
            detected=len(new_conflict_ids),
            open_conflicts=len(ranked_conflicts),
            entries=tuple(stored),
            skipped=skipped,
            contended=len(contended),
            published=len(published),
            unpersisted=unpersisted,
        )
        return StructureWatchResult(
            district_id=district_id,
            read_at=now,
            entries=tuple(stored),
            conflicts=ranked_conflicts,
            skipped=skipped,
            new_conflict_ids=tuple(new_conflict_ids),
            published_event_ids=published,
            contended=tuple(contended),
        )

    # ------------------------------------------------------------ internals

    def _past(self, deadline: datetime | None, *, margin_ms: int) -> bool:
        """Whether the budget is spent down to the tail this step still needs.

        ``margin_ms`` is what has to be left for everything after the check,
        which is why the two callers pass different numbers -- detection must
        leave room for the ranking, and the ranked lines must leave room for the
        record of the pass.

        The injected clock, never the wall clock. A deadline derived from a
        ``SteppingClock`` and compared against ``datetime.now()`` reads as spent
        before the first structure, and every district would rank nothing.
        """
        if deadline is None:
            return False
        return self._clock.now() >= deadline - timedelta(milliseconds=margin_ms)

    async def _record_pass(
        self,
        district_id: str,
        correlation_id: str,
        *,
        profiles: int,
        detected: int,
        open_conflicts: int,
        entries: Sequence[SurveyQueueEntry],
        skipped: int,
        contended: int,
        published: int,
        unpersisted: int = 0,
    ) -> None:
        """One line saying what this pass read, found, and put in front of whom.

        Both halves of the merge, because both are this agent's work and either
        one alone misreads it: a pass that detected nothing new but re-ranked a
        district did work, and so did a pass that detected four disagreements on
        structures that all scored below the floor.

        ``skipped`` beside ``ranked`` for the same reason ``deferred`` sits
        beside ``measured`` on the geometry pass -- a district where nothing was
        worth a company's morning and a district nobody looked at produce the
        same empty queue and are not the same statement.

        Counts and derived ids only. A rank reason is a sentence this agent
        composed about a building, and it belongs on the queue row where an
        officer reads it with the rule id that produced it, not restated here
        with neither.
        """
        if self._audit is None or self._ids is None:
            return
        detail = {
            "profiles": str(profiles),
            "conflicts_detected": str(detected),
            "open_conflicts": str(open_conflicts),
            "ranked": str(len(entries)),
            "skipped": str(skipped),
            "contended": str(contended),
            "events_published": str(published),
        }
        if unpersisted:
            # A district the budget did not finish detecting on is not a
            # district with nothing to detect, and the queue below was ranked
            # over the whole reading either way. Saying so is the difference.
            detail["unpersisted"] = str(unpersisted)
        if entries:
            # Which row a company is actually sent to. The entry id, not the
            # score: the score is on the row, derived from reasons that are also
            # on the row, and a second copy here would be a number nobody could
            # re-derive from what this record carries.
            detail["entry_id"] = entries[0].entry_id
            detail["address_id"] = entries[0].address_id
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_PASS,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=district_id,
                # A slow-loop pass belongs to no incident, and stamping one on
                # it would put a district sweep inside a fire's counters.
                correlation_id=correlation_id or self._ids.new_id("corr"),
                detail=detail,
            )
        )

    async def _record_step(
        self, reading: ProfileReading, correlation_id: str, *, contended: bool
    ) -> None:
        """One structure, as this pass finishes with it.

        Only where the reading produced something the store did not already
        hold. A district is hundreds of profiles and most passes change none of
        them, so a line per profile read would be hundreds of lines saying
        nothing and would bury the four that say something -- the same
        discipline the other watchers keep, where a step is an address that was
        written rather than an address that was considered.

        A lost version check still counts. Another writer ran the same
        deterministic engine over the same facts, so the finding stands; what
        did not happen is this pass's write, and that is worth one word here
        rather than silence indistinguishable from success.
        """
        if self._audit is None or self._ids is None:
            return
        if not reading.new_conflicts and not reading.changed:
            return
        keys = sorted({str(conflict.canonical_key) for conflict in reading.new_conflicts})
        rules = sorted({conflict.rule_id for conflict in reading.new_conflicts})
        detail = {
            "conflicts_detected": str(len(reading.new_conflicts)),
            "open_conflicts": str(len(reading.open_conflicts)),
            # The engine's own vocabulary -- the attribute in dispute and the
            # rule that fired -- never what either source said it was.
            "keys": ",".join(keys) if keys else "none",
            "rules": ",".join(rules) if rules else "none",
            "profile_version": str(reading.profile.profile_version),
        }
        if contended:
            detail["contended"] = "true"
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_STEP,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=reading.address_id,
                address_id=reading.address_id,
                # The pass's own id, so a structure's step groups under the
                # pass that produced it instead of floating loose in the log.
                correlation_id=correlation_id or self._ids.new_id("corr"),
                detail=detail,
            )
        )

    async def _record_ranked(self, entry: SurveyQueueEntry, correlation_id: str) -> None:
        """One structure, as it lands on the queue a company works from.

        The rank and the rules that produced it, never the reason text. A
        :class:`RankReason` detail is a sentence this agent composed about a
        building; it is already on the row, beside the rule id, the weight and
        the fact ids that support it, and a copy here would read as a finding
        with none of that behind it.
        """
        if self._audit is None or self._ids is None:
            return
        rules = sorted({reason.rule_id for reason in entry.reasons})
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_STEP,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=entry.address_id,
                address_id=entry.address_id,
                correlation_id=correlation_id or self._ids.new_id("corr"),
                detail={
                    "entry_id": entry.entry_id,
                    "rank": str(entry.rank),
                    "reasons": str(len(entry.reasons)),
                    "rules": ",".join(rules),
                    "status": str(entry.status),
                },
            )
        )

    async def _persist(self, reading: ProfileReading) -> bool:
        """Store what one reading derived. False means another writer won.

        Losing the version check is *not* an error and does not invalidate the
        reading: the other writer ran the same deterministic engine over the
        same facts, so its conflicts and decay are ours. Ranking therefore
        continues from the reading we already hold rather than re-reading --
        a re-read here is exactly the second reading this agent exists to avoid.
        """
        for conflict in reading.new_conflicts:
            # Conflict ids are derived, so "already stored" means the same rule
            # fired on the same facts -- the same finding, not a second one.
            try:
                await self._conflicts.add(conflict)
            except AppendOnlyViolationError:
                logger.debug(
                    "conflict_already_recorded", extra={"conflict_id": conflict.conflict_id}
                )

        if not reading.changed:
            # The expected outcome of a pass over facts nothing has touched.
            return True
        try:
            await self._profiles.save(reading.profile, expected_version=reading.base_version)
        except StaleVersionError:
            logger.info("structure_watch_contended", extra={"address_id": reading.address_id})
            return False
        return True

    async def _announce(
        self,
        district: DistrictReading,
        *,
        new_conflicts: tuple[Conflict, ...],
        entries: tuple[SurveyQueueEntry, ...],
        correlation_id: str,
    ) -> tuple[str, ...]:
        """Publish what this pass found. Identifiers only, as always.

        Two topics, and both were broken in their own way:

        ``conflict.detected`` announces a disagreement this pass recorded that
        nothing had recorded before. Republishing cannot double-notify because
        the key is derived from the conflict id, and a redelivery of it back to
        this agent is harmless for the same reason the whole design works --
        the second pass over the same facts finds nothing new to announce.

        ``queue.ranked`` is what wakes the referral clerk. It was declared as a
        topic and named by that agent's subscription, and *nothing in the system
        had ever published it* -- so the clerk's subscription was a pipe with no
        water in it. Its key is derived from the ranking itself, so a pass that
        re-derives the same order does not notify anybody a second time.
        """
        if self._bus is None or self._ids is None or not correlation_id:
            return ()

        published: list[str] = []
        for conflict in new_conflicts:
            envelope = EventEnvelope(
                event_id=self._ids.new_id("evt"),
                topic=Topic.CONFLICT_DETECTED,
                occurred_at=district.read_at,
                producer=AGENT_ID,
                producer_version=self._agent_version,
                correlation_id=correlation_id,
                ids={
                    "address_id": conflict.address_id,
                    "conflict_id": conflict.conflict_id,
                    "rule_id": conflict.rule_id,
                    "fact_ids": conflict.fact_ids,
                },
                idempotency_key=self._ids.idempotency_key(
                    "conflict.detected", conflict.conflict_id
                ),
            )
            await self._bus.publish(envelope)
            published.append(envelope.event_id)

        if entries:
            ranking = EventEnvelope(
                event_id=self._ids.new_id("evt"),
                topic=Topic.QUEUE_RANKED,
                occurred_at=district.read_at,
                producer=AGENT_ID,
                producer_version=self._agent_version,
                correlation_id=correlation_id,
                ids={
                    "district_id": district.district_id,
                    "entry_id": entries[0].entry_id,
                    "address_id": entries[0].address_id,
                },
                idempotency_key=self._ids.idempotency_key(
                    "queue.ranked", district.district_id, ranking_digest(entries)
                ),
            )
            await self._bus.publish(ranking)
            published.append(ranking.event_id)
        return tuple(published)


def ranking_digest(entries: Sequence[SurveyQueueEntry]) -> str:
    """A stable digest of one ranking's order and scores.

    Determinism, made checkable: two passes over unchanged profiles produce the
    same digest, which is what lets the ``queue.ranked`` key say "this is the
    same ranking you already saw" rather than "this is another ranking".
    """
    material = "|".join(f"{e.rank}:{e.address_id}:{e.score}" for e in entries)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def survey_interval_days(entries: Sequence[SurveyQueueEntry]) -> timedelta:
    """How far apart surveys would be if the whole queue were worked evenly."""
    return timedelta(days=max(1, len(entries)))


__all__ = [
    "AGENT_ID",
    "MIN_SCORE",
    "RULE_ATTRIBUTE_DECAY",
    "RULE_CHURN",
    "RULE_CONFLICT",
    "RULE_DECAY",
    "RULE_LIFE_SAFETY",
    "RULE_NEVER_SURVEYED",
    "RULE_SEVERITY",
    "RULE_SURVEY_AGE",
    "RULE_UNRESOLVED_AGE",
    "WEIGHT_ATTRIBUTE_DECAY",
    "WEIGHT_CHURN",
    "WEIGHT_CONFLICT",
    "WEIGHT_DECAY",
    "WEIGHT_LIFE_SAFETY",
    "WEIGHT_SEVERITY",
    "WEIGHT_SURVEY_AGE",
    "WEIGHT_UNRESOLVED_AGE",
    "DistrictReading",
    "ProfileReading",
    "RankedConflict",
    "StructureWatch",
    "StructureWatchResult",
    "rank_conflicts",
    "rank_structures",
    "ranking_digest",
    "read_district",
    "read_profile",
    "score_conflict",
    "score_reading",
    "survey_interval_days",
]
