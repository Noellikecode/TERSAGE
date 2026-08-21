"""Authenticated internal endpoints.

Pub/Sub delivers by pushing to an HTTP endpoint. That makes this route a door
into the fleet's event stream: anyone who can POST here can make the fleet
re-read state and act. So it authenticates first, and if it cannot authenticate
-- no verifier configured -- it refuses every request rather than falling open.

The status codes are the contract with the broker, and they are chosen around
one failure mode: **a poison message that is nacked forever is a queue that
stops moving.**

* ``200`` -- delivered, deduped, or dead-lettered. All three are "do not send
  this again": the dead letter is recorded and redelivering it would only
  produce the same dead letter.
* ``503`` -- the consumer's breaker is open, or another worker holds the claim.
  Genuinely worth handing back later, so Pub/Sub retries with its own backoff.
* ``401`` / ``403`` -- the caller is not the fleet.

Delivery detail returns in the body rather than in the status code, because an
operator debugging a stuck subscription needs to know *which* of those five
outcomes happened.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from firstdue.api.auth import InternalCaller, require_internal_caller
from firstdue.api.dependencies import Caller, require_audit_read
from firstdue.api.routes.health import get_container
from firstdue.container import Container
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.errors import ConfigurationError, NotAuthorizedError, NotFoundError, ValidationError
from firstdue.eventing.deadletter import DeadLetterRecord
from firstdue.eventing.dispatch import DeliveryOutcome, DeliveryStatus
from firstdue.eventing.pubsub_codec import decode_push
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind
from firstdue.reliability.retry import FailureClass
from firstdue.security.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify_signature,
)
from firstdue.services.replay import IncidentReplay

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class DeliveryReport(BaseModel):
    """What happened to one delivery, per subscriber."""

    model_config = ConfigDict(extra="forbid")

    subscriber: str
    status: DeliveryStatus
    attempts: int
    error_code: str | None = None
    backoffs_ms: list[int] = Field(default_factory=list)

    @classmethod
    def of(cls, outcome: DeliveryOutcome) -> DeliveryReport:
        return cls(
            subscriber=outcome.subscriber,
            status=outcome.status,
            attempts=outcome.attempts,
            error_code=outcome.error_code,
            backoffs_ms=list(outcome.backoffs_ms),
        )


class PushResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    event_id: str | None = None
    topic: Topic | None = None
    deliveries: list[DeliveryReport] = Field(default_factory=list)
    #: Set when the message could not be parsed into an envelope at all.
    poison_reason: str | None = None


class DeadLetterView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    topic: Topic
    subscriber: str
    attempts: int
    reason: str
    failure_class: FailureClass
    dead_lettered_at: str
    correlation_id: str

    @classmethod
    def of(cls, record: DeadLetterRecord) -> DeadLetterView:
        return cls(
            event_id=record.envelope.event_id,
            topic=record.envelope.topic,
            subscriber=record.subscriber,
            attempts=record.attempts,
            reason=record.reason,
            failure_class=record.failure_class,
            dead_lettered_at=record.dead_lettered_at.isoformat(),
            correlation_id=record.envelope.correlation_id,
        )


class DeadLetterListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dead_letters: list[DeadLetterView]
    count: int


def _dead_letter_store(container: Container) -> Any:
    """The dead-letter queue, whichever bus this process is running."""
    return getattr(container.bus, "dead_letter_store", None)


@router.post(
    "/events/push",
    response_model=PushResponse,
    summary="Pub/Sub push delivery",
    responses={
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Not processed; the broker should redeliver."},
    },
)
async def push_event(
    body: dict[str, Any],
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[InternalCaller, Depends(require_internal_caller)],
    subscriber: Annotated[str | None, Query(max_length=120)] = None,
) -> PushResponse:
    """Accept one pushed envelope and route it to local subscribers."""
    try:
        envelope: EventEnvelope = decode_push(body)
    except ValidationError as exc:
        # Unparseable: recorded as poison and acked. Nacking it would guarantee
        # the same bytes arrive again forever.
        logger.error(
            "push_undecodable",
            extra={"caller": caller.subject, "reason": str(exc.code)},
        )
        store = _dead_letter_store(container)
        if store is not None:
            store_reason = "UNDECODABLE_MESSAGE"
            await store.add(
                DeadLetterRecord(
                    envelope=_placeholder_envelope(container),
                    subscriber=subscriber or "unrouted",
                    attempts=1,
                    reason=store_reason,
                    failure_class=FailureClass.POISON,
                    dead_lettered_at=container.clock.now(),
                )
            )
        return PushResponse(accepted=False, poison_reason="UNDECODABLE_MESSAGE")

    handle = getattr(container.bus, "handle_push", None)
    if handle is None:  # pragma: no cover - both wired buses implement it
        raise ValidationError("this process cannot accept pushed events")

    outcomes: tuple[DeliveryOutcome, ...] = await handle(envelope, subscriber=subscriber)
    reports = [DeliveryReport.of(outcome) for outcome in outcomes]

    if outcomes and not all(outcome.should_ack for outcome in outcomes):
        # Hand it back: a breaker will close, and a competing claim will finish.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return PushResponse(
        accepted=all(outcome.should_ack for outcome in outcomes) if outcomes else True,
        event_id=envelope.event_id,
        topic=envelope.topic,
        deliveries=reports,
    )


def _placeholder_envelope(container: Container) -> EventEnvelope:
    """A minimal envelope standing in for bytes that were not one.

    The dead-letter record needs *something* addressable so the message is
    countable and visible. It carries no claim about what the message said.
    """
    return EventEnvelope(
        event_id=container.ids.new_id("undecodable"),
        topic=Topic.SOURCE_POLL,
        occurred_at=container.clock.now(),
        producer="internal-push",
        producer_version="0.0.0",
        correlation_id=container.ids.new_id("corr"),
        idempotency_key=container.ids.idempotency_key("undecodable"),
    )


class CallbackBody(BaseModel):
    """What a receiving system tells us after it processed a write."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=120)
    external_ref: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/callbacks/write",
    summary="Signed callback from a receiving system",
    responses={401: {"description": "Missing, stale, or invalid signature."}},
)
async def write_callback(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """Accept an acknowledgement from a system we wrote to.

    Authenticated by HMAC signature over the method, path, timestamp, and a hash
    of the body -- not by the caller asserting who it is. The timestamp is
    inside the signed material and carries a freshness window, because a
    signature that never goes stale is a replay waiting to happen.

    A callback changes what a profile says about a filed referral, so a forged
    one would be a way to write to the record from outside.
    """
    secret = container.settings.resolved_callback_secret
    if not secret:
        raise ConfigurationError(
            "no callback secret is configured; this endpoint is refusing traffic",
            details={"mode": container.settings.mode_label},
        )

    raw = await request.body()
    try:
        verify_signature(
            secret=secret,
            method=request.method,
            path=request.url.path,
            body=raw,
            signature=request.headers.get(SIGNATURE_HEADER),
            timestamp=request.headers.get(TIMESTAMP_HEADER),
            now=container.clock.now(),
        )
    except SignatureError as exc:
        # Deliberately opaque: which check failed is not the caller's business.
        logger.warning("callback_rejected", extra={"path": request.url.path})
        raise NotAuthorizedError("callback signature is not valid") from exc

    body = CallbackBody.model_validate_json(raw)
    action = await container.write_actions.get(body.action_id)
    if action is None:
        raise NotFoundError("no such write action", details={"action_id": body.action_id})

    await container.audit.record_event(
        AuditEvent(
            audit_id=container.ids.new_id("audit"),
            kind=AuditEventKind.WRITE_EXECUTED,
            occurred_at=container.clock.now(),
            actor="external-callback",
            target=action.target,
            address_id=action.address_id,
            incident_id=action.incident_id,
            correlation_id=container.ids.new_id("corr"),
            detail={
                "action_id": body.action_id,
                "external_ref": body.external_ref,
                "status": body.status,
                "signed": "true",
            },
        )
    )
    return {"accepted": True, "action_id": body.action_id, "external_ref": body.external_ref}


class SchedulerTickRequest(BaseModel):
    """What Cloud Scheduler sends on each tick."""

    model_config = ConfigDict(extra="forbid")

    district_id: str | None = Field(default=None, max_length=120)
    #: Cloud Scheduler retries on a non-2xx. The tick is idempotent by
    #: construction -- derived ids mean a repeated poll writes nothing new --
    #: so a retry is safe rather than merely tolerated.
    reason: str = Field(default="scheduled", max_length=60)


@router.post(
    "/scheduler/tick",
    summary="Cloud Scheduler tick: run one slow-loop pass",
    responses={401: {"description": "Missing or invalid credentials."}},
)
async def scheduler_tick(
    body: SchedulerTickRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[InternalCaller, Depends(require_internal_caller)],
) -> dict[str, Any]:
    """Drive the slow loop on a schedule.

    Phase 3 built the loop and phase 3's own notes recorded the gap: "`poll`
    runs when something calls it. Cloud Scheduler wiring is not written." This
    is what Cloud Scheduler calls, authenticated as a service the same way the
    Pub/Sub push endpoint is.

    A referral is never auto-approved here. The pass stages it and a human
    files it, exactly as it does when a person triggers the poll.
    """
    from firstdue.demo.scenario import run_slow_loop

    district = body.district_id or container.settings.default_district_id
    report = await run_slow_loop(container, district_id=district, approve=False)
    logger.info(
        "scheduler_tick",
        extra={
            "district_id": district,
            "reason": body.reason,
            "caller": caller.subject,
            "facts": report.facts_written,
            "conflicts": len(report.conflicts),
        },
    )
    return report.model_dump(mode="json")


class MetricsView(BaseModel):
    """The nine metrics, as a snapshot."""

    model_config = ConfigDict(extra="forbid")

    time_to_first_line_p50_ms: float
    time_to_first_line_p95_ms: float
    enriched_brief_latency_p50_ms: float
    enriched_brief_latency_p95_ms: float
    conflicts_per_1000_structures: float
    queue_precision: float
    referral_acceptance: float
    notification_delivery: float
    policy_denials: int
    injection_blocks: int
    model_output_rejections: int
    samples: int


@router.get(
    "/metrics",
    response_model=MetricsView,
    summary="Operational metrics snapshot",
)
async def metrics_snapshot(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_audit_read)],
) -> MetricsView:
    """The same numbers the OpenTelemetry exporter emits.

    Served here as well so the console and the staging smoke test can read them
    without a monitoring backend in the loop.
    """
    from firstdue.observability.metrics import METRICS

    return MetricsView(**METRICS.snapshot().model_dump())


class AuditEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    kind: AuditEventKind
    occurred_at: str
    actor: str
    target: str | None = None
    correlation_id: str
    detail: dict[str, str]


@router.get(
    "/audit/events",
    response_model=list[AuditEventView],
    summary="Immutable audit events",
)
async def list_audit_events(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_audit_read)],
    incident_id: Annotated[str | None, Query(max_length=120)] = None,
    kind: Annotated[AuditEventKind | None, Query()] = None,
    #: The whole point of a correlation id is that one value finds everything
    #: one request caused. Without this filter it is a field you can read but
    #: not search by, which is not the same thing.
    correlation_id: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEventView]:
    """Every recorded decision, exception, block, write, and grant.

    Detail was redacted on the way in, so this endpoint cannot leak what the
    records said even to a caller who is entitled to read the audit log.
    """
    events = await container.audit.list_events(incident_id=incident_id, kind=kind, limit=limit)
    if correlation_id is not None:
        events = tuple(e for e in events if e.correlation_id == correlation_id)
    return [
        AuditEventView(
            audit_id=e.audit_id,
            kind=e.kind,
            occurred_at=e.occurred_at.isoformat(),
            actor=e.actor,
            target=e.target,
            correlation_id=e.correlation_id,
            detail=e.detail,
        )
        for e in events
    ]


class PolicyDecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    agent_id: str
    target: str
    operation: str
    classification: str
    action: str
    rule_id: str
    justification: str
    policy_version: str
    decided_at: str
    decided_by: str


@router.get(
    "/audit/decisions",
    response_model=list[PolicyDecisionView],
    summary="Gateway policy decisions",
)
async def list_policy_decisions(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_audit_read)],
    incident_id: Annotated[str | None, Query(max_length=120)] = None,
    agent_id: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PolicyDecisionView]:
    """Why every access was allowed, derived, withheld, staged, or denied."""
    decisions = await container.audit.list_decisions(
        incident_id=incident_id, agent_id=agent_id, limit=limit
    )
    return [
        PolicyDecisionView(
            decision_id=d.decision_id,
            agent_id=d.agent_id,
            target=d.target,
            operation=str(d.operation),
            classification=str(d.classification),
            action=str(d.action),
            rule_id=d.rule_id,
            justification=d.justification,
            policy_version=d.policy_version,
            decided_at=d.decided_at.isoformat(),
            # A constant on the record: a model can explain a decision, never make one.
            decided_by=d.decided_by,
        )
        for d in decisions
    ]


@router.get(
    "/events/dead-letters",
    response_model=DeadLetterListResponse,
    summary="Dead-lettered envelopes",
)
async def list_dead_letters(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[InternalCaller, Depends(require_internal_caller)],
    subscriber: Annotated[str | None, Query(max_length=120)] = None,
) -> DeadLetterListResponse:
    """Everything the fleet could not process. Surfaced, never dropped."""
    store = _dead_letter_store(container)
    records: list[DeadLetterRecord] = (
        list(await store.list_all(subscriber=subscriber)) if store is not None else []
    )
    return DeadLetterListResponse(
        dead_letters=[DeadLetterView.of(r) for r in records], count=len(records)
    )


