"""Documents to facts, with provenance and a screen in front of the model.

The order is the design:

1. **Screen.** The document is untrusted input. Recognised injection shapes are
   stripped before anything sees them, and reported. A screen that could not
   run does not become a pass: the document is withheld from the model, and the
   outcome says the screen was down rather than reading like an empty document.
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

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import SourceSpan, StructuralFact, natural_fact_id
from firstdue.errors import UpstreamTimeoutError
from firstdue.extraction.coercion import coerce_value
from firstdue.extraction.screening import screen_document
from firstdue.extraction.triage import NARRATIVE_KEYS, TriageDecision, triage
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import IdGenerator
from firstdue.ports.model import ModelClient
from firstdue.ports.sources import SourceRecord, SourceSnapshot
from firstdue.security.armor import ArmorVerdict, DocumentScreen, LocalInjectionDetector

logger = get_logger(__name__)

#: Confidence assigned to a value a model read out of prose. Deliberately below
#: a filed structured field: prose is evidence, a column is a filing.
MODEL_CONFIDENCE_CEILING: Final[float] = 0.72
EXTRACTION_DEADLINE_MS: Final[int] = 8_000
#: Triage is a cheap call and must stay cheap: a triage that takes as long as
#: an extraction has saved nothing.
TRIAGE_DEADLINE_MS: Final[int] = 1_500


class ExtractionOutcome(BaseModel):
    """Everything one document produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: tuple[StructuralFact, ...] = ()
    #: Injection patterns the screen removed before the model saw the text.
    screen_findings: tuple[str, ...] = ()
    #: Which screen produced the findings. Carried rather than assumed: with
    #: Model Armor configured the answer is not the local detector, and an
    #: audit record naming the wrong screen is worse than one naming none.
    screen: str = Field(default="", max_length=120)
    #: True when triage decided the document was not worth a model call.
    triaged_out: bool = False
    #: Set when the model was unreachable or its output was rejected. The
    #: structured fields were still extracted; only the narrative was lost.
    model_unavailable_reason: str | None = Field(default=None, max_length=200)
    #: Set when the *screen* could not run, and the document was therefore
    #: withheld from the model entirely. Distinct from a document that screened
    #: to nothing: this pass is not reporting that the narrative was empty, it
    #: is reporting that nobody read it. A caller that cannot tell those apart
    #: records "nothing found" about a document nothing looked at.
    screen_unavailable_reason: str | None = Field(default=None, max_length=200)
    #: The document text *after* screening, for anything downstream that stores
    #: or indexes it. Never the raw text: whatever an ingested document tried
    #: to instruct has been removed, and a later semantic query must not be
    #: able to recall the injection attempt.
    screened_text: str | None = Field(default=None, max_length=200_000)

    @property
    def used_model(self) -> bool:
        return (
            not self.triaged_out
            and self.model_unavailable_reason is None
            and self.screen_unavailable_reason is None
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
        screen: DocumentScreen | None = None,
    ) -> None:
        self._ids = ids
        self._model = model
        self._agent_id = agent_id
        self._agent_version = agent_version
        self._use_triage = use_triage
        # The configured screen, not the module function. Live mode composes
        # Model Armor *around* the local detector, and an extractor that called
        # the local one directly would never reach Armor at all -- which is
        # exactly what it did before this argument existed.
        self._screen = screen

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

        verdict = await self._screen_document(record.document_text)
        if verdict.unavailable_reason is not None:
            # No screen ran, so no model sees this text -- the same fail-closed
            # rule the intake follows. The filed columns above still stand: a
            # permit's filing does not stop being true because a prose screen
            # is down. What must not happen is this returning like an ordinary
            # empty pass, because "the screen was down" and "the narrative said
            # nothing" are different claims and only one of them is true.
            logger.warning(
                "extraction_screen_unavailable",
                extra={"record_ref": record.record_ref, "screen": verdict.screen},
            )
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=verdict.findings,
                screen=verdict.screen,
                screen_unavailable_reason=verdict.unavailable_reason,
            )

        if not verdict.safe_text or self._model is None:
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=verdict.findings,
                screen=verdict.screen,
                screened_text=verdict.safe_text or None,
            )

        if self._use_triage:
            decision = await self._triage(verdict.safe_text)
            if not decision.extract:
                logger.info("extraction_triaged_out", extra={"reason": decision.reason})
                return ExtractionOutcome(
                    facts=tuple(facts),
                    screen_findings=verdict.findings,
                    screen=verdict.screen,
                    triaged_out=True,
                    screened_text=verdict.safe_text,
                )

        try:
            result = await self._model.extract(
                document_text=verdict.safe_text,
                schema_keys=NARRATIVE_KEYS,
                source_ref=record.record_ref,
                deadline_ms=EXTRACTION_DEADLINE_MS,
            )
        except UpstreamTimeoutError as exc:
            # The columns already extracted still stand. Only the narrative is lost.
            logger.warning("extraction_model_unavailable", extra={"error_code": str(exc.code)})
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=verdict.findings,
                screen=verdict.screen,
                model_unavailable_reason="UPSTREAM_TIMEOUT",
                screened_text=verdict.safe_text,
            )

        if not result.accepted:
            logger.warning("model_output_rejected", extra={"model_ref": result.model_ref})
            return ExtractionOutcome(
                facts=tuple(facts),
                screen_findings=verdict.findings,
                screen=verdict.screen,
                model_unavailable_reason="MODEL_OUTPUT_REJECTED",
                screened_text=verdict.safe_text,
            )

        already = {f.canonical_key for f in facts}
        for candidate in result.values:
            if candidate.canonical_key in already:
                # A filed column already settled this attribute. Prose does not
                # get to restate it at a lower confidence.
                continue
            # The 60 characters before the match are what tell a negated phrase
            # from an asserted one.
            preceding = verdict.safe_text[
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

        return ExtractionOutcome(
            facts=tuple(facts),
            screen_findings=verdict.findings,
            screen=verdict.screen,
            screened_text=verdict.safe_text,
        )

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

    async def _screen_document(self, document_text: str | None) -> ArmorVerdict:
        """Screen ingested text through whichever screen this process was given.

        With no screen configured this is the local detector, which is what the
        test suite and fake mode run. With Model Armor configured, the verdict
        comes from both: Armor's answer and the local one, two screens with
        different failure modes that a document has to get past.

        The verdict rather than a
        :class:`~firstdue.extraction.screening.ScreenResult`, because only the
        verdict can say that *no* screen ran -- and that is the one outcome this
        pass must not confuse with a document that screened to nothing.
        """
        if self._screen is None:
            result = screen_document(document_text)
            return ArmorVerdict(
                safe_text=result.safe_text,
                blocked=result.blocked,
                findings=result.findings,
                screen=LocalInjectionDetector.screen_name,
            )
        return await self._screen.inspect(document_text)

    async def _triage(self, text: str) -> TriageDecision:
        """Ask the cheap model, and fall back to the local classifier.

        The failure path answers *extract* -- always. A triage that cannot run
        must not be able to silence a document: the expensive model runs and
        the document is read. The only thing a broken triage costs is money.
        """
        local = triage(text)
        if self._model is None:
            return local

        try:
            verdict = await self._model.triage(
                document_text=text,
                schema_keys=NARRATIVE_KEYS,
                deadline_ms=TRIAGE_DEADLINE_MS,
            )
        except UpstreamTimeoutError as exc:
            logger.info(
                "triage_model_unavailable",
                extra={"error_code": str(exc.code), "fallback": "local"},
            )
            return local

        if not verdict.accepted:
            logger.info("triage_model_rejected", extra={"fallback": "local"})
            return local

        # Two screens, and a document only skips if *both* say skip. The cheap
        # model is allowed to save a call, never to hide a document the local
        # vocabulary check thought was worth reading.
        if not verdict.extract and not local.extract:
            return TriageDecision(
                extract=False,
                reason=verdict.reason,
                candidate_keys=verdict.candidate_keys,
            )
        return TriageDecision(
            extract=True,
            reason=verdict.reason if verdict.extract else local.reason,
            candidate_keys=tuple(sorted({*verdict.candidate_keys, *local.candidate_keys})),
        )
