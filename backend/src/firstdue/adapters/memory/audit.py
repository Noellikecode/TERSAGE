"""In-memory audit sink.

Detail is redacted on the way in, not on the way out: an audit record that
never held record contents cannot leak them later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from firstdue.domain.policy import PolicyDecision
from firstdue.observability.redaction import redact_mapping
from firstdue.ports.audit import AuditEvent, AuditEventKind


class InMemoryAuditSink:
    def __init__(self) -> None:
        self._decisions: list[PolicyDecision] = []
        self._events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def record_decision(self, decision: PolicyDecision) -> None:
        async with self._lock:
            self._decisions.append(decision)

    async def record_event(self, event: AuditEvent) -> None:
        async with self._lock:
            self._events.append(event.model_copy(update={"detail": redact_mapping(event.detail)}))

    async def list_decisions(
        self,
        *,
        incident_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[PolicyDecision]:
        results = self._decisions
        if incident_id is not None:
            results = [d for d in results if d.incident_id == incident_id]
        if agent_id is not None:
            results = [d for d in results if d.agent_id == agent_id]
        return list(reversed(results))[:limit]

    async def list_events(
        self,
        *,
        incident_id: str | None = None,
        kind: AuditEventKind | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEvent]:
        results = self._events
        if incident_id is not None:
            results = [e for e in results if e.incident_id == incident_id]
        if kind is not None:
            results = [e for e in results if e.kind is kind]
        return list(reversed(results))[:limit]

    @property
    def decision_count(self) -> int:
        return len(self._decisions)

    @property
    def event_count(self) -> int:
        return len(self._events)
