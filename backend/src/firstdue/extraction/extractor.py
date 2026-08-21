"""Documents to facts, with provenance and a screen in front of the model.

The order is the design:

1. **Screen.** The document is untrusted input. Recognised injection shapes are
   stripped before anything sees them, and reported.
2. **Triage** (optional, Gemma). Most inspection narratives say nothing new. A
   cheap local classifier decides whether the document is worth a Gemini call
   at all -- and its only power is to skip work. It can never assert a fact,
   so a wrong triage costs an extraction, not a wrong brief.
3. **Extract** (Gemini, or the deterministic fake). Returns candidate values,
   each bound to a character span in the document.
4. **Coerce and provenance.** Each candidate becomes a typed value or is
   dropped. Survivors become facts carrying the source ref, the snapshot id,
   and the span -- because a value a human cannot trace back to a line in a
   filed document is not a fact, it is a claim.

A rejected model response is not a failure of the pass. The structured fields
the source published are extracted deterministically regardless; the model only
ever adds what a narrative says that a column does not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import SourceSpan, StructuralFact, natural_fact_id
from firstdue.domain.keys import Keys
from firstdue.errors import UpstreamTimeoutError
from firstdue.extraction.coercion import coerce_value
from firstdue.extraction.screening import ScreenResult, screen_document
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import IdGenerator
from firstdue.ports.model import ModelClient
from firstdue.ports.sources import SourceRecord, SourceSnapshot

logger = get_logger(__name__)

#: What a narrative may be asked about. A closed list: a model cannot mint a
#: canonical key, because a key nothing recognises is a value nothing renders.
NARRATIVE_KEYS: Final[tuple[str, ...]] = (
    Keys.STORIES,
    Keys.CONSTRUCTION_TYPE,
    Keys.YEAR_BUILT,
    Keys.LIGHTWEIGHT_TRUSS,
    Keys.SUPPRESSION_SPRINKLERED,
    Keys.EGRESS_OBSTRUCTION,
    Keys.HAZARD_SOLAR_ARRAY,
)

#: Confidence assigned to a value a model read out of prose. Deliberately below
#: a filed structured field: prose is evidence, a column is a filing.
MODEL_CONFIDENCE_CEILING: Final[float] = 0.72
EXTRACTION_DEADLINE_MS: Final[int] = 8_000

#: Documents shorter than this are not worth a model call at all.
TRIAGE_MIN_CHARS: Final[int] = 40


@dataclass(slots=True)
class TriageDecision:
    """Whether a document is worth extracting from, and why."""

    extract: bool
    reason: str
    #: Keys the triage thought were plausibly present. Advisory only.
    candidate_keys: tuple[str, ...] = field(default_factory=tuple)


class ExtractionOutcome(BaseModel):
    """Everything one document produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: tuple[StructuralFact, ...] = ()
    #: Injection patterns the screen removed before the model saw the text.
    screen_findings: tuple[str, ...] = ()
    #: True when triage decided the document was not worth a model call.
    triaged_out: bool = False
    #: Set when the model was unreachable or its output was rejected. The
    #: structured fields were still extracted; only the narrative was lost.
    model_unavailable_reason: str | None = Field(default=None, max_length=200)

    @property
    def used_model(self) -> bool:
        return not self.triaged_out and self.model_unavailable_reason is None


def triage(text: str, *, keys: Sequence[str] = NARRATIVE_KEYS) -> TriageDecision:
    """Cheap local classifier: is this document worth a Gemini call?

    Stands in for the Gemma pass. It looks for the vocabulary the structural
    keys are actually described in, and it can only ever *skip* work -- a false
    negative costs one extraction, and a false positive costs one model call.
    Neither can put a wrong fact in front of an officer.
    """
    if len(text.strip()) < TRIAGE_MIN_CHARS:
        return TriageDecision(extract=False, reason="document too short to carry a structural fact")

    lowered = text.lower()
    signals: dict[str, tuple[str, ...]] = {
        Keys.STORIES: ("storey", "story", "stories", "floor"),
        Keys.CONSTRUCTION_TYPE: ("wood-frame", "wood frame", "ordinary", "timber", "type i"),
        Keys.YEAR_BUILT: ("built in",),
        Keys.LIGHTWEIGHT_TRUSS: ("truss",),
        Keys.SUPPRESSION_SPRINKLERED: ("sprinkler",),
        Keys.EGRESS_OBSTRUCTION: ("stairwell", "egress", "obstructed"),
        Keys.HAZARD_SOLAR_ARRAY: ("solar",),
    }
    present = tuple(key for key in keys if any(token in lowered for token in signals.get(key, ())))
    if not present:
        return TriageDecision(extract=False, reason="no structural vocabulary present")
    return TriageDecision(
        extract=True, reason="structural vocabulary present", candidate_keys=present
    )


