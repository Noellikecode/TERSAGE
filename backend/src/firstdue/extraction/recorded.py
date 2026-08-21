"""Recorded model responses.

Fake mode must produce the same facts on every run, and it must do so without a
model. :class:`FakeModelClient` already extracts deterministically, but a
*recorded* response is stronger: it pins the exact output a given document
produced, so a change in the extractor shows up as a diff rather than as a
quietly different demo.

Lookup is by a hash of the request -- document text, schema keys, and the verb.
A hit replays the stored response. A miss delegates to the inner client and,
when recording is enabled, writes the response to the cassette directory for
next time.

Nothing here talks to a model. The inner client does, and in fake mode the inner
client is the deterministic one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final

from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS
from firstdue.observability.tracing import Span, model_invoke_span
from firstdue.ports.model import (
    ExtractionResult,
    ModelClient,
    ProseChunk,
    ProseResult,
    TriageResult,
)

logger = get_logger(__name__)

CASSETTE_DIR: Final[str] = "model-responses"


def request_digest(verb: str, *parts: str) -> str:
    """A stable id for one model request."""
    material = "|".join((verb, *parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _record_outcome(active: Span, result: ExtractionResult | ProseResult) -> None:
    """One place decides what a rejection looks like in telemetry.

    A rejected output is a normal, expected result -- the deterministic facts
    stand and the brief degrades -- so it is a counted outcome rather than an
    error. It still has to be *visible*, because a model that starts failing
    its schema is a real regression that produces no exceptions at all.
    """
    active.set("model.ref", result.model_ref)
    if result.accepted:
        return
    METRICS.record_model_rejection()
    # The reason is a code the extractor chose ("schema_invalid", "timeout"),
    # never the output that failed -- that output is the untrusted text.
    active.set_rejected(result.rejection_reason or "model_output_rejected")


class RecordedModelClient:
    """Replays recorded model responses, delegating on a miss."""

    def __init__(
        self,
        inner: ModelClient,
        *,
        fixtures_dir: Path,
        record: bool = False,
    ) -> None:
        self._inner = inner
        self._dir = fixtures_dir / CASSETTE_DIR
        self._record = record
        self.replays = 0
        #: What the trace calls the thing behind this wrapper. Class name, not
        #: an endpoint or a project id -- neither belongs in telemetry.
        self.model_ref = type(inner).__name__
        self.misses = 0

    def _path(self, digest: str) -> Path:
        return self._dir / f"{digest}.json"

    def _load(self, digest: str) -> dict[str, Any] | None:
        path = self._path(digest)
        if not path.is_file():
            return None
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def _store(self, digest: str, payload: Mapping[str, Any]) -> None:
        if not self._record:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(digest).write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def compose_stream(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> AsyncIterator[ProseChunk]:
        """Streaming passes through to the inner client.

        A cassette pins the *completed* narrative, which is what the record
        stores and what a replay has to reproduce. Pinning the chunk boundaries
        too would make a cassette assert how a vendor happened to split its
        tokens on the day it was recorded.
        """
        return self._inner.compose_stream(
            template_id=template_id,
            fields=fields,
            max_chars=max_chars,
            deadline_ms=deadline_ms,
        )

    async def triage(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        deadline_ms: int,
    ) -> TriageResult:
        """Triage passes straight through to the inner client.

        Cassettes pin what a document *extracted*, which is the output an
        officer sees. Whether the cheap model chose to skip it is a cost
        decision, not a recorded result, and pinning it would make a cassette
        replay a spend pattern rather than a finding.
        """
        return await self._inner.triage(
            document_text=document_text,
            schema_keys=schema_keys,
            deadline_ms=deadline_ms,
        )

    async def extract(
        self,
        *,
        document_text: str,
        schema_keys: tuple[str, ...],
        source_ref: str,
        deadline_ms: int,
    ) -> ExtractionResult:
        digest = request_digest("extract", document_text, ",".join(sorted(schema_keys)))
        recorded = self._load(digest)
        if recorded is not None:
            self.replays += 1
            return ExtractionResult.model_validate(recorded)

        self.misses += 1
        # The span wraps the miss only. A replay made no call, and a span
        # claiming one would put invented latency into the trace.
        with model_invoke_span(
            model_ref=self.model_ref,
            verb="extract",
            schema_ref="ExtractionResult",
            key_count=len(schema_keys),
            deadline_ms=deadline_ms,
        ) as active:
            result = await self._inner.extract(
                document_text=document_text,
                schema_keys=schema_keys,
                source_ref=source_ref,
                deadline_ms=deadline_ms,
            )
            _record_outcome(active, result)
        self._store(digest, result.model_dump(mode="json"))
        return result

    async def compose(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        digest = request_digest(
            "compose", template_id, json.dumps(dict(fields), sort_keys=True, default=str)
        )
        recorded = self._load(digest)
        if recorded is not None:
            self.replays += 1
            return ProseResult.model_validate(recorded)

        self.misses += 1
        with model_invoke_span(
            model_ref=self.model_ref,
            verb="compose",
            schema_ref="ProseResult",
            deadline_ms=deadline_ms,
        ) as active:
            result = await self._inner.compose(
                template_id=template_id,
                fields=fields,
                max_chars=max_chars,
                deadline_ms=deadline_ms,
            )
            _record_outcome(active, result)
        self._store(digest, result.model_dump(mode="json"))
        return result

    async def explain(
        self,
        *,
        deterministic_result: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        digest = request_digest(
            "explain", json.dumps(dict(deterministic_result), sort_keys=True, default=str)
        )
        recorded = self._load(digest)
        if recorded is not None:
            self.replays += 1
            return ProseResult.model_validate(recorded)

        self.misses += 1
        with model_invoke_span(
            model_ref=self.model_ref,
            verb="explain",
            schema_ref="ProseResult",
            deadline_ms=deadline_ms,
        ) as active:
            result = await self._inner.explain(
                deterministic_result=deterministic_result,
                max_chars=max_chars,
                deadline_ms=deadline_ms,
            )
            _record_outcome(active, result)
        self._store(digest, result.model_dump(mode="json"))
        return result
