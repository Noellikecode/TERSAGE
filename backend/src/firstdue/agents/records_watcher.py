"""Records Watcher -- filings become facts.

Polls the permit, assessor, inspection, and violation feeds for a district,
resolves each record to a building through the city adapter, extracts
provenanced facts, and appends them to the profile. Then it materializes: the
deterministic conflict engine runs, and any disagreement is recorded.

Everything about it is idempotent by construction rather than by flag. Fact ids
are derived from the observation's natural key, conflict ids from the rule and
the facts, so a second poll of an unchanged source re-derives the same ids and
writes nothing. That is what makes "run the demo twice, get no duplicates" a
property of the arithmetic instead of a check somebody has to remember.

A source that is down does not stop the pass. Its records are missing from this
poll and the profile says the source was unavailable -- never that the hazard
was absent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.errors import AppendOnlyViolationError, SourceUnavailableError, StaleVersionError
from firstdue.extraction.extractor import FactExtractor
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import FactRepository, ProfileRepository
from firstdue.ports.sources import SourceAdapter, SourceRecord, SourceSnapshot
from firstdue.services.materialization import ProfileMaterializer
from firstdue.sources.catalog import ASSESSOR, INSPECTIONS, PERMITS, VIOLATIONS

logger = get_logger(__name__)

AGENT_ID: Final[str] = "records-watcher"

#: Structured columns that are already facts. No model is involved in these:
#: a filed column is a filing, and reading it does not require judgement.
FIELD_MAPS: Final[dict[str, dict[str, str]]] = {
    PERMITS: {"stories_filed": Keys.STORIES},
    ASSESSOR: {
        "year_built": Keys.YEAR_BUILT,
        "construction_type": Keys.CONSTRUCTION_TYPE,
        "use_code": Keys.OCCUPANCY_TYPE,
        "footprint_area_m2": Keys.FOOTPRINT_AREA_M2,
    },
    INSPECTIONS: {},
    VIOLATIONS: {"status": Keys.OPEN_VIOLATION},
}

#: Which merge tier each source's records land in.
SOURCE_TYPES: Final[dict[str, SourceType]] = {
    PERMITS: SourceType.PERMIT,
    ASSESSOR: SourceType.ASSESSOR,
    INSPECTIONS: SourceType.FIRE_INSPECTION,
    VIOLATIONS: SourceType.VIOLATION,
}

WATCHED_SOURCES: Final[tuple[str, ...]] = (PERMITS, ASSESSOR, INSPECTIONS, VIOLATIONS)


class WatchResult(BaseModel):
    """What one watcher pass did to a district."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    addresses_touched: tuple[str, ...] = ()
    facts_written: int = Field(default=0, ge=0)
    #: Facts re-derived identically and therefore not written again.
    facts_deduped: int = Field(default=0, ge=0)
    conflicts_detected: tuple[str, ...] = ()
    #: Sources that could not be reached on this pass. Rendered as UNAVAILABLE.
    unavailable_sources: tuple[str, ...] = ()
    #: Injection patterns the screen removed from ingested documents.
    screen_findings: tuple[str, ...] = ()
    documents_triaged_out: int = Field(default=0, ge=0)


