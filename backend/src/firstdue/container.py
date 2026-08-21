"""Composition root.

One place decides which implementation of every port the process uses.

Two switches, deliberately independent:

* ``USE_FAKE_AGENTS`` chooses between deterministic adapters and Google-backed
  agent, model, and source adapters.
* ``STORAGE_BACKEND`` and ``EVENT_BACKEND`` choose durable memory and event
  transport. These are separate from fake mode on purpose -- the Firestore
  repositories and the Pub/Sub codec run against local emulators with no
  credentials at all, which is how they are tested.

There is no partial mode. A process that quietly fell back to an in-memory
repository because Firestore was unreachable would lose every write on restart
while reporting success, so a missing adapter is a startup failure rather than a
downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from firstdue.adapters.clock import (
    DeterministicIdGenerator,
    RandomIdGenerator,
    SteppingClock,
    SystemClock,
)
from firstdue.adapters.fake.model import FakeModelClient
from firstdue.adapters.fake.office import FakeCalendar, FakeMailer, FakeObjectStore
from firstdue.adapters.fake.runtime import FakeRuntime
from firstdue.adapters.fake.sources import InMemorySourceRegistry
from firstdue.adapters.fake.writes import FakeWriteTarget
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.adapters.memory.bus import InMemoryEventBus
from firstdue.adapters.memory.repositories import (
    InMemoryAgentRunRepository,
    InMemoryApprovalRepository,
    InMemoryCompensationRepository,
    InMemoryConflictRepository,
    InMemoryFactRepository,
    InMemoryGrantRepository,
    InMemoryIdempotencyRepository,
    InMemoryIncidentLogRepository,
    InMemoryIncidentRepository,
    InMemoryLockRepository,
    InMemoryProfileRepository,
    InMemoryQueueRepository,
    InMemoryReferralRepository,
    InMemoryRegistryRepository,
    InMemorySnapshotRepository,
    InMemorySurveyRepository,
    InMemoryWriteActionRepository,
)
from firstdue.adapters.pubsub.bus import PubSubEventBus
from firstdue.city.san_francisco import SanFranciscoAdapter
from firstdue.domain.enums import Department
from firstdue.errors import ConfigurationError
from firstdue.eventing.dispatch import RepositoryDedupeStore
from firstdue.extraction.recorded import RecordedModelClient
from firstdue.gateway.engine import PolicyEngine
from firstdue.observability.metrics import configure_metrics
from firstdue.observability.tracing import configure_tracing
from firstdue.ports.audit import AuditSink
from firstdue.ports.bus import EventBus
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.model import ModelClient
from firstdue.ports.office import CalendarClient, MailClient, ObjectStore
from firstdue.ports.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    CompensationRepository,
    ConflictRepository,
    FactRepository,
    GrantRepository,
    IdempotencyRepository,
    IncidentLogRepository,
    IncidentRepository,
    LockRepository,
    ProfileRepository,
    QueueRepository,
    ReferralRepository,
    RegistryRepository,
    SnapshotRepository,
    SurveyRepository,
    WriteActionRepository,
)
from firstdue.ports.runtime import AgentRuntime
from firstdue.ports.sources import SourceAdapter, SourceRegistry
from firstdue.ports.writes import ExternalWriteTarget
from firstdue.reliability.retry import RetryPolicy
from firstdue.security.armor import LocalInjectionDetector, ModelArmorClient, build_screen
from firstdue.settings import EventBackend, Settings, StorageBackend
from firstdue.sources.catalog import build_sources

#: The five systems FIRST DUE writes into.
WRITE_TARGET_IDS: tuple[tuple[str, Department, str], ...] = (
    ("building-referral-intake", Department.BUILDING, "REF"),
    ("inspection-work-orders", Department.FIRE, "WO"),
    ("preincident-plan-store", Department.FIRE, "PLAN"),
    ("agency-notifications", Department.FIRE, "NOTIF"),
    ("department-rms", Department.FIRE, "RMS"),
)


@dataclass(slots=True)
class Container:
    """Every port, resolved once per process."""

    settings: Settings
    clock: Clock
    ids: IdGenerator
    city: CityAdapter

    profiles: ProfileRepository
    snapshots: SnapshotRepository
    facts: FactRepository
    conflicts: ConflictRepository
    incidents: IncidentRepository
    incident_log: IncidentLogRepository
    registry: RegistryRepository
    grants: GrantRepository
    queue: QueueRepository
    referrals: ReferralRepository
    approvals: ApprovalRepository
    surveys: SurveyRepository
    write_actions: WriteActionRepository
    locks: LockRepository
    idempotency: IdempotencyRepository
    runs: AgentRunRepository
    compensations: CompensationRepository

    bus: EventBus
    audit: AuditSink
    runtime: AgentRuntime
    model: ModelClient
    sources: SourceRegistry
    write_targets: dict[str, ExternalWriteTarget]

    #: The configured source adapters, in catalog order.
    source_adapters: tuple[SourceAdapter, ...]
    calendar: CalendarClient
    mailer: MailClient
    plan_store: ObjectStore

    #: The gateway. Every read and write the fleet performs decides here.
    policy: PolicyEngine
    #: The document screen in front of every model call.
    screen: LocalInjectionDetector | ModelArmorClient

    @property
    def mode(self) -> str:
        return self.settings.mode_label

    @property
    def storage_label(self) -> str:
        return str(self.settings.storage_backend)

    @property
    def event_label(self) -> str:
        return str(self.settings.event_backend)

    @property
    def lock_lease(self) -> timedelta:
        return timedelta(seconds=self.settings.lock_lease_seconds)

    @property
    def policy_version(self) -> str:
        return self.policy.policy_version

    def source(self, source_id: str) -> SourceAdapter | None:
        for adapter in self.source_adapters:
            if adapter.source_id == source_id:
                return adapter
        return None


def _parse_epoch(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "DEMO_EPOCH must be an ISO-8601 timestamp", details={"value": raw}
        ) from exc
    if parsed.tzinfo is None:
        raise ConfigurationError("DEMO_EPOCH must be timezone-aware", details={"value": raw})
    return parsed


def _retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.event_max_attempts,
        base_delay_ms=settings.event_base_delay_ms,
        max_delay_ms=settings.event_max_delay_ms,
        jitter_ratio=settings.event_jitter_ratio,
    )


@dataclass(slots=True)
class Stores:
    """Every durable repository, resolved together.

    Grouped so the two backends are chosen once rather than seventeen times, and
    so a half-Firestore, half-memory process is not expressible. The contract
    suite builds these directly, which is how it holds both backends to one set
    of behaviours.
    """

    profiles: ProfileRepository
    snapshots: SnapshotRepository
    facts: FactRepository
    conflicts: ConflictRepository
    incidents: IncidentRepository
    incident_log: IncidentLogRepository
    registry: RegistryRepository
    grants: GrantRepository
    queue: QueueRepository
    referrals: ReferralRepository
    approvals: ApprovalRepository
    surveys: SurveyRepository
    write_actions: WriteActionRepository
    locks: LockRepository
    idempotency: IdempotencyRepository
    runs: AgentRunRepository
    compensations: CompensationRepository
    audit: AuditSink


def build_memory_stores() -> Stores:
    return Stores(
        profiles=InMemoryProfileRepository(),
        snapshots=InMemorySnapshotRepository(),
        facts=InMemoryFactRepository(),
        conflicts=InMemoryConflictRepository(),
        incidents=InMemoryIncidentRepository(),
        incident_log=InMemoryIncidentLogRepository(),
        registry=InMemoryRegistryRepository(),
        grants=InMemoryGrantRepository(),
        queue=InMemoryQueueRepository(),
        referrals=InMemoryReferralRepository(),
        approvals=InMemoryApprovalRepository(),
        surveys=InMemorySurveyRepository(),
        write_actions=InMemoryWriteActionRepository(),
        locks=InMemoryLockRepository(),
        idempotency=InMemoryIdempotencyRepository(),
        runs=InMemoryAgentRunRepository(),
        compensations=InMemoryCompensationRepository(),
        audit=InMemoryAuditSink(),
    )


def build_firestore_stores(settings: Settings) -> Stores:
    """Wire the Firestore repositories.

    Imported here rather than at module scope so a memory-backed process never
    loads the Google client at all.
    """
    from firstdue.adapters.firestore import (
        FirestoreAgentRunRepository,
        FirestoreApprovalRepository,
        FirestoreAuditSink,
        FirestoreCompensationRepository,
        FirestoreConfig,
        FirestoreConflictRepository,
        FirestoreFactRepository,
        FirestoreGrantRepository,
        FirestoreIdempotencyRepository,
        FirestoreIncidentLogRepository,
        FirestoreIncidentRepository,
        FirestoreLockRepository,
        FirestoreProfileRepository,
        FirestoreQueueRepository,
        FirestoreReferralRepository,
        FirestoreRegistryRepository,
        FirestoreSnapshotRepository,
        FirestoreSurveyRepository,
        FirestoreWriteActionRepository,
        build_client,
    )

    if not settings.gcp_project_id:  # pragma: no cover - settings validate this first
        raise ConfigurationError("Firestore storage requires GCP_PROJECT_ID")

    config = FirestoreConfig(
        project_id=settings.gcp_project_id,
        database=settings.firestore_database,
        namespace=settings.firestore_namespace,
    )
    client = build_client(config)
    return Stores(
        profiles=FirestoreProfileRepository(client, config),
        snapshots=FirestoreSnapshotRepository(client, config),
        facts=FirestoreFactRepository(client, config),
        conflicts=FirestoreConflictRepository(client, config),
        incidents=FirestoreIncidentRepository(client, config),
        incident_log=FirestoreIncidentLogRepository(client, config),
        registry=FirestoreRegistryRepository(client, config),
        grants=FirestoreGrantRepository(client, config),
        queue=FirestoreQueueRepository(client, config),
        referrals=FirestoreReferralRepository(client, config),
        approvals=FirestoreApprovalRepository(client, config),
        surveys=FirestoreSurveyRepository(client, config),
        write_actions=FirestoreWriteActionRepository(client, config),
        locks=FirestoreLockRepository(client, config),
        idempotency=FirestoreIdempotencyRepository(client, config),
        runs=FirestoreAgentRunRepository(client, config),
        compensations=FirestoreCompensationRepository(client, config),
        audit=FirestoreAuditSink(client, config),
    )


def _build_bus(settings: Settings, *, clock: Clock, stores: Stores) -> EventBus:
    """Choose the event transport.

    Both buses run the same dispatcher. The Pub/Sub one dedupes through the
    durable idempotency store, because Pub/Sub redelivers across process
    restarts and a dedupe set in memory forgets everything when Cloud Run
    replaces the instance.
    """
    policy = _retry_policy(settings)
    if settings.event_backend is EventBackend.MEMORY:
        return InMemoryEventBus(max_attempts=policy.max_attempts, clock=clock, policy=policy)
    if not settings.gcp_project_id:  # pragma: no cover - settings validate this first
        raise ConfigurationError("Pub/Sub events require GCP_PROJECT_ID")
    return PubSubEventBus(
        project_id=settings.gcp_project_id,
        topic_prefix=settings.pubsub_topic_prefix,
        clock=clock,
        policy=policy,
        dedupe=RepositoryDedupeStore(stores.idempotency),
    )


def _build_model(settings: Settings, *, secrets: object | None = None) -> ModelClient:
    """The model client, wrapped so recorded responses still replay.

    Live mode puts Gemini *inside* the cassette layer rather than beside it, so
    a recorded response is still a recorded response and a miss reaches Vertex.
    """
    inner: ModelClient
    if settings.use_fake_agents:
        inner = FakeModelClient()
    else:
        from firstdue.adapters.vertex.model import VertexModelClient

        if not settings.vertex_configured:
            raise ConfigurationError(
                "live mode requires GCP_PROJECT_ID, VERTEX_LOCATION and GEMINI_MODEL",
                details={"missing": "vertex configuration"},
            )
        inner = VertexModelClient(
            project_id=settings.gcp_project_id or "",
            location=settings.vertex_location,
            model=settings.gemini_model,
            policy=_retry_policy(settings),
        )
    return RecordedModelClient(
        inner,
        fixtures_dir=settings.fixtures_dir,
        record=settings.record_model_responses,
    )


def _build_office(
    settings: Settings, *, clock: Clock, ids: IdGenerator, stores: Stores
) -> tuple[CalendarClient, MailClient, ObjectStore]:
    """Calendar, mail, and the plan store.

    The live three dedupe through the durable idempotency repository, not a
    process-local dict -- a restart must not double-book a company.
    """
    if settings.use_fake_agents:
        return (
            FakeCalendar(clock=clock, ids=ids),
            FakeMailer(clock=clock),
            FakeObjectStore(
                bucket=settings.gcs_plans_bucket or "firstdue-plans-local", clock=clock
            ),
        )

    from firstdue.adapters.google.office import (
        GmailClient,
        GoogleCalendarClient,
        GoogleObjectStore,
    )

    if not settings.gcs_plans_bucket:  # pragma: no cover - settings validate first
        raise ConfigurationError("live mode requires GCS_PLANS_BUCKET")
    return (
        GoogleCalendarClient(clock=clock, idempotency=stores.idempotency),
        GmailClient(
            sender=f"firstdue@{settings.municipality_id}",
            clock=clock,
            idempotency=stores.idempotency,
        ),
        GoogleObjectStore(bucket=settings.gcs_plans_bucket, clock=clock),
    )


def _build_runtime(settings: Settings, *, clock: Clock, ids: IdGenerator) -> AgentRuntime:
    if settings.use_fake_agents:
        return FakeRuntime(clock=clock, ids=ids)

    from firstdue.adapters.vertex.runtime import ADKRuntime

    return ADKRuntime(
        clock=clock,
        ids=ids,
        project_id=settings.gcp_project_id or "",
        location=settings.vertex_location,
    )


def build_container(settings: Settings) -> Container:
    """Wire the process according to its mode.

    There is no partial live mode. A live process that quietly used a
    deterministic id generator, or a process-local dedupe, or a fixture where a
    feed should be, would be a system that lies about where its data came from
    -- so every live piece is either wired or the process refuses to start.
    """
    configure_tracing(
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        project_id=settings.gcp_project_id if not settings.use_fake_agents else None,
    )
    configure_metrics(enabled=settings.otel_enabled, service_name=settings.otel_service_name)

    # Determinism is a property of the demo, not of the system. Live mode reads
    # the wall clock and mints random ids: two Cloud Run instances sharing a
    # seeded counter would mint the same fact id for different facts.
    clock: Clock
    ids: IdGenerator
    if settings.use_fake_agents:
        clock = SteppingClock(_parse_epoch(settings.demo_epoch), step=timedelta(milliseconds=50))
        ids = DeterministicIdGenerator(settings.demo_seed)
    else:
        clock, ids = build_live_clock_and_ids()

    city: CityAdapter = SanFranciscoAdapter(settings.fixtures_dir)

    stores = (
        build_firestore_stores(settings)
        if settings.storage_backend is StorageBackend.FIRESTORE
        else build_memory_stores()
    )

    sources = build_sources(
        fixtures_dir=settings.fixtures_dir, clock=clock, live=not settings.use_fake_agents
    )
    source_registry = InMemorySourceRegistry(sources)

    # Recorded responses pin what each document extracted, so a change in the
    # extractor shows up as a diff rather than as a quietly different demo.
    model: ModelClient = _build_model(settings)

    write_targets: dict[str, ExternalWriteTarget] = {
        target_id: FakeWriteTarget(
            target_id=target_id,
            receiving_department=department,
            clock=clock,
            ids=ids,
            external_ref_prefix=prefix,
        )
        for target_id, department, prefix in WRITE_TARGET_IDS
    }

    office = _build_office(settings, clock=clock, ids=ids, stores=stores)

    return Container(
        settings=settings,
        clock=clock,
        ids=ids,
        city=city,
        profiles=stores.profiles,
        snapshots=stores.snapshots,
        facts=stores.facts,
        conflicts=stores.conflicts,
        incidents=stores.incidents,
        incident_log=stores.incident_log,
        registry=stores.registry,
        grants=stores.grants,
        queue=stores.queue,
        referrals=stores.referrals,
        approvals=stores.approvals,
        surveys=stores.surveys,
        write_actions=stores.write_actions,
        locks=stores.locks,
        idempotency=stores.idempotency,
        runs=stores.runs,
        compensations=stores.compensations,
        bus=_build_bus(settings, clock=clock, stores=stores),
        audit=stores.audit,
        runtime=_build_runtime(settings, clock=clock, ids=ids),
        model=model,
        sources=source_registry,
        write_targets=write_targets,
        source_adapters=sources,
        policy=PolicyEngine(ids=ids),
        screen=build_screen(
            use_fake=settings.use_fake_agents,
            template=settings.model_armor_template,
            project_id=settings.gcp_project_id,
        ),
        calendar=office[0],
        mailer=office[1],
        plan_store=office[2],
    )


def build_live_clock_and_ids() -> tuple[Clock, IdGenerator]:
    """Live-mode time and identity. Kept here so the seam is visible."""
    return SystemClock(), RandomIdGenerator()
