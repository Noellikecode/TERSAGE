"""Composition root.

One place decides which implementation of every port the process uses.

Two switches, deliberately independent:

* ``USE_FAKE_AGENTS`` chooses between deterministic adapters and Google-backed
  agent, model, and source adapters.
* ``STORAGE_BACKEND`` and ``EVENT_BACKEND`` choose durable memory and event
  transport. These are separate from fake mode on purpose, so the Firestore
  repositories and the Pub/Sub transport can be exercised against real Google
  services without also turning on live models and live municipal sources.
* ``WORKSPACE_WRITES`` chooses whether Calendar and Gmail are called for real.
  Separate again, because those two are the only write targets that need
  delegated *user* authority rather than the deployment's own identity.

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
from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
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
from firstdue.adapters.memory.threads import InMemoryThreadIndex
from firstdue.adapters.memory.vectors import InMemoryVectorIndex
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
from firstdue.ports.fireactivity import FireActivityClient
from firstdue.ports.grounding import GroundingService
from firstdue.ports.imagery import ImageryClient
from firstdue.ports.memory import CheckpointRepository, OpenQuestionRepository
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
from firstdue.ports.threads import ThreadIndex
from firstdue.ports.tiles import TileClient
from firstdue.ports.vectors import VectorIndex
from firstdue.ports.vision import VisionClient
from firstdue.ports.writes import ExternalWriteTarget
from firstdue.reliability.retry import RetryPolicy
from firstdue.security.armor import LocalInjectionDetector, ModelArmorClient, build_screen
from firstdue.services.memory_bank import MemoryBank
from firstdue.settings import (
    EventBackend,
    ImageryProvider,
    Settings,
    StorageBackend,
    WorkspaceWrites,
)
from firstdue.sources.catalog import CentralFetcherFactory, LiveCredentials, build_sources

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
    #: Imagery. Sensor Fusion's own extraction path -- a frame in, observations
    #: bound to image regions out. Separate from ``model`` because it is a
    #: different contract with one verb, not a fifth verb on the text one.
    vision: VisionClient
    #: A photograph of the building, beside the massing model the fleet derived.
    #: Separate from ``vision``: that one *reads* a frame a crew captured, this
    #: one *fetches* one nobody on scene took. Different provider, different
    #: failure mode, and only one of them can be metered by a third party.
    imagery: ImageryClient
    #: Regional fire activity and fire-weather context. Held on the container
    #: for the same reason imagery is: the adapter owns a cache and a token
    #: bucket in front of someone else's quota, and one rebuilt per request
    #: arrives with neither.
    fire_activity: FireActivityClient
    #: The two tile grids the regional terrain mesh is built from. On the
    #: container for the same reason as the two above: it holds a Map Tiles
    #: session, a tile cache and a token bucket, and one rebuilt per request
    #: would mint a session for every square a camera move asks for.
    tiles: TileClient
    vectors: VectorIndex
    sources: SourceRegistry
    write_targets: dict[str, ExternalWriteTarget]

    #: The configured source adapters, in catalog order.
    source_adapters: tuple[SourceAdapter, ...]
    calendar: CalendarClient
    mailer: MailClient
    #: Where an *approved* referral is emailed. Deliberately a separate client
    #: from ``mailer``: notifying a crew and filing against a property owner are
    #: different acts, with different recipients and different consequences, and
    #: a deployment may reasonably want one configured and not the other. Falls
    #: back to ``mailer`` when Resend is unconfigured, so the path is never
    #: silently missing -- only simulated, and the console says which.
    referral_mailer: MailClient
    plan_store: ObjectStore

    #: The gateway. Every read and write the fleet performs decides here.
    policy: PolicyEngine
    #: The document screen in front of every model call.
    screen: LocalInjectionDetector | ModelArmorClient
    #: Durable agent working memory: the questions a pass could not close, and
    #: the graph positions it parked. ``None`` when the bank is switched off,
    #: which is the one honest way to run without it -- an agent that silently
    #: forgot would repeat the same failed work forever and look busy doing it.
    memory: MemoryBank | None
    #: Resolves a fuzzy external reference to a canonical id, or declines. It
    #: never authors a claim about a building; that distinction is what lets it
    #: reach the public web at all.
    grounding: GroundingService

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
    def workspace_label(self) -> str:
        """Whether the survey calendar and crew mail reach Workspace for real.

        Reported so the console can say so. In fake mode the whole fleet is
        simulated and the mode label already carries that; what this
        distinguishes is a *live* deployment in which four integrations are
        real and these two are not.
        """
        if self.settings.use_fake_agents:
            return str(WorkspaceWrites.FAKE)
        return str(self.settings.workspace_writes)

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
    #: Durable agent working memory. Grouped here with every other repository
    #: so a half-Firestore, half-memory process stays inexpressible -- an open
    #: question that outlives a deployment is worth nothing if it lands in a
    #: store that does not.
    open_questions: OpenQuestionRepository
    checkpoints: CheckpointRepository
    #: The Firestore client and its config, when this is a Firestore process.
    #:
    #: Exposed because the central database is *not* a repository -- it is read
    #: through the source seam, by a fetcher the catalog builds -- and opening a
    #: second client for it would mean two connection pools and two places to
    #: get the namespace wrong. ``None`` on a memory-backed process, which is
    #: what makes "no central database in fake mode" a type rather than a rule.
    firestore: tuple[object, object] | None = None


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
        open_questions=InMemoryOpenQuestionRepository(),
        checkpoints=InMemoryCheckpointRepository(),
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
    from firstdue.adapters.firestore.memory_bank import (
        FirestoreCheckpointRepository,
        FirestoreOpenQuestionRepository,
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
        firestore=(client, config),
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
        open_questions=FirestoreOpenQuestionRepository(client, config),
        checkpoints=FirestoreCheckpointRepository(client, config),
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
            # The cheap model that decides whether the expensive one runs.
            triage_model=settings.gemma_model,
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

    The three are built independently, because they do not authenticate the
    same way. Cloud Storage answers to the principal itself, so it goes live
    whenever the rest of the fleet does. Calendar and Gmail act *as a user* and
    need delegated authority Application Default Credentials do not carry, so
    they follow ``WORKSPACE_WRITES`` instead -- see :class:`WorkspaceWrites`.

    The live clients dedupe through the durable idempotency repository, not a
    process-local dict: a restart must not double-book a company.
    """
    if settings.use_fake_agents:
        return (
            FakeCalendar(clock=clock, ids=ids),
            FakeMailer(clock=clock),
            FakeObjectStore(
                bucket=settings.gcs_plans_bucket or "firstdue-plans-local", clock=clock
            ),
        )

    from firstdue.adapters.google.office import GoogleObjectStore

    if not settings.gcs_plans_bucket:  # pragma: no cover - settings validate first
        raise ConfigurationError("live mode requires GCS_PLANS_BUCKET")
    plans = GoogleObjectStore(bucket=settings.gcs_plans_bucket, clock=clock)

    if settings.workspace_writes is WorkspaceWrites.FAKE:
        # Recorded, audited, and idempotent exactly as the live clients are.
        # The console labels both actions simulated; a silently skipped crew
        # notification would be worse than an admitted one.
        return (FakeCalendar(clock=clock, ids=ids), FakeMailer(clock=clock), plans)

    from firstdue.adapters.google.office import GmailClient, GoogleCalendarClient

    return (
        GoogleCalendarClient(clock=clock, idempotency=stores.idempotency),
        GmailClient(
            sender=f"firstdue@{settings.municipality_id}",
            clock=clock,
            idempotency=stores.idempotency,
        ),
        plans,
    )


