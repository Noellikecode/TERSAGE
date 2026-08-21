"""Hazard Watcher -- federal registries and the one confidential filing.

EPA (FRS, TRI, RMP), PHMSA pipelines, NREL EV charging, and Tier II. Four very
different data sources with one thing in common: **absence here is dangerous to
misread.** "No Tier II filing on record" and "no hazardous materials present"
are different statements, and a watcher that returned an empty list for a source
it could not reach would collapse them.

So every fact this agent writes carries its classification, and a source that is
unavailable produces an ``UNAVAILABLE`` fact naming the source rather than
nothing at all.

Tier II is the reason this agent is published by county emergency management
rather than by the fire department: the filings are confidential, the county
holds them, and the fire department subscribes to a pinned version of the agent
that reads them. The subscription is the authorization boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import ProfileEvent, ProfileEventType
from firstdue.domain.values import (
    BooleanValue,
    FactValue,
    QuantityValue,
    TextValue,
    UnavailableValue,
)
from firstdue.errors import AppendOnlyViolationError, SourceUnavailableError, StaleVersionError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.repositories import FactRepository, ProfileRepository
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.services.materialization import ProfileMaterializer
from firstdue.sources.catalog import EPA, NREL, PHMSA, TIER_II

logger = get_logger(__name__)

AGENT_ID: Final[str] = "hazard-watcher"

SOURCE_TYPES: Final[dict[str, SourceType]] = {
    EPA: SourceType.EPA_FRS,
    PHMSA: SourceType.PHMSA_PIPELINE,
    NREL: SourceType.NREL_EV,
    TIER_II: SourceType.TIER_II,
}

#: The attribute each source settles. Used to write an explicit UNAVAILABLE
#: when the source is down, so absence never reads as "nothing there".
SOURCE_KEYS: Final[dict[str, str]] = {
    EPA: Keys.HAZARD_TIER_II_PRESENT,
    PHMSA: Keys.HAZARD_PIPELINE_PROXIMITY_M,
    NREL: Keys.HAZARD_EV_CHARGER,
    TIER_II: Keys.HAZARD_TIER_II_PRESENT,
}


class HazardWatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    addresses_touched: tuple[str, ...] = ()
    facts_written: int = Field(default=0, ge=0)
    #: The ids of the facts this pass appended, for the run record.
    written_fact_ids: tuple[str, ...] = ()
    #: Facts written as UNAVAILABLE because a registry could not be reached.
    unavailable_facts: int = Field(default=0, ge=0)
    unavailable_sources: tuple[str, ...] = ()
    #: Classifications actually written, so the console can show what was touched.
    classifications: tuple[str, ...] = ()


def _values_for(source_id: str, record: SourceRecord) -> list[tuple[str, FactValue, float]]:
    """Every (key, value, confidence) one hazard record supports."""
    fields = record.fields
    if source_id == TIER_II:
        values: list[tuple[str, FactValue, float]] = [
            (Keys.HAZARD_TIER_II_PRESENT, BooleanValue(boolean=bool(fields.get("present"))), 0.95)
        ]
        location = fields.get("storage_location")
        if location:
            values.append(
                (Keys.HAZARD_TIER_II_LOCATION, TextValue(text=str(location)[:2000]), 0.95)
            )
        return values

    if source_id == EPA:
        programs = fields.get("programs") or []
        return [
            (
                Keys.HAZARD_TIER_II_PRESENT,
                BooleanValue(boolean=bool({"RMP", "TRI"} & set(programs))),
                0.9,
            )
        ]

    if source_id == PHMSA:
        proximity = fields.get("proximity_m")
        if proximity is None:
            return []
        return [
            (
                Keys.HAZARD_PIPELINE_PROXIMITY_M,
                QuantityValue(magnitude=float(proximity), unit="m"),
                0.9,
            )
        ]

    if source_id == NREL:
        return [(Keys.HAZARD_EV_CHARGER, BooleanValue(boolean=True), 0.9)]

    return []


class HazardWatcher:
    """Federal and confidential hazard registries, with classification intact."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        materializer: ProfileMaterializer,
        clock: Clock,
        agent_version: str = "1.0.0",
    ) -> None:
        self._profiles = profiles
        self._facts = facts
        self._materializer = materializer
        self._clock = clock
        self._agent_version = agent_version

    async def poll(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
    ) -> HazardWatchResult:
        now = self._clock.now()
        pending: dict[str, list[StructuralFact]] = {}
        unavailable_sources: list[str] = []
        unavailable_facts = 0
        classifications: set[str] = set()

        for source in sources:
            source_type = SOURCE_TYPES.get(source.source_id)
            if source_type is None:
                continue
            try:
                snapshot = await source.fetch()
            except SourceUnavailableError as exc:
                logger.warning(
                    "hazard_source_unavailable",
                    extra={"source_id": source.source_id, "error_code": str(exc.code)},
                )
                unavailable_sources.append(source.source_id)
                # Every address in the district gets an explicit UNAVAILABLE
                # for this attribute. "The registry is down" is an operational
                # fact, and it is not the same as "no hazard here".
                for profile in await self._profiles.list_by_district(district_id):
                    fact = self._unavailable_fact(
                        profile.address_id, source.source_id, source_type, now
                    )
                    pending.setdefault(profile.address_id, []).append(fact)
                    unavailable_facts += 1
                continue

            for record in snapshot.records:
                if record.address_id is None:
                    continue
                classifications.add(str(record.classification))
                for key, value, confidence in _values_for(source.source_id, record):
                    pending.setdefault(record.address_id, []).append(
                        self._fact(
                            record.address_id,
                            key,
                            value,
                            record=record,
                            source_type=source_type,
                            confidence=confidence,
                            now=now,
                        )
                    )

        written = 0
        touched: list[str] = []
        for address_id in sorted(pending):
            # A registry row can name a building this district has no profile
            # for -- another district, or one nothing has filed on yet. Hazard
            # facts do not create profiles; the records watcher does that.
            existing = await self._profiles.get(address_id)
            if existing is None or existing.district_id != district_id:
                continue
            touched.append(address_id)
            written += await self._apply(address_id, district_id, pending[address_id], now)
            await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
            )

        return HazardWatchResult(
            district_id=district_id,
            addresses_touched=tuple(touched),
            facts_written=written,
            unavailable_facts=unavailable_facts,
            unavailable_sources=tuple(unavailable_sources),
            classifications=tuple(sorted(classifications)),
        )

    # ------------------------------------------------------------ internals

    async def _apply(
        self,
        address_id: str,
        district_id: str,
        facts: Sequence[StructuralFact],
        now: datetime,
    ) -> int:
        profile = await self._profiles.get(address_id)
        if profile is None:
            return 0

        written = 0
        updated = profile
        for fact in sorted(facts, key=lambda f: (f.canonical_key, f.fact_id)):
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
                        type=ProfileEventType.FACT_WRITTEN,
                        actor=AGENT_ID,
                        actor_version=self._agent_version,
                        summary=f"{fact.source_type} recorded {fact.canonical_key}",
                        canonical_keys=(fact.canonical_key,),
                        fact_ids=(fact.fact_id,),
                    ),
                )
            except AppendOnlyViolationError:
                continue
            written += 1

        if updated.profile_version != profile.profile_version:
            try:
                await self._profiles.save(updated, expected_version=profile.profile_version)
            except StaleVersionError:
                return 0
        return written

    def _unavailable_fact(
        self, address_id: str, source_id: str, source_type: SourceType, now: datetime
    ) -> StructuralFact:
        key = SOURCE_KEYS.get(source_id, Keys.HAZARD_TIER_II_PRESENT)
        value = UnavailableValue(source_id=source_id, reason="source unreachable")
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=address_id,
                canonical_key=key,
                source_ref=f"{source_id}/unavailable",
                observed_at=now,
                rendered_value=value.render(),
            ),
            address_id=address_id,
            canonical_key=key,
            value=value,
            source_type=source_type,
            source_ref=f"{source_id}/unavailable",
            source_snapshot_id=f"{source_id}:unavailable:{now.isoformat()}",
            observed_at=now,
            ingested_at=now,
            confidence=0.0,
            classification=Classification.PUBLIC,
            produced_by_agent=AGENT_ID,
            produced_by_version=self._agent_version,
        )

    def _fact(
        self,
        address_id: str,
        key: str,
        value: Any,
        *,
        record: SourceRecord,
        source_type: SourceType,
        confidence: float,
        now: datetime,
    ) -> StructuralFact:
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=address_id,
                canonical_key=key,
                source_ref=record.record_ref,
                observed_at=record.observed_at,
                rendered_value=value.render(),
            ),
            address_id=address_id,
            canonical_key=key,
            value=value,
            source_type=source_type,
            source_ref=record.record_ref,
            source_snapshot_id=record.record_ref,
            observed_at=record.observed_at,
            ingested_at=now,
            confidence=confidence,
            # The record's own classification travels with the fact. A Tier II
            # filing stays TIER_II_CONFIDENTIAL all the way to the vector guard.
            classification=record.classification,
            produced_by_agent=AGENT_ID,
            produced_by_version=self._agent_version,
        )