class ReplayedEntryView(BaseModel):
    """One reconstructed log entry, as the audit console renders it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    entry_id: str
    entry_type: str
    occurred_at: str
    profile_snapshot_id: str
    agent_versions: dict[str, str] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    intact: bool


class IncidentReplayView(BaseModel):
    """What a commander was shown, reconstructed from what was recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    profile_snapshot_id: str
    #: False when the snapshot the brief was built from is no longer readable.
    #: The replay is still returned: an incomplete reconstruction that says so
    #: is more useful than a refusal.
    snapshot_available: bool
    entries: tuple[ReplayedEntryView, ...] = ()
    agent_versions: dict[str, str] = Field(default_factory=dict)
    policy_versions: tuple[str, ...] = ()
    sealed_at: str | None = None
    #: True when every stored hash still matches its stored content.
    intact: bool
    #: Entries whose stored hash does not match their stored content.
    tampered_sequences: tuple[int, ...] = ()
    #: A hash over the ordered entry hashes. Two replays of an untouched
    #: incident produce the same digest; one changed byte anywhere changes it.
    digest: str


@router.get(
    "/audit/incidents/{incident_id}/replay",
    response_model=IncidentReplayView,
    summary="Replay one incident from its own record",
    responses={404: {"description": "No such incident was ever opened."}},
)
async def replay_incident(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_audit_read)],
) -> IncidentReplayView:
    """Reconstruct what the commander saw, in the order they saw it.

    This is the endpoint a NIOSH investigation or a subpoena reaches for. It
    replays the append-only incident log, checks each entry against its own
    stored hash, and reports the agent and policy versions that were *recorded*
    at the time -- never the ones this build happens to ship today.

    Two tampering shapes are caught by two different checks. Editing an entry's
    content under its own hash fails the per-entry check and lands in
    ``tampered_sequences``. Editing content *and* rehashing it passes that check
    and changes ``digest``, which is why both are returned.

    A tampered log still replays. Refusing to show it would deny an
    investigator the very evidence that something was altered.
    """
    replay = IncidentReplay(
        incidents=container.incidents,
        incident_log=container.incident_log,
        snapshots=container.snapshots,
        audit=container.audit,
    )
    result = await replay.replay(incident_id)
    return IncidentReplayView(
        incident_id=result.incident_id,
        profile_snapshot_id=result.profile_snapshot_id,
        snapshot_available=result.snapshot_available,
        entries=tuple(
            ReplayedEntryView(
                sequence=entry.sequence,
                entry_id=entry.entry_id,
                entry_type=entry.entry_type,
                occurred_at=entry.occurred_at.isoformat(),
                profile_snapshot_id=entry.profile_snapshot_id,
                agent_versions=dict(entry.agent_versions),
                content=dict(entry.content),
                content_hash=entry.content_hash,
                intact=entry.intact,
            )
            for entry in result.entries
        ),
        agent_versions=dict(result.agent_versions),
        policy_versions=result.policy_versions,
        sealed_at=result.sealed_at.isoformat() if result.sealed_at else None,
        intact=result.is_intact,
        tampered_sequences=result.tampered_sequences,
        digest=result.digest,
    )