def _build_vision(settings: Settings) -> VisionClient:
    """Gemini multimodal, or the deterministic double.

    Uses ``GEMINI_MODEL`` rather than a separate setting: the frame reader and
    the document reader are the same model doing the same job on a different
    medium, and a second knob would be a second thing to get wrong.
    """
    if settings.use_fake_agents:
        from firstdue.adapters.fake.vision import FakeVisionClient

        return FakeVisionClient()

    from firstdue.adapters.vertex.vision import VertexVisionClient

    return VertexVisionClient(
        project_id=settings.gcp_project_id or "",
        location=settings.vertex_location,
        model=settings.gemini_model,
    )


def _build_central_fetchers(settings: Settings, *, stores: Stores) -> CentralFetcherFactory | None:
    """A factory for central-database fetchers, or ``None``.

    Returns a callable rather than a set of adapters because the catalog decides
    *which* sources a central collection backs; this only knows how to open one.

    ``None`` is the ordinary answer. With the central database off, the municipal
    sources resolve to a fixture or a live feed exactly as before, which is what
    keeps `make demo` credential-free.
    """
    if not settings.central_database_enabled:
        return None
    if settings.storage_backend is not StorageBackend.FIRESTORE:
        raise ConfigurationError(
            "CENTRAL_DATABASE_ENABLED requires STORAGE_BACKEND=firestore; a "
            "corpus that vanished on restart would not be a database",
            details={"storage_backend": str(settings.storage_backend)},
        )
    from firstdue.adapters.firestore.central import CentralDatabaseFetcher

    if stores.firestore is None:  # pragma: no cover - guarded by the check above
        raise ConfigurationError("the central database needs a Firestore client")
    client, config = stores.firestore
    return lambda collection: CentralDatabaseFetcher(client, config, collection)  # type: ignore[arg-type]


