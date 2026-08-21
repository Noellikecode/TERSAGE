"""Recording a company survey -- the only thing that closes a conflict.

A survey is the moment somebody stood in the building and looked. That makes it
the only input that can do three things nothing else can:

* set ``human_verified`` on a fact -- extraction cannot, a model cannot;
* resolve an open conflict, because a disagreement between two documents is
  settled by observation and not by a newer document;
* reset the survey-age signal that put the building in the queue.

The original facts stay. Resolving a conflict records what settled it; it does
not erase that the permit and the lidar disagreed, because that disagreement is
why anybody went.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflict_engine import survey_resolutions
from firstdue.domain.enums import Classification, SourceType, SurveyOutcome
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.profiles import ProfileEvent, ProfileEventType
from firstdue.domain.values import FactValue
from firstdue.domain.work import QueueEntryStatus, SurveyRecord
from firstdue.errors import AppendOnlyViolationError, NotFoundError, StaleVersionError
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS
from firstdue.ports.clock import Clock
from firstdue.ports.repositories import (
    ConflictRepository,
    FactRepository,
    ProfileRepository,
    QueueRepository,
    SurveyRepository,
)

logger = get_logger(__name__)

AGENT_ID = "survey-ranker"


class SurveyResult(BaseModel):
    """What recording one survey changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    survey_id: str
    address_id: str
    facts_written: int = Field(default=0, ge=0)
    conflicts_resolved: tuple[str, ...] = ()
    queue_entry_closed: str | None = None


class SurveyService:
    """Records a survey and applies the human override it carries."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        conflicts: ConflictRepository,
        surveys: SurveyRepository,
        queue: QueueRepository,
        clock: Clock,
    ) -> None:
        self._profiles = profiles
        self._facts = facts
        self._conflicts = conflicts
        self._surveys = surveys
        self._queue = queue
        self._clock = clock

    async def record(
        self,
        survey: SurveyRecord,
        *,
        observations: Mapping[CanonicalKey, FactValue],
    ) -> SurveyResult:
        """Record a survey and everything that follows from it.

        Args:
            survey: the completed survey record.
            observations: what the crew actually saw, per attribute. Only these
                become human-verified facts -- a key listed as verified with
                nothing observed leaves its conflict open, which is the honest
                outcome.
        """
        profile = await self._profiles.get(survey.address_id)
        if profile is None:
            raise NotFoundError("profile not found", details={"address_id": survey.address_id})

        now = self._clock.now()
        try:
            await self._surveys.add(survey)
        except AppendOnlyViolationError:
            logger.info("survey_already_recorded", extra={"survey_id": survey.survey_id})

        updated = profile
        written = 0
        resolving_fact_ids: dict[CanonicalKey, str] = {}

        for key, value in sorted(observations.items()):
            fact = self._human_fact(survey, key=key, value=value, now=now)
            resolving_fact_ids[key] = fact.fact_id
            try:
                await self._facts.append(fact)
            except AppendOnlyViolationError:
                continue
            try:
                updated = updated.with_fact(
                    fact,
                    event=ProfileEvent(
                        event_id=f"pevt_{fact.fact_id.removeprefix('fact_')}",
                        sequence=updated.next_sequence,
                        occurred_at=now,
                        type=ProfileEventType.SURVEY_COMPLETED,
                        actor=survey.surveyor,
                        summary=f"{survey.company} verified {key} on site",
                        canonical_keys=(key,),
                        fact_ids=(fact.fact_id,),
                    ),
                )
            except AppendOnlyViolationError:
                continue
            written += 1

        resolutions = survey_resolutions(
            updated.conflicts, survey, resolving_fact_ids=resolving_fact_ids
        )
        resolved_ids: list[str] = []
        for conflict_id, resolution in resolutions:
            await self._conflicts.resolve(conflict_id, resolution)
            resolved_ids.append(conflict_id)
            updated = updated.model_copy(
                update={
                    "conflicts": tuple(
                        c.resolve(resolution) if c.conflict_id == conflict_id else c
                        for c in updated.conflicts
                    )
                }
            ).append_event(
                ProfileEvent(
                    event_id=f"pevt_res_{conflict_id.removeprefix('conflict_')}",
                    sequence=updated.next_sequence,
                    occurred_at=now,
                    type=ProfileEventType.CONFLICT_RESOLVED,
                    actor=survey.surveyor,
                    summary=f"Conflict settled on site by {survey.company}",
                    conflict_id=conflict_id,
                )
            )

        updated = updated.model_copy(update={"last_human_survey": survey.completed_at})
        try:
            await self._profiles.save(updated, expected_version=profile.profile_version)
        except StaleVersionError:
            logger.info("survey_write_contended", extra={"address_id": survey.address_id})

        closed = await self._close_queue_entry(survey)

        # Queue precision: of the structures the ranker sent someone to, how
        # many turned out to be worth the trip. A survey that wrote a fact or
        # settled a conflict confirmed the ranking; one that found nothing did
        # not. This is the only feedback the ranker ever gets, and without it a
        # ranker that is quietly wrong stays quietly wrong.
        if survey.queue_entry_id is not None:
            METRICS.record_survey_outcome(
                confirmed_the_ranking=bool(written or resolved_ids),
            )

        logger.info(
            "survey_recorded",
            extra={
                "address_id": survey.address_id,
                "facts": written,
                "resolved": len(resolved_ids),
            },
        )
        return SurveyResult(
            survey_id=survey.survey_id,
            address_id=survey.address_id,
            facts_written=written,
            conflicts_resolved=tuple(resolved_ids),
            queue_entry_closed=closed,
        )

    async def _close_queue_entry(self, survey: SurveyRecord) -> str | None:
        if survey.queue_entry_id is None:
            return None
        entry = await self._queue.get(survey.queue_entry_id)
        if entry is None:
            return None
        await self._queue.save(
            entry.model_copy(
                update={"status": QueueEntryStatus.SURVEYED, "survey_id": survey.survey_id}
            )
        )
        return entry.entry_id

    def _human_fact(
        self, survey: SurveyRecord, *, key: str, value: FactValue, now: datetime
    ) -> StructuralFact:
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=survey.address_id,
                canonical_key=key,
                source_ref=f"survey/{survey.survey_id}",
                observed_at=survey.completed_at,
                rendered_value=value.render(),
            ),
            address_id=survey.address_id,
            canonical_key=key,
            value=value,
            source_type=SourceType.HUMAN_SURVEY,
            source_ref=f"survey/{survey.survey_id}",
            source_snapshot_id=f"survey:{survey.survey_id}",
            observed_at=survey.completed_at,
            ingested_at=now,
            confidence=0.98,
            classification=Classification.PUBLIC,
            # Only a survey record can assert this, and the model refuses to
            # construct the fact without naming the survey that did.
            human_verified=survey.outcome is not SurveyOutcome.NO_ACCESS,
            survey_id=survey.survey_id,
            produced_by_agent=AGENT_ID,
        )
