"""Vertex AI: the agent runtime, the model client, and the vector index.

Imports of the Google client are deferred to first use, so a fake-mode process
never loads them and a checkout without the ``google`` extra still type-checks
and tests.
"""

from __future__ import annotations

from firstdue.adapters.vertex.model import VertexModelClient, extraction_response_schema
from firstdue.adapters.vertex.runtime import ADKRuntime
from firstdue.adapters.vertex.vectors import VertexVectorIndex

__all__ = [
    "ADKRuntime",
    "VertexModelClient",
    "VertexVectorIndex",
    "extraction_response_schema",
]
