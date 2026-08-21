"""Vector payload construction, with a hard classification gate.

Raw sensitive document text never reaches the vector layer. ``PHI`` and
``TIER_II_CONFIDENTIAL`` facts can never be serialized into an embedding
payload -- this is checked at construction, so there is no code path that
embeds them by omission.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS, Classification
from firstdue.domain.facts import StructuralFact
from firstdue.errors import ClassificationViolationError


class VectorPayload(BaseModel):
    """Text destined for semantic recall, plus the metadata needed to trace it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=8000)
    classification: Classification
    source_ref: str = Field(min_length=1, max_length=400)
    observed_at: datetime

    def model_post_init(self, __context: object) -> None:
        if self.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be serialized into a vector payload",
                details={"classification": str(self.classification)},
            )


def build_vector_payload(fact: StructuralFact, *, payload_id: str, text: str) -> VectorPayload:
    """Build a payload from a fact, refusing sensitive classifications.

    Raises:
        ClassificationViolationError: for ``PHI`` and ``TIER_II_CONFIDENTIAL``.
    """
    return VectorPayload(
        payload_id=payload_id,
        address_id=fact.address_id,
        canonical_key=fact.canonical_key,
        text=text,
        classification=fact.classification,
        source_ref=fact.source_ref,
        observed_at=fact.observed_at,
    )
