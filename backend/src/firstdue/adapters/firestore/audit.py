"""Firestore audit sink.

Detail is redacted on the way in, not on the way out: an audit record that never
held record contents cannot leak them later, whatever queries someone writes
against the collection afterwards.

Policy decisions and audit events live in separate collections because they
answer separate questions -- "was this allowed, and under which rule" versus
"what did the fleet do" -- and an investigator reads them separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from firstdue.adapters.firestore.client import FirestoreConfig, document_store
from firstdue.adapters.firestore.codec import decode_all, encode
from firstdue.domain.policy import PolicyDecision
from firstdue.observability.redaction import redact_mapping
from firstdue.ports.audit import AuditEvent, AuditEventKind


class FirestoreAuditSink:
    """Durable, redacted, correlated audit records."""

    def __init__(self, client: Any, config: FirestoreConfig) -> None:
        self._client = client
        self._config = config

    def _decisions(self) -> Any:
        return document_store(self._client, self._config, "policy_decisions")

    def _events(self) -> Any:
        return document_store(self._client, self._config, "audit_events")

    async def record_decision(self, decision: PolicyDecision) -> None:
        await self._decisions().put(
            decision.decision_id,
            encode(
                decision,
                decision_id=decision.decision_id,
                incident_id=decision.incident_id,
                agent_id=decision.agent_id,
                action=str(decision.action),
            ),
        )

    async def record_event(self, event: AuditEvent) -> None:
        redacted = event.model_copy(
            update={"detail": {k: str(v) for k, v in redact_mapping(event.detail).items()}}
        )
        await self._events().put(
            redacted.audit_id,
            encode(
                redacted,
                audit_id=redacted.audit_id,
                incident_id=redacted.incident_id,
                kind=str(redacted.kind),
                correlation_id=redacted.correlation_id,
            ),
        )

    async def list_decisions(
        self,
        *,
        incident_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[PolicyDecision]:
        filters: list[tuple[str, str, Any]] = []
        if incident_id is not None:
            filters.append(("incident_id", "==", incident_id))
        if agent_id is not None:
            filters.append(("agent_id", "==", agent_id))
        documents = await self._decisions().list(filters)
        decisions = sorted(
            decode_all(PolicyDecision, documents),
            key=lambda d: (d.decided_at, d.decision_id),
            reverse=True,
        )
        return decisions[:limit]

    async def list_events(
        self,
        *,
        incident_id: str | None = None,
        kind: AuditEventKind | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEvent]:
        filters: list[tuple[str, str, Any]] = []
        if incident_id is not None:
            filters.append(("incident_id", "==", incident_id))
        if kind is not None:
            filters.append(("kind", "==", str(kind)))
        documents = await self._events().list(filters)
        events = sorted(
            decode_all(AuditEvent, documents),
            key=lambda e: (e.occurred_at, e.audit_id),
            reverse=True,
        )
        return events[:limit]
