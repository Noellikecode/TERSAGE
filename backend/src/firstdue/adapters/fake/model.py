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
from collections.abc import Mapping
from typing import Any, Final

from firstdue.domain.facts import SourceSpan
from firstdue.domain.keys import Keys
from firstdue.errors import UpstreamTimeoutError
from firstdue.ports.model import ExtractedValue, ExtractionResult, ProseResult

MODEL_REF: Final[str] = "fake-extractor/1"

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
)


class FakeModelClient:
    """Deterministic stand-in with the same contract as the Gemini client."""

    def __init__(self, *, unavailable: bool = False, reject_output: bool = False) -> None:
        self.unavailable = unavailable
        self.reject_output = reject_output
        self.extract_calls = 0
        self.compose_calls = 0
        self.explain_calls = 0

    def _guard(self) -> None:
        if self.unavailable:
            raise UpstreamTimeoutError(
                "model endpoint unavailable",
                details={"model_ref": MODEL_REF},
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
        parts = [f"{key}: {value}" for key, value in sorted(fields.items())]
        text = f"[{template_id}] " + "; ".join(parts) + "."
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
