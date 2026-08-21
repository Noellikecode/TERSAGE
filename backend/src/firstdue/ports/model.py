"""Model client protocol -- deliberately only three verbs.

Gemini may **extract** facts into strict schemas, **compose** bounded prose, and
**explain** a deterministic result. There is no ``decide``, no ``rank``, no
``judge``. The absence is the design: a capability that does not exist on the
protocol cannot be reached for under deadline pressure.

Every result carries ``accepted``. Malformed model output is rejected, the
deterministic value stands, and ``model_output_rejected`` goes to the audit log.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.facts import SourceSpan


class ExtractedValue(BaseModel):
    """One candidate value, bound to the span of source text that produced it.

    A value a human cannot trace back to a span in a filed document is not a
    fact, it is a claim -- so the span is required.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_key: str = Field(min_length=1, max_length=120)
    raw_value: str = Field(min_length=1, max_length=2000)
    span: SourceSpan
    model_confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """Structured output contract. ``unknowns`` and ``conflicts_noted`` are required.

    A model that must name what it could not determine cannot quietly fill an
    UNKNOWN with a plausible guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: tuple[ExtractedValue, ...]
    #: Attributes the document did not settle. Required field, may be empty.
    unknowns: tuple[str, ...]
    #: Disagreements the model noticed. Advisory only -- the deterministic
    #: engine decides whether a conflict exists.
    conflicts_noted: tuple[str, ...]

    accepted: bool = True
    rejection_reason: str | None = Field(default=None, max_length=300)
    model_ref: str = Field(min_length=1, max_length=120)


class ProseResult(BaseModel):
    """Bounded prose. Length is capped by the caller, not by the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=20_000)
    accepted: bool = True
    rejection_reason: str | None = Field(default=None, max_length=300)
    model_ref: str = Field(min_length=1, max_length=120)
    truncated: bool = False


@runtime_checkable
class ModelClient(Protocol):
    async def extract(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        source_ref: str,
        deadline_ms: int,
    ) -> ExtractionResult:
        """Extract typed values, each bound to a source span."""
        ...

    async def compose(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        """Compose prose from already-resolved fields. Invents nothing."""
        ...

    async def explain(
        self,
        *,
        deterministic_result: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        """Narrate a result the deterministic engine already produced."""
        ...
