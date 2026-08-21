"""Structural facts -- the atomic unit of everything FIRST DUE knows.

Invariants enforced *here*, not by convention elsewhere:

* A fact cannot be constructed without provenance (``source_type``,
  ``source_ref``, ``source_snapshot_id``).
* ``human_verified`` can only be true when the source is a human survey that
  names its survey record. Extraction cannot set it; a model cannot set it.
* Facts are frozen. Correcting a fact means writing a new one and marking the
  old one superseded -- the old one is never edited and never deleted.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firstdue.domain.enums import Classification, SourceTier, SourceType, tier_for
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.values import FactValue
from firstdue.errors import (
    HumanVerificationForbiddenError,
    ProvenanceRequiredError,
    ValidationError,
)


def _require_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes.

    A naive timestamp on an incident record is an ambiguity nobody can resolve
    two years later during a line-of-duty-death investigation.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationError(f"{field_name} must be timezone-aware", details={"field": field_name})
    return value


class SourceSpan(BaseModel):
    """Where in a source document an extracted value came from.

    Required for anything a model extracted: a value a human cannot trace back
    to a span in a filed document is not a fact, it is a claim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    locator: str = Field(min_length=1, max_length=200, description="page/section locator")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    quoted_text: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise ValidationError(
                "source span end_offset must be greater than start_offset",
                details={"start_offset": self.start_offset, "end_offset": self.end_offset},
            )
        return self


class StructuralFact(BaseModel):
    """One provenanced assertion about one address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey
    value: FactValue

    # ---- provenance (all required; see _check_provenance) ----
    source_type: SourceType
    source_ref: str = Field(min_length=1, max_length=400)
    source_snapshot_id: str = Field(min_length=1, max_length=200)
    source_span: SourceSpan | None = None

    observed_at: datetime = Field(description="when the world was this way")
    ingested_at: datetime = Field(description="when FIRST DUE learned it")

    confidence: float = Field(ge=0.0, le=1.0)
    classification: Classification

    human_verified: bool = False
    survey_id: str | None = Field(default=None, max_length=120)

    #: Set when a later fact from the same lineage corrects this one. Facts that
    #: merely *disagree* are never superseded -- disagreement is signal.
    superseded_by: str | None = Field(default=None, max_length=120)

    #: Which agent wrote it, at which pinned version (replay requirement).
    produced_by_agent: str | None = Field(default=None, max_length=120)
    produced_by_version: str | None = Field(default=None, max_length=40)

    #: True when a model read this value out of prose rather than a filed field.
    #: It is what makes the span requirement below checkable: a column in a
    #: dataset is a filing and needs no span, while a value a model read from a
    #: narrative is a claim until it can be traced back to the line it came from.
    extracted_by_model: bool = False

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def _aware(cls, v: datetime, info: object) -> datetime:
        name = getattr(info, "field_name", "timestamp")
        return _require_aware(v, str(name))

    @field_validator("source_ref", "source_snapshot_id")
    @classmethod
    def _non_blank_provenance(cls, v: str) -> str:
        if not v.strip():
            raise ProvenanceRequiredError(
                "a structural fact cannot be constructed without provenance",
                details={"field": "source_ref"},
            )
        return v

    @model_validator(mode="after")
    def _check_human_verification(self) -> Self:
        """Only a survey record may assert human verification."""
        if self.human_verified:
            if self.source_type is not SourceType.HUMAN_SURVEY:
                raise HumanVerificationForbiddenError(
                    "human_verified may only be set by a human survey",
                    details={"source_type": str(self.source_type)},
                )
            if not self.survey_id:
                raise HumanVerificationForbiddenError(
                    "human_verified requires the survey_id that verified it",
                    details={"canonical_key": self.canonical_key},
                )
        return self

    @model_validator(mode="after")
    def _check_extraction_span(self) -> Self:
        """Anything a model read out of prose must cite where it read it.

        The rule keys on ``extracted_by_model`` rather than on the source type,
        because those are different things: a storey count read out of an
        inspection narrative is a claim until it can be traced to the line it
        came from, while the same number in a filed dataset column is a filing
        and has no line to point at. Keying on source type conflated them and
        made a deterministic column read look like an extraction.

        A value a human cannot trace back to where a model read it is a claim,
        not a fact.
        """
        if self.extracted_by_model and self.value.is_known and self.source_span is None:
            raise ProvenanceRequiredError(
                "a model-extracted value must cite a source span",
                details={"canonical_key": self.canonical_key},
            )
        return self

    # ------------------------------------------------------------- behaviour

    @property
    def tier(self) -> SourceTier:
        """Merge precedence tier derived from the source type."""
        return tier_for(self.source_type)

    @property
    def is_known(self) -> bool:
        return self.value.is_known

    @property
    def is_active(self) -> bool:
        return self.superseded_by is None

    def age_seconds(self, now: datetime) -> float:
        """Seconds between observation and ``now`` (never negative)."""
        _require_aware(now, "now")
        return max(0.0, (now - self.observed_at).total_seconds())

    def supersede(self, *, by_fact_id: str) -> StructuralFact:
        """Return a copy marked as corrected by ``by_fact_id``.

        Use only for a correction within the same source lineage (a permit
        amended, a survey redone). Two sources that *disagree* both stay active.
        """
        if not by_fact_id.strip():
            raise ValidationError("superseding fact_id is required")
        return self.model_copy(update={"superseded_by": by_fact_id})


def natural_fact_id(
    *,
    address_id: str,
    canonical_key: str,
    source_ref: str,
    observed_at: datetime,
    rendered_value: str,
) -> str:
    """The id one observation always produces.

    Derived from the observation's natural key -- which building, which
    attribute, which document, when the world was that way, and what it said.
    Re-polling a source therefore re-derives the same id, and the append-only
    fact store recognises the second write as the same fact rather than storing
    a duplicate under a fresh identifier.

    The snapshot id is deliberately *not* part of it. Two pulls of one permit an
    hour apart are two snapshots of one observation, not two observations.
    """
    material = "|".join(
        (address_id, canonical_key, source_ref, observed_at.isoformat(), rendered_value)
    )
    return f"fact_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


#: Sources whose payload is document text. Extraction from these is where a
#: model reads prose, so it is where :attr:`StructuralFact.extracted_by_model`
#: is expected to be set -- and the span requirement then applies.
DOCUMENT_SOURCES: frozenset[SourceType] = frozenset(
    {
        SourceType.PERMIT,
        SourceType.FIRE_INSPECTION,
        SourceType.VIOLATION,
        SourceType.TIER_II,
        SourceType.EPA_RMP,
    }
)
