"""Model client protocol -- deliberately only four verbs.

Gemini may **extract** facts into strict schemas, **compose** bounded prose, and
**explain** a deterministic result. Gemma may **triage** a document: decide
whether it is worth an extraction at all. There is no ``decide``, no ``rank``,
no ``judge``. The absence is the design: a capability that does not exist on
the protocol cannot be reached for under deadline pressure.

``triage`` is the one verb whose *failure* is safe, and it is safe by
construction: it can only ever answer "skip this" or "look at this", and a
wrong answer either way costs one model call. It can never put a fact in front
of an officer, which is why a cheap model is allowed to make the call at all.

Every result carries ``accepted``. Malformed model output is rejected, the
deterministic value stands, and ``model_output_rejected`` goes to the audit log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
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


class TriageResult(BaseModel):
    """Whether a document is worth an extraction call.

    ``extract=True`` is the safe answer and therefore the default on every
    failure path. A triage that wrongly says yes costs one extraction; a triage
    that wrongly says no costs one document nobody read -- so when the cheap
    model is unavailable, unsure, or malformed, the answer is yes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    extract: bool
    reason: str = Field(min_length=1, max_length=300)
    #: Keys the document appears to speak to. Advisory: the extractor still
    #: asks for the full schema, because a triage model narrowing the schema
    #: could silently drop an attribute nobody notices is missing.
    candidate_keys: tuple[str, ...] = ()
    accepted: bool = True
    model_ref: str = Field(min_length=1, max_length=120)


class ProseChunk(BaseModel):
    """One fragment of prose, as it is being composed.

    A chunk is **provisional**. It is prose arriving before the composition it
    belongs to has been accepted, and it carries no facts, no values, and no
    conclusions -- only the words of a narrative whose final, persisted form
    may still be refused. A consumer that renders chunks has to be able to
    retract them, which is why ``final`` exists and why the accumulated text is
    always a prefix of what the completed call returns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=20_000)
    #: True on the last chunk of an accepted composition. A stream that ends
    #: without one ended in a refusal or a timeout, and whatever was shown must
    #: be withdrawn.
    final: bool = False
    model_ref: str = Field(min_length=1, max_length=120)


@runtime_checkable
class ModelClient(Protocol):
    async def triage(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        deadline_ms: int,
    ) -> TriageResult:
        """Decide whether this document is worth an extraction call."""
        ...

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

    def compose_stream(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> AsyncIterator[ProseChunk]:
        """Compose the same prose, emitted as it is produced.

        Same template, same fields, same bound as :meth:`compose`. The only
        difference is when the caller hears about it: a commander watching a
        brief fill in is being told the system is working, and four seconds of
        nothing looks identical to four seconds of broken.

        The stream yields chunks and ends. It does not raise on a model failure
        -- it simply ends without a ``final`` chunk, which is the signal to
        withdraw whatever was shown.
        """
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
