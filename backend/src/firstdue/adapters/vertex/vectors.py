"""Vertex AI Vector Search, behind the classification gate that already exists.

Semantic recall over what a department knows. The reason this adapter is short
is that the interesting rule is upstream of it: :class:`VectorPayload` in
:mod:`firstdue.domain.vectors` refuses at construction to hold ``PHI`` or
``TIER_II_CONFIDENTIAL``, so a payload that reaches this class has already
proved it may be embedded. This adapter re-checks anyway -- the guard is cheap
and the failure it prevents is a statutory breach.

**Off by default.** A Vector Search index endpoint bills for provisioned serving
nodes whether or not anything queries it, which is not a cost a staging
environment should carry silently. ``VECTOR_SEARCH_ENABLED`` defaults to false
and the Terraform module's endpoint is behind the same switch.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS
from firstdue.domain.vectors import VectorPayload
from firstdue.errors import ClassificationViolationError, ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import source_query_span, source_write_span

logger = get_logger(__name__)

#: The embedding model. Named here so a change is a reviewable diff rather than
#: a silently different vector space.
DEFAULT_EMBEDDING_MODEL: Final[str] = "text-embedding-004"


class VectorMatch(BaseModel):
    """One neighbour, with the id that traces it back to a fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: str = Field(min_length=1, max_length=120)
    distance: float


class VertexVectorIndex:
    """Upserts and queries an index, refusing anything it may not embed."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        index_id: str,
        endpoint_id: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        client: Any | None = None,
    ) -> None:
        if not project_id or not index_id:
            raise ConfigurationError(
                "Vector Search requires GCP_PROJECT_ID and VECTOR_SEARCH_INDEX",
                details={"missing": "vector_search_index"},
            )
        self._project_id = project_id
        self._location = location
        self._index_id = index_id
        self._endpoint_id = endpoint_id
        self._embedding_model = embedding_model
        self._client = client
        self.upserts = 0
        self.queries = 0

    @staticmethod
    def _guard(payload: VectorPayload) -> None:
        """The gate, checked again at the boundary it protects.

        ``VectorPayload`` already refuses these at construction. Re-checking
        here costs a set membership test and covers the case where a payload is
        reconstructed from stored JSON rather than built.
        """
        if payload.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be serialized into a vector payload",
                details={"classification": str(payload.classification)},
            )

    def _embedder(self) -> Any:  # pragma: no cover - live mode only
        if self._client is None:
            try:
                import vertexai
                from vertexai.language_models import TextEmbeddingModel
            except ImportError as exc:
                raise ConfigurationError(
                    "google-cloud-aiplatform is not installed; install the 'google' extra",
                    details={"package": "google-cloud-aiplatform"},
                ) from exc
            vertexai.init(project=self._project_id, location=self._location)
            self._client = TextEmbeddingModel.from_pretrained(self._embedding_model)
        return self._client

    async def upsert(self, payloads: Sequence[VectorPayload]) -> int:
        """Embed and store. Returns how many were written."""
        for payload in payloads:
            self._guard(payload)
        if not payloads:
            return 0

        with source_write_span(target="vertex-vector-search") as span:
            span.set("payloads", len(payloads))
            written = await self._write(payloads)
            self.upserts += written
            span.set("written", written)
            return written

    async def query(self, text: str, *, limit: int = 5) -> tuple[VectorMatch, ...]:
        """Nearest neighbours for a query string."""
        with source_query_span(source_id="vertex-vector-search") as span:
            span.set("limit", limit)
            matches = await self._search(text, limit)
            self.queries += 1
            span.set("matches", len(matches))
            return matches

    async def _write(
        self, payloads: Sequence[VectorPayload]
    ) -> int:  # pragma: no cover - live mode only
        import asyncio

        import google.cloud.aiplatform as aiplatform

        embeddings = await asyncio.to_thread(
            self._embedder().get_embeddings, [p.text for p in payloads]
        )
        index = aiplatform.MatchingEngineIndex(index_name=self._index_id)
        await asyncio.to_thread(
            index.upsert_datapoints,
            datapoints=[
                {"datapoint_id": payload.payload_id, "feature_vector": embedding.values}
                for payload, embedding in zip(payloads, embeddings, strict=True)
            ],
        )
        return len(payloads)

    async def _search(
        self, text: str, limit: int
    ) -> tuple[VectorMatch, ...]:  # pragma: no cover - live mode only
        import asyncio

        import google.cloud.aiplatform as aiplatform

        if not self._endpoint_id:
            # An index with no deployed endpoint cannot be queried. Saying so is
            # better than returning an empty list that reads as "nothing similar".
            raise ConfigurationError(
                "Vector Search has no deployed endpoint; queries are unavailable",
                details={"index_id": self._index_id},
            )

        embedding = (await asyncio.to_thread(self._embedder().get_embeddings, [text]))[0]
        endpoint = aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name=self._endpoint_id)
        response = await asyncio.to_thread(
            endpoint.find_neighbors,
            deployed_index_id=self._index_id,
            queries=[embedding.values],
            num_neighbors=limit,
        )
        return tuple(
            VectorMatch(
                payload_id=neighbour.id,
                address_id=neighbour.id.split(":")[0],
                canonical_key=neighbour.id.split(":")[-1],
                distance=float(neighbour.distance),
            )
            for group in response
            for neighbour in group
        )
