"""FakeModelClient -- a real, deterministic extractor and composer.

This is not a stub that returns canned success. It does genuine work:
:meth:`extract` scans the document with deterministic patterns and returns
values **bound to the real character offsets** where it found them, so the
source-span invariant is exercised rather than bypassed. :meth:`compose`
renders and truncates real prose against ``max_chars``.

It also fails the way the live client fails: set ``unavailable=True`` and calls
raise :class:`~firstdue.errors.UpstreamTimeoutError`, which is how the
degraded-brief path gets tested without credentials.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

from firstdue.domain.facts import SourceSpan
from firstdue.domain.keys import IntakeKeys, Keys
from firstdue.errors import UpstreamTimeoutError
from firstdue.extraction.triage import NARRATIVE_KEYS
from firstdue.extraction.triage import triage as local_triage
from firstdue.ports.model import (
    ExtractedValue,
    ExtractionResult,
    ProseChunk,
    ProseResult,
    TriageResult,
)

MODEL_REF: Final[str] = "fake-extractor/1"
#: The triage model is a separate, cheaper one in live mode, so the fake names
#: it separately too -- a trace that cannot tell the two apart cannot show that
#: the expensive model was skipped.
TRIAGE_MODEL_REF: Final[str] = "fake-triage/1"

#: Words per streamed chunk. Small enough that a test can see more than one.
FAKE_CHUNK_WORDS: Final[int] = 4

#: Deterministic patterns. Each maps a canonical key to a regex whose first
#: group is the value. Order is fixed so output is reproducible.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (Keys.STORIES, re.compile(r"\b(\d{1,2})[- ]stor(?:y|ey|ies)\b", re.IGNORECASE)),
    (Keys.YEAR_BUILT, re.compile(r"\bbuilt\s+in\s+(\d{4})\b", re.IGNORECASE)),
    (
        Keys.CONSTRUCTION_TYPE,
        re.compile(r"\b(wood[- ]frame|ordinary|heavy timber|type\s+[IV]{1,3})\b", re.IGNORECASE),
    ),
    (
        Keys.LIGHTWEIGHT_TRUSS,
        re.compile(r"\b(lightweight\s+(?:parallel[- ]chord\s+)?truss)\b", re.IGNORECASE),
    ),
    (Keys.SUPPRESSION_SPRINKLERED, re.compile(r"\b(sprinkler(?:ed|\s+system)?)\b", re.IGNORECASE)),
    (
        Keys.EGRESS_OBSTRUCTION,
        re.compile(r"\b(stairwell\s+(?:partially\s+)?obstructed)\b", re.IGNORECASE),
    ),
    (Keys.HAZARD_SOLAR_ARRAY, re.compile(r"\b(solar\s+(?:array|panels?))\b", re.IGNORECASE)),
    # ---- what a 911 caller or a CAD dispatcher says ----
    #
    # Separate rows rather than a second client, because the intake asks the
    # same verb the same way: a document, a closed key set, and a span per
    # value. A key is only tried when the caller asked for it, so these never
    # fire on a permit and the structural rows never fire on a transcript.
    (
        IntakeKeys.REPORTED_OCCUPANCY,
        re.compile(
            r"\b(apartment building|apartment|single[- ]family home|duplex|row house|"
            r"restaurant|warehouse|office building|school|hotel|corner store|garage)\b",
            re.IGNORECASE,
        ),
    ),
    (
        IntakeKeys.ENTRAPMENT_REPORTED,
        re.compile(
            r"\b((?:two|three|four|several|some)?\s?(?:people|persons?|kids|children|"
            r"residents?)\s+(?:are\s+)?still inside|still inside|somebody is inside|"
            r"someone is inside|trapped)\b",
            re.IGNORECASE,
        ),
    ),
    (
        IntakeKeys.HAZMAT_REPORTED,
        re.compile(
            r"\b(propane cylinders|propane tanks?|propane|acetylene|gasoline|diesel|"
            r"natural gas|chlorine|ammonia|pool chemicals|paint thinner)\b",
            re.IGNORECASE,
        ),
    ),
    (
        IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,
        re.compile(
            r"\b((?:ground|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
            r"tenth|eleventh|twelfth|\d{1,2}(?:st|nd|rd|th)?)\s+floor)\b",
            re.IGNORECASE,
        ),
    ),
    (
        IntakeKeys.REPORTED_ALARM_LEVEL,
        re.compile(
            r"\b((?:second|third|fourth|fifth|\d)\s+alarm)\b",
            re.IGNORECASE,
        ),
    ),
    (
        IntakeKeys.ACCESS_NOTE,
        re.compile(
            r"\b(driveway is blocked|street is blocked|gate is locked|locked gate|"
            r"narrow alley|hydrant is blocked|no rear access)\b",
            re.IGNORECASE,
        ),
    ),
)


class FakeModelClient:
    """Deterministic stand-in with the same contract as the Gemini client."""

    def __init__(self, *, unavailable: bool = False, reject_output: bool = False) -> None:
        self.unavailable = unavailable
        self.reject_output = reject_output
        self.extract_calls = 0
        self.compose_calls = 0
        self.explain_calls = 0
        self.triage_calls = 0
        self.compose_stream_calls = 0

    def _guard(self) -> None:
        if self.unavailable:
            raise UpstreamTimeoutError(
                "model endpoint unavailable",
                details={"model_ref": MODEL_REF},
            )

    async def triage(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        deadline_ms: int,
    ) -> TriageResult:
        """The local vocabulary classifier, standing in for Gemma.

        Unlike the other verbs this one does *not* raise when the client is
        marked unavailable: a triage outage must never be able to silence a
        document. It reports ``accepted=False`` and the caller falls back.
        """
        self.triage_calls += 1
        if self.unavailable or self.reject_output:
            return TriageResult(
                extract=True,
                reason="triage unavailable; extracting rather than skipping",
                accepted=False,
                model_ref=TRIAGE_MODEL_REF,
            )
        decision = local_triage(document_text, keys=schema_keys or NARRATIVE_KEYS)
        return TriageResult(
            extract=decision.extract,
            reason=decision.reason,
            candidate_keys=decision.candidate_keys,
            model_ref=TRIAGE_MODEL_REF,
        )

    async def extract(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        source_ref: str,
        deadline_ms: int,
    ) -> ExtractionResult:
        self._guard()
        self.extract_calls += 1

        if self.reject_output:
            return ExtractionResult(
                values=(),
                unknowns=tuple(schema_keys),
                conflicts_noted=(),
                accepted=False,
                rejection_reason="schema validation failed",
                model_ref=MODEL_REF,
            )

        wanted = set(schema_keys)
        values: list[ExtractedValue] = []
        found: set[str] = set()

        for key, pattern in _PATTERNS:
            if wanted and key not in wanted:
                continue
            match = pattern.search(document_text)
            if match is None:
                continue
            start, end = match.span(1)
            values.append(
                ExtractedValue(
                    canonical_key=key,
                    raw_value=match.group(1),
                    span=SourceSpan(
                        locator=source_ref,
                        start_offset=start,
                        end_offset=end,
                        quoted_text=document_text[start:end],
                    ),
                    # Deterministic: longer evidence, marginally more confidence.
                    model_confidence=round(min(0.95, 0.60 + (end - start) / 100.0), 4),
                )
            )
            found.add(key)

        unknowns = tuple(sorted(wanted - found)) if wanted else ()
        return ExtractionResult(
            values=tuple(values),
            unknowns=unknowns,
            conflicts_noted=(),
            accepted=True,
            model_ref=MODEL_REF,
        )

    async def compose_stream(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> AsyncIterator[ProseChunk]:
        """The same prose as :meth:`compose`, in word-sized pieces.

        Deterministic: the same call produces the same chunks in the same
        order, and their concatenation equals what ``compose`` returns. That
        equality is what lets the console render chunks and the record store
        the completed text without the two ever disagreeing.

        A failure ends the stream with no ``final`` chunk. It does not raise --
        a half-composed narrative is withdrawn by the consumer, not by an
        exception unwinding through a fireground stream.
        """
        self.compose_stream_calls += 1
        if self.unavailable or self.reject_output:
            return

        text = self._composed_text(template_id, fields)[:max_chars]
        words = text.split(" ")
        for index in range(0, len(words), FAKE_CHUNK_WORDS):
            chunk = " ".join(words[index : index + FAKE_CHUNK_WORDS])
            is_last = index + FAKE_CHUNK_WORDS >= len(words)
            yield ProseChunk(
                text=chunk if index == 0 else f" {chunk}",
                final=is_last,
                model_ref=MODEL_REF,
            )

    @staticmethod
    def _composed_text(template_id: str, fields: Mapping[str, Any]) -> str:
        parts = [f"{key}: {value}" for key, value in sorted(fields.items())]
        return f"[{template_id}] " + "; ".join(parts) + "."

    async def compose(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        self._guard()
        self.compose_calls += 1
        if self.reject_output:
            # The rejection path the enriched brief has to survive: the caller
            # keeps its deterministic brief and says the narrative is absent.
            return ProseResult(
                text="",
                accepted=False,
                rejection_reason="composed output failed contract validation",
                model_ref=MODEL_REF,
            )
        text = self._composed_text(template_id, fields)
        truncated = len(text) > max_chars
        return ProseResult(
            text=text[:max_chars],
            accepted=True,
            model_ref=MODEL_REF,
            truncated=truncated,
        )

    async def explain(
        self,
        *,
        deterministic_result: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        self._guard()
        self.explain_calls += 1
        rule = deterministic_result.get("rule_id", "unknown-rule")
        detail = "; ".join(
            f"{k}={v}" for k, v in sorted(deterministic_result.items()) if k != "rule_id"
        )
        text = f"Rule {rule} fired. {detail}."
        truncated = len(text) > max_chars
        return ProseResult(
            text=text[:max_chars],
            accepted=True,
            model_ref=MODEL_REF,
            truncated=truncated,
        )
