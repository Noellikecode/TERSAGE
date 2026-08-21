"""Turning documents into facts, without letting documents give instructions."""

from __future__ import annotations

from firstdue.extraction.coercion import coerce_value, value_type_for
from firstdue.extraction.extractor import ExtractionOutcome, FactExtractor
from firstdue.extraction.recorded import RecordedModelClient
from firstdue.extraction.screening import ScreenResult, screen_document

__all__ = [
    "ExtractionOutcome",
    "FactExtractor",
    "RecordedModelClient",
    "ScreenResult",
    "coerce_value",
    "screen_document",
    "value_type_for",
]