class RecordsWatcher:
    """Turns municipal filings into provenanced facts on a building profile."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        city: CityAdapter,
        extractor: FactExtractor,
        materializer: ProfileMaterializer,
        clock: Clock,
        ids: IdGenerator,
        audit: AuditSink | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._profiles = profiles
        self._facts = facts
        self._city = city
        self._extractor = extractor
        self._materializer = materializer
        self._clock = clock
        self._ids = ids
        self._audit = audit
        self._agent_version = agent_version

    async def poll(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        since: datetime | None = None,
    ) -> WatchResult:
        """Poll every watched source for a district and materialize the results."""
        pending: dict[str, list[StructuralFact]] = {}
        unavailable: list[str] = []
        findings: set[str] = set()
        triaged = 0

        for source in sources:
            source_type = SOURCE_TYPES.get(source.source_id)
            if source_type is None:
                continue
            try:
                snapshots = await self._pull_all(source, since=since)
            except SourceUnavailableError as exc:
                logger.warning(
                    "watcher_source_unavailable",
                    extra={"source_id": source.source_id, "error_code": str(exc.code)},
                )
                unavailable.append(source.source_id)
                continue

            for snapshot in snapshots:
                for record in snapshot.records:
                    address_id = self._resolve(record, district_id)
                    if address_id is None:
                        continue
                    outcome = await self._extractor.extract(
                        record,
                        address_id=address_id,
                        snapshot=snapshot,
                        source_type=source_type,
                        ingested_at=self._clock.now(),
                        field_map=FIELD_MAPS.get(source.source_id, {}),
                    )
                    if outcome.screen_findings:
                        # An ingested document tried to give instructions. The
                        # instruction was removed, the rest of the narrative was
                        # kept, and the attempt is on the record.
                        await self._audit_event(
                            AuditEventKind.INJECTION_BLOCKED,
                            target=source.source_id,
                            address_id=address_id,
                            detail={
                                "record_ref": record.record_ref,
                                "patterns": ",".join(outcome.screen_findings),
                                "screen": "local-injection-detector/1",
                            },
                        )
                    if outcome.model_unavailable_reason == "MODEL_OUTPUT_REJECTED":
                        await self._audit_event(
                            AuditEventKind.MODEL_OUTPUT_REJECTED,
                            target=source.source_id,
                            address_id=address_id,
                            detail={"record_ref": record.record_ref},
                        )
                    findings.update(outcome.screen_findings)
                    triaged += 1 if outcome.triaged_out else 0
                    pending.setdefault(address_id, []).extend(outcome.facts)

        written, deduped = 0, 0
        conflicts: list[str] = []
        for address_id in sorted(pending):
            stored, skipped = await self._apply(address_id, district_id, pending[address_id])
            written += stored
            deduped += skipped
            materialized = await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
            )
            conflicts.extend(materialized.new_conflict_ids)

        logger.info(
            "records_watcher_pass",
            extra={
                "district_id": district_id,
                "addresses": len(pending),
                "facts_written": written,
                "conflicts": len(conflicts),
                "unavailable": len(unavailable),
            },
        )
        return WatchResult(
            district_id=district_id,
            addresses_touched=tuple(sorted(pending)),
            facts_written=written,
            facts_deduped=deduped,
            conflicts_detected=tuple(conflicts),
            unavailable_sources=tuple(unavailable),
            screen_findings=tuple(sorted(findings)),
            documents_triaged_out=triaged,
        )

    # ------------------------------------------------------------ internals

    async def _pull_all(
        self, source: SourceAdapter, *, since: datetime | None, max_pages: int = 50
    ) -> list[SourceSnapshot]:
        snapshots: list[SourceSnapshot] = []
        cursor: str | None = None
        for _ in range(max_pages):
            snapshot = await source.fetch(since=since, cursor=cursor)
            snapshots.append(snapshot)
            cursor = snapshot.next_cursor
            if cursor is None:
                break
        return snapshots

    def _resolve(self, record: SourceRecord, district_id: str) -> str | None:
        """Which building this record is about, or None to skip it.

        Resolution is the city adapter's job. A record that will not resolve is
        dropped rather than attached to a best guess -- a permit filed against
        the wrong building is worse than a permit nobody saw.
        """
        raw = record.address_id or str(record.fields.get("street_address") or "")
        if not raw:
            return None
        address = self._city.normalize_address(raw)
        if address is None or address.district_id != district_id:
            return None
        return address.address_id

    async def _apply(
        self, address_id: str, district_id: str, facts: Sequence[StructuralFact]
    ) -> tuple[int, int]:
        """Append facts to the store and the profile. Returns (written, deduped)."""
        profile = await self._profiles.get(address_id)
        if profile is None:
            profile = await self._profiles.create(
                BuildingProfile(address_id=address_id, district_id=district_id)
            )

        written = 0
        deduped = 0
        updated = profile
        for fact in sorted(facts, key=lambda f: (f.observed_at, f.fact_id)):
            try:
                await self._facts.append(fact)
            except AppendOnlyViolationError:
                # Re-derived identically: the same observation, not a new one.
                deduped += 1
                continue
            try:
                updated = updated.with_fact(fact, event=self._event(updated, fact))
            except AppendOnlyViolationError:
                deduped += 1
                continue
            written += 1

        if updated.profile_version != profile.profile_version:
            try:
                await self._profiles.save(updated, expected_version=profile.profile_version)
            except StaleVersionError:
                # Another instance wrote first. Its pass extracted the same
                # facts from the same records, so there is nothing to redo.
                logger.info("watcher_write_contended", extra={"address_id": address_id})
                return 0, written + deduped
        return written, deduped

    async def _audit_event(
        self,
        kind: AuditEventKind,
        *,
        target: str,
        detail: dict[str, str],
        address_id: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=kind,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=target,
                address_id=address_id,
                correlation_id=self._ids.new_id("corr"),
                detail=detail,
            )
        )

    def _event(self, profile: BuildingProfile, fact: StructuralFact) -> ProfileEvent:
        return ProfileEvent(
            event_id=f"pevt_{fact.fact_id.removeprefix('fact_')}",
            sequence=profile.next_sequence,
            occurred_at=fact.ingested_at,
            type=ProfileEventType.FACT_WRITTEN,
            actor=AGENT_ID,
            actor_version=self._agent_version,
            summary=f"{fact.source_type} recorded {fact.canonical_key}",
            canonical_keys=(fact.canonical_key,),
            fact_ids=(fact.fact_id,),
        )


def field_map_for(source_id: str) -> Mapping[str, str]:
    return FIELD_MAPS.get(source_id, {})