class FactExtractor:
    """Turns one source record into provenanced facts."""

    def __init__(
        self,
        *,
        ids: IdGenerator,
        model: ModelClient | None = None,
        agent_id: str = "records-watcher",
        agent_version: str = "1.0.0",
        use_triage: bool = True,
    ) -> None:
        self._ids = ids
        self._model = model
        self._agent_id = agent_id
        self._agent_version = agent_version
        self._use_triage = use_triage

    async def extract(
        self,
        record: SourceRecord,
        *,
        address_id: str,
        snapshot: SourceSnapshot,
        source_type: SourceType,
        ingested_at: datetime,
        field_map: Mapping[str, str] | None = None,
    ) -> ExtractionOutcome:
        """Extract every fact one record supports.

        Args:
            record: the record as the source returned it.
            address_id: the building it belongs to, already resolved.
            snapshot: the pull it came from -- its id lands on every fact.
            source_type: drives the merge tier.
            ingested_at: when FIRST DUE learned it.
            field_map: structured column to canonical key. Columns are filings
                and need no model.
        """
        facts: list[StructuralFact] = []

        for column, key in (field_map or {}).items():
            if column not in record.fields:
                continue
            value = coerce_value(key, str(record.fields[column]))
            if value is None:
                continue
            facts.append(
                self._fact(
                    address_id=address_id,
                    key=key,
                    value=value,
                    record=record,
                    snapshot=snapshot,
                    source_type=source_type,
                    ingested_at=ingested_at,
                    # A filed column is a filing, not a reading of prose.
                    confidence=0.92,
                    span=None,
                    by_model=False,
                )
            )

        screen: ScreenResult = screen_document(record.document_text)
        if not screen.safe_text or self._model is None:
            return ExtractionOutcome(facts=tuple(facts), screen_findings=screen.findings)

        if self._use_triage:
            decision = triage(screen.safe_text)
            if not decision.extract:
                logger.info("extraction_triaged_out", extra={"reason": decision.reason})
                return ExtractionOutcome(
                    facts=tuple(facts), screen_findings=screen.findings, triaged_out=True
                )

        try:
            result = await self._model.extract(
                document_text=screen.safe_text,
                schema_keys=NARRATIVE_KEYS,
                source_ref=record.record_ref,
                deadline_ms=EXTRACTION_DEADLINE_MS,
            )
        except UpstreamTimeoutError as exc:
            # The columns already extracted still stand. Only the narrative is lost.
            logger.warning("extraction_model_unavailable", extra={"error_code": str(exc.code)})
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=screen.findings,
                model_unavailable_reason="UPSTREAM_TIMEOUT",
            )

        if not result.accepted:
            logger.warning("model_output_rejected", extra={"model_ref": result.model_ref})
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=screen.findings,
                model_unavailable_reason="MODEL_OUTPUT_REJECTED",
            )

        already = {f.canonical_key for f in facts}
        for candidate in result.values:
            if candidate.canonical_key in already:
                # A filed column already settled this attribute. Prose does not
                # get to restate it at a lower confidence.
                continue
            # The 60 characters before the match are what tell a negated phrase
            # from an asserted one.
            preceding = screen.safe_text[
                max(0, candidate.span.start_offset - 60) : candidate.span.start_offset
            ]
            value = coerce_value(
                candidate.canonical_key, candidate.raw_value, preceding_text=preceding
            )
            if value is None:
                logger.info(
                    "extraction_candidate_dropped",
                    extra={"canonical_key": candidate.canonical_key, "reason": "negated"},
                )
                continue
            facts.append(
                self._fact(
                    address_id=address_id,
                    key=candidate.canonical_key,
                    value=value,
                    record=record,
                    snapshot=snapshot,
                    source_type=source_type,
                    ingested_at=ingested_at,
                    confidence=min(MODEL_CONFIDENCE_CEILING, candidate.model_confidence),
                    span=candidate.span,
                    by_model=True,
                )
            )

        return ExtractionOutcome(facts=tuple(facts), screen_findings=screen.findings)

    def _fact(
        self,
        *,
        address_id: str,
        key: str,
        value: Any,
        record: SourceRecord,
        snapshot: SourceSnapshot,
        source_type: SourceType,
        ingested_at: datetime,
        confidence: float,
        span: SourceSpan | None,
        by_model: bool,
    ) -> StructuralFact:
        return StructuralFact(
            # Derived, not minted: re-polling the same record re-derives the
            # same id, so the append-only store recognises it as one fact.
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
            source_snapshot_id=snapshot.snapshot_id,
            source_span=span,
            observed_at=record.observed_at,
            ingested_at=ingested_at,
            confidence=confidence,
            classification=record.classification or Classification.PUBLIC,
            produced_by_agent=self._agent_id,
            produced_by_version=self._agent_version,
            extracted_by_model=by_model,
        )