def _build_vectors(settings: Settings) -> VectorIndex:
    """Semantic recall over screened narratives.

    Selected independently of fake mode, like storage and events are: the
    in-memory index is a real second implementation of the same protocol, and
    it is what runs unless Vector Search is both enabled and configured. A
    process that quietly ran with no recall at all would report empty results
    that read as "nothing similar on file".
    """
    if not settings.vector_search_enabled:
        return InMemoryVectorIndex()
    if settings.use_fake_agents:
        raise ConfigurationError(
            "VECTOR_SEARCH_ENABLED requires live mode; the in-memory index is "
            "what fake mode uses and it needs no configuration",
            details={"setting": "VECTOR_SEARCH_ENABLED"},
        )
    if not settings.vector_search_index:
        raise ConfigurationError(
            "VECTOR_SEARCH_ENABLED requires VECTOR_SEARCH_INDEX",
            details={"missing": ["VECTOR_SEARCH_INDEX"]},
        )
    from firstdue.adapters.vertex.vectors import VertexVectorIndex

    return VertexVectorIndex(
        project_id=settings.gcp_project_id or "",
        location=settings.vertex_location,
        index_id=settings.vector_search_index,
        endpoint_id=settings.vector_search_endpoint,
        embedding_model=settings.vector_embedding_model,
    )


def _build_referral_mailer(
    settings: Settings,
    *,
    clock: Clock,
    stores: Stores,
    fallback: MailClient,
) -> MailClient:
    """Resend for approved referrals, or the mailer everything else uses.

    Falls back rather than returning ``None`` because a referral that cannot be
    emailed must still be *recorded as sent somewhere* -- the fake mailer keeps
    the audit trail and the console label honest. A missing key is a documented
    state; a silently skipped notification is not.

    Settings validation already refuses a key without a sender, so reaching
    here with one and not the other is not expressible.
    """
    if settings.use_fake_agents or not settings.resend_api_key:
        return fallback

    from firstdue.adapters.resend.mail import ResendMailClient

    return ResendMailClient(
        api_key=settings.resend_api_key,
        sender=settings.resend_from_address or "",
        clock=clock,
        idempotency=stores.idempotency,
        policy=_retry_policy(settings),
    )


def _build_fire_activity(
    settings: Settings, *, city: CityAdapter, clock: Clock
) -> FireActivityClient:
    """Regional fire activity, delegating the three-way choice to the adapter.

    The bounding boxes are parsed here rather than inside the adapter so a
    malformed box is a startup failure naming the setting, not a refusal an
    officer reads as an outage.
    """
    from firstdue.adapters.nasa import build_fire_activity
    from firstdue.ports.fireactivity import BoundingBox

    return build_fire_activity(
        use_fake=settings.use_fake_agents,
        map_key=settings.firms_map_key,
        city=city,
        clock=clock,
        region=BoundingBox.parse(settings.fire_activity_region),
        city_bounds=BoundingBox.parse(settings.fire_activity_city_bounds),
    )


def _build_imagery(settings: Settings, *, city: CityAdapter, clock: Clock) -> ImageryClient:
    """A photograph of the building, or an adapter that says why there is none.

    Three states, and the third is the one that matters. Fake mode gets a
    deterministic synthetic elevation, watermarked and captioned as generated.
    Live mode with a Maps key gets Street View, falling back to satellite.

    Live mode *without* a key gets an adapter that refuses -- never the
    synthetic one. A drawing that stood in for a photograph on a real
    deployment would be the exact failure this project refuses everywhere else:
    a commander cannot be shown a picture of a building nobody photographed.
    """
    # `IMAGERY_PROVIDER` decides, and it defaults to following fake mode. A team
    # holding a Maps key can set it to `google` and get real Street View and a
    # real satellite tile without taking Vertex, Firestore and every source live
    # in the same move -- which is what flipping `USE_FAKE_AGENTS` does.
    wants_google = settings.imagery_provider is ImageryProvider.GOOGLE or (
        not settings.use_fake_agents
    )
    if not wants_google:
        from firstdue.adapters.fake.imagery import FakeImageryClient

        return FakeImageryClient(city=city)

    from firstdue.adapters.google.imagery import GoogleImageryClient, UnconfiguredImageryClient

    if not settings.google_maps_api_key:
        return UnconfiguredImageryClient()
    return GoogleImageryClient(api_key=settings.google_maps_api_key, city=city, clock=clock)


def _build_tiles(settings: Settings, *, clock: Clock) -> TileClient:
    """Map tiles for the regional terrain mesh. Three states, like imagery.

    Follows ``IMAGERY_PROVIDER`` rather than introducing a fourth switch: the
    terrain skin comes from the same Maps key and the same terms as the building
    photograph, and a deployment that has decided about one has decided about
    the other.

    The region is passed in so the live client can refuse tiles outside it. That
    check is what keeps a public console from being an open relay onto the
    department's Map Tiles quota.
    """
    from firstdue.ports.fireactivity import BoundingBox

    region = BoundingBox.parse(settings.fire_activity_region)

    wants_google = settings.imagery_provider is ImageryProvider.GOOGLE or (
        not settings.use_fake_agents
    )
    if not wants_google:
        from firstdue.adapters.fake.tiles import FakeTileClient

        return FakeTileClient(region=region)

    from firstdue.adapters.tiles import GoogleTerrainTileClient, UnconfiguredTileClient

    if not settings.google_maps_api_key:
        return UnconfiguredTileClient()
    return GoogleTerrainTileClient(api_key=settings.google_maps_api_key, region=region, clock=clock)


def _build_memory(settings: Settings, *, stores: Stores, clock: Clock) -> MemoryBank | None:
    """Durable agent working memory, or an honest absence.

    Selected independently of fake mode, like storage and events are: the
    in-memory repositories are a real second implementation of the same
    protocols, and fake mode uses them rather than doing without. A pass that
    could not settle a question needs somewhere to put it in either mode --
    that is the whole component.

    ``None`` is a deliberate state, not a fallback. Every caller treats a bank
    it does not have as "do not open questions", never as "opened and lost".
    """
    if not settings.memory_bank_enabled:
        return None
    return MemoryBank(
        questions=stores.open_questions,
        checkpoints=stores.checkpoints,
        clock=clock,
        threads=_build_threads(settings),
    )


def _build_threads(settings: Settings) -> ThreadIndex:
    """Semantic recall over open question threads.

    Selected the way the vector index is, and for the same reason: the
    in-memory implementation is a real second implementation of the protocol
    rather than a stub, so a process without a Memory Bank engine still recalls
    by meaning -- per-instance and non-durable, but not silently empty.

    The managed index needs live mode because writing a memory embeds it, which
    is a Vertex call. Asking for one in fake mode is a configuration error
    rather than a quiet downgrade: a credential-free process that appeared to
    have a managed memory bank would misreport what it is.
    """
    if not settings.memory_bank_engine_id:
        return InMemoryThreadIndex()
    if settings.use_fake_agents:
        raise ConfigurationError(
            "MEMORY_BANK_ENGINE_ID requires live mode; the in-memory thread "
            "index is what fake mode uses and it needs no configuration",
            details={"setting": "MEMORY_BANK_ENGINE_ID"},
        )
    from firstdue.adapters.vertex.threads import VertexThreadIndex

    return VertexThreadIndex(
        project_id=settings.gcp_project_id or "",
        # Not ``vertex_location``: that is ``global`` for the models, and an
        # Agent Engine instance is regional. See the setting.
        location=settings.memory_bank_location,
        engine_id=settings.memory_bank_engine_id,
    )


def _build_grounding(
    settings: Settings,
    *,
    screen: LocalInjectionDetector | ModelArmorClient,
    clock: Clock,
) -> GroundingService:
    """Reference resolution, and the one path that reaches the public web.

    Three states, not two. Fake mode gets the deterministic double. Live mode
    with grounding enabled gets Gemini with Google Search. Live mode with it
    disabled gets the double in its **unavailable** state, which declines every
    reference with a reason and returns no reports.

    That third state is the one worth explaining: routing a live process to the
    ordinary double would answer from a digest, and a binding derived from
    arithmetic is indistinguishable on the console from one that was retrieved.
    Declining is the truthful answer to "what did the web say" when nobody
    asked the web.
    """
    from firstdue.adapters.fake.grounding import FakeGroundingService

    if settings.use_fake_agents:
        return FakeGroundingService(screen=screen, clock=clock)
    if not settings.grounding_search_enabled:
        return FakeGroundingService(screen=screen, clock=clock, unavailable=True)

    from firstdue.adapters.vertex.grounding import VertexGroundingService

    return VertexGroundingService(
        project_id=settings.gcp_project_id or "",
        location=settings.vertex_location,
        model=settings.gemini_model,
        screen=screen,
        clock=clock,
        policy=_retry_policy(settings),
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
        fixtures_dir=settings.fixtures_dir,
        clock=clock,
        live=not settings.use_fake_agents,
        # Named sources may run live while the rest stay on fixtures. Geometry
        # is the reason: two of its three feeds need no credential, and the
        # massing model is worthless -- worse, it is a "measured height" nobody
        # measured -- while they are synthetic.
        live_source_ids=settings.live_source_ids,
        central=_build_central_fetchers(settings, stores=stores),
        # The city adapter is the only component that knows where an address
        # is, so the point-query sources resolve their coordinates through it.
        city=city,
        credentials=LiveCredentials(
            maps_api_key=settings.google_maps_api_key,
            nrel_api_key=settings.nrel_api_key,
            socrata_app_token=settings.socrata_app_token,
            contact_email=settings.source_contact_email,
        ),
    )
    source_registry = InMemorySourceRegistry(sources)

    # Recorded responses pin what each document extracted, so a change in the
    # extractor shows up as a diff rather than as a quietly different demo.
    model: ModelClient = _build_model(settings)
    vectors: VectorIndex = _build_vectors(settings)

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

    # Built before the container literal because grounding screens every snippet
    # it retrieves, and a web page is the least trustworthy input in the fleet.
    screen = build_screen(
        use_fake=settings.use_fake_agents,
        template=settings.model_armor_template,
        project_id=settings.gcp_project_id,
    )
    memory = _build_memory(settings, stores=stores, clock=clock)
    imagery = _build_imagery(settings, city=city, clock=clock)
    tiles = _build_tiles(settings, clock=clock)
    fire_activity = _build_fire_activity(settings, city=city, clock=clock)
    referral_mailer = _build_referral_mailer(
        settings, clock=clock, stores=stores, fallback=office[1]
    )
    grounding = _build_grounding(settings, screen=screen, clock=clock)

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
        vision=_build_vision(settings),
        imagery=imagery,
        tiles=tiles,
        fire_activity=fire_activity,
        vectors=vectors,
        sources=source_registry,
        write_targets=write_targets,
        source_adapters=sources,
        policy=PolicyEngine(ids=ids),
        screen=screen,
        memory=memory,
        grounding=grounding,
        calendar=office[0],
        mailer=office[1],
        referral_mailer=referral_mailer,
        plan_store=office[2],
    )


def build_live_clock_and_ids() -> tuple[Clock, IdGenerator]:
    """Live-mode time and identity. Kept here so the seam is visible."""
    return SystemClock(), RandomIdGenerator()
