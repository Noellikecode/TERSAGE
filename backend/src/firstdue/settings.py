"""Application settings.

Two modes, one code path:

* **Fake mode** (default) -- ``USE_FAKE_AGENTS=true``. The entire fleet,
  gateway, and console run with no Google credentials. This is how the test
  suite runs and how a judge evaluates the system for free.
* **Live mode** -- ``USE_FAKE_AGENTS=false``. Every Google setting the live
  adapters need becomes required, and startup fails loudly if one is missing.
  A half-configured live mode that silently falls back to fakes would be a
  system that lies about where its data came from.

No secret values live here or in ``.env.example``; secrets come from the
environment, or Secret Manager in deployed environments.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from firstdue.errors import ConfigurationError


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ServiceRole(StrEnum):
    """Which surfaces this process serves.

    One image, three deployments. Terraform sets ``FIRSTDUE_LOOP`` per service
    and this is what reads it -- before, the variable was set and read by
    nothing, so both backend services ran the identical full app and the
    per-service split was cosmetic.

    ``ALL`` is the default and is what the demo and the test suite run: a single
    process serving everything, with no split to reason about.
    """

    ALL = "all"
    #: The slow loop: district polls, the survey queue, profiles, referrals.
    SLOW = "slow"
    #: The incident loop: dispatch, briefs, streams, resources, the log.
    INCIDENT = "incident"


class StorageBackend(StrEnum):
    """Where durable memory lives.

    ``firestore`` is selectable in fake mode too, so the Firestore repositories
    can be exercised against a real database without turning on live models or
    live sources. It always reaches a real Firestore: there is no emulator.
    """

    MEMORY = "memory"
    FIRESTORE = "firestore"


class WorkspaceWrites(StrEnum):
    """Whether the survey calendar and crew mail reach Google Workspace.

    Calendar and Gmail are the only two write targets in the fleet that a
    service account cannot reach on its own authority. Both act *as a user*,
    which needs either domain-wide delegation on a Workspace domain or an
    interactive OAuth consent -- neither of which a bare
    ``gcloud auth application-default login`` on a personal account provides.

    Every other live integration -- Firestore, Pub/Sub, Cloud Storage, Vertex
    -- authenticates as the principal itself and needs none of that. Tying all
    six to one ``USE_FAKE_AGENTS`` flag would mean a deployment with perfectly
    good credentials for four of them could not use any, or would construct two
    clients that raise on first call. So this is its own setting.

    ``FAKE`` records the work in the durable idempotency store and the audit
    log exactly as the live clients do, and the console labels those two
    actions as simulated. A silently-skipped notification would be worse than
    an admitted one.
    """

    #: Record calendar events and crew mail locally; do not call Workspace.
    FAKE = "fake"
    #: Call Calendar and Gmail for real. Requires delegated credentials.
    GOOGLE = "google"


class EventBackend(StrEnum):
    """How events move between agents."""

    MEMORY = "memory"
    PUBSUB = "pubsub"


class Settings(BaseSettings):
    """Environment-driven configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---------------------------------------------------------------- app ---
    app_env: AppEnv = AppEnv.LOCAL
    app_name: str = "firstdue"
    #: Cloud Run supplies PORT. Honour it; never hard-code 8080.
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    log_json: bool = True
    #: Seconds to finish in-flight work after SIGTERM before the process exits.
    shutdown_grace_seconds: float = Field(default=10.0, ge=0.0, le=120.0)

    # ------------------------------------------------------------ modes -----
    #: The master switch. True means no Google credentials are needed anywhere.
    use_fake_agents: bool = True
    #: Which loop this process serves. Cloud Run sets it per service; a local
    #: process serves everything.
    firstdue_loop: ServiceRole = ServiceRole.ALL
    #: Which single agent this process is, when it is an agent worker. Empty
    #: means the process runs the whole fleet, which is what the demo and the
    #: test suite do. A worker refuses events addressed to any other agent:
    #: a push subscription pointed at the wrong service would otherwise be
    #: silently absorbed instead of visibly misrouted.
    firstdue_agent: str = ""
    #: Durable memory. Independent of fake mode, so the Firestore repositories
    #: can run against a real database without live models or live sources.
    storage_backend: StorageBackend = StorageBackend.MEMORY
    #: Event transport. Pub/Sub publishes out and receives via the push endpoint.
    event_backend: EventBackend = EventBackend.MEMORY
    #: Whether Calendar and Gmail are called for real. Independent of fake mode
    #: because those two need delegated user authority that Application Default
    #: Credentials do not carry; see :class:`WorkspaceWrites`.
    workspace_writes: WorkspaceWrites = WorkspaceWrites.FAKE

    # ------------------------------------------------- municipality ---------
    #: Default municipality. City behaviour is isolated behind CityAdapter.
    municipality_id: str = "san-francisco-ca"
    default_district_id: str = "sffd-district-03"

    # --------------------------------------------------------- determinism --
    #: Write model responses to fixtures on a cache miss. Off by default: a
    #: demo run must not silently change the pinned responses it replays.
    record_model_responses: bool = False
    #: Seed for the deterministic id generator, so `make reset` is reproducible.
    demo_seed: str = "firstdue-demo"
    #: Start of the deterministic demo timeline (ISO-8601, timezone-aware).
    demo_epoch: str = "2026-08-20T08:00:00+00:00"
    #: Synthetic fixtures and real public reference data used in fake mode.
    fixtures_dir: Path = Path("fixtures")
    #: Where `make reset` writes the deterministic demo state.
    demo_state_dir: Path = Path(".demo-state")

    # ------------------------------------------------------------- google ---
    gcp_project_id: str | None = None
    gcp_region: str = "us-west1"
    firestore_database: str = "(default)"
    pubsub_topic_prefix: str = "firstdue"
    gcs_plans_bucket: str | None = None
    #: ``global``, not a region. Verified 2026-08-21 against a real project:
    #: ``gemini-3.5-flash`` and ``gemma-4-26b-a4b-it-maas`` both 404 in
    #: ``us-central1`` and both answer on ``global``. Only ``gemini-2.5-flash``
    #: resolves regionally, and 2.5 does not satisfy the Gemini-3.5-or-newer
    #: requirement, so the region is not a fallback worth keeping.
    vertex_location: str = "global"
    gemini_model: str = "gemini-3.5-flash"
    #: Verified against the live publisher catalogue. ``gemma-3-4b-it``, which
    #: this defaulted to until it was checked, does not exist on Vertex at all:
    #: the deployable Model Garden entries (``gemma3``, ``gemma4``) are not
    #: callable through ``generateContent``, and the ``-maas`` suffix is what
    #: marks the managed endpoint that is.
    gemma_model: str = "gemma-4-26b-a4b-it-maas"
    vector_search_index: str | None = None
    model_armor_template: str | None = None

    # ------------------------------------------------- live source access ---
    #: Google Maps Platform key for the Solar API. Without it the solar source
    #: reports UNCONFIGURED in live mode -- it never falls back to a fixture.
    google_maps_api_key: str | None = None
    #: developer.nrel.gov key for the alternative-fuel-station registry.
    nrel_api_key: str | None = None
    #: DataSF app token. It identifies the caller to lift the anonymous
    #: throttle; it authorizes nothing, and the feeds are public without it.
    socrata_app_token: str | None = None
    #: NWS asks every caller to identify itself and throttles those that do not.
    source_contact_email: str = "firstdue@example.org"
    #: Prefixes every Firestore collection. Empty in production; set per run by
    #: the contract suite so parallel runs cannot see each other's documents
    #: and each run can delete exactly what it wrote.
    firestore_namespace: str = ""

    # ------------------------------------------------------ event delivery ---
    #: Attempts per consumer before an envelope becomes a dead letter.
    event_max_attempts: int = Field(default=5, ge=1, le=20)
    event_base_delay_ms: int = Field(default=250, ge=1, le=600_000)
    event_max_delay_ms: int = Field(default=60_000, ge=1, le=3_600_000)
    #: Fraction of each delay that derived jitter may subtract.
    event_jitter_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    #: Consecutive transient failures before a consumer's breaker opens.
    breaker_failure_threshold: int = Field(default=3, ge=1, le=100)
    breaker_cooldown_seconds: float = Field(default=30.0, gt=0.0, le=3600.0)
    #: How long a district poll holds its processing lock.
    lock_lease_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)

    # ------------------------------------------------- internal push auth ----
    #: Shared-secret bearer token for the internal push endpoint. Leave unset in
    #: fake mode and one is derived from DEMO_SEED -- deterministic, in no file,
    #: and printed by `firstdue status`. Live mode ignores it and requires OIDC.
    internal_push_token: str | None = None
    #: OIDC audience the push endpoint requires. Live mode only.
    internal_push_audience: str | None = None
    #: The service account Pub/Sub pushes as. Live mode only.
    internal_push_service_account: str | None = None

    # ------------------------------------------------- observability --------
    #: Tracing and metrics are off unless asked for. Fake mode never turns them
    #: on, so the credential-free demo stays credential-free and the test suite
    #: needs no collector.
    otel_enabled: bool = False
    otel_service_name: str = "firstdue"
    #: Vector Search bills for provisioned serving nodes whether or not anything
    #: queries it, so it is opt-in rather than opt-out.
    vector_search_enabled: bool = False
    vector_search_endpoint: str | None = None
    vector_embedding_model: str = "text-embedding-004"

    # --------------------------------------------------------- secret names --
    #: Names, never values. The value is fetched from Secret Manager at startup
    #: so it never sits in an environment variable, a crash dump, or the output
    #: of `gcloud run services describe`.
    callback_secret_name: str | None = None
    internal_push_token_secret_name: str | None = None

    # ------------------------------------------------------ request limits ---
    #: Sustained requests per caller. A misconfigured retry loop must not be
    #: able to take the incident loop down.
    rate_limit_per_second: float = Field(default=20.0, gt=0.0, le=10_000.0)
    rate_limit_burst: int = Field(default=40, ge=1, le=10_000)
    #: Largest request body accepted. An id-only envelope is a few hundred
    #: bytes; a pre-plan is a few tens of kilobytes.
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=64 * 1024 * 1024)
    #: Shared secret for signed callbacks from receiving systems. Derived from
    #: DEMO_SEED in fake mode, like every other demo credential.
    callback_secret: str | None = None

    # ---------------------------------------------------------------- api ---
    cors_allow_origins: tuple[str, ...] = ("http://localhost:3000",)
    api_prefix: str = "/api/v1"
    #: Instant-brief budget. Exceeding it is a defect, not a slow day.
    instant_brief_budget_ms: int = Field(default=500, gt=0)

    @model_validator(mode="after")
    def _check_live_mode_is_fully_configured(self) -> Self:
        """Live mode requires real configuration; there is no silent fallback."""
        if self.storage_backend is StorageBackend.FIRESTORE and not self.gcp_project_id:
            # A Firestore client without a project id writes nowhere in
            # particular, and the failure surfaces on the first write rather
            # than at startup where a missing setting belongs.
            raise ConfigurationError(
                "STORAGE_BACKEND=firestore requires GCP_PROJECT_ID",
                details={"missing": ["GCP_PROJECT_ID"]},
            )
        if self.use_fake_agents:
            return self
        missing = [
            name
            for name, value in (
                ("GCP_PROJECT_ID", self.gcp_project_id),
                ("GCS_PLANS_BUCKET", self.gcs_plans_bucket),
                # Without this the signed-callback endpoint refuses every
                # request -- at request time, on a fireground, rather than at
                # startup where a missing setting belongs. There is no derived
                # fallback in live mode, deliberately: deriving a production
                # secret from a seed that ships in the repository would be
                # worse than having none.
                ("CALLBACK_SECRET", self.callback_secret),
            )
            if not value
        ]
        if self.event_backend is EventBackend.PUBSUB:
            # A push endpoint that cannot verify who called it is an open door
            # into the fleet's event stream. Live mode will not start without
            # the identity it is supposed to check for.
            missing.extend(
                name
                for name, value in (
                    ("INTERNAL_PUSH_AUDIENCE", self.internal_push_audience),
                    ("INTERNAL_PUSH_SERVICE_ACCOUNT", self.internal_push_service_account),
                )
                if not value
            )
        if missing:
            raise ConfigurationError(
                "live mode requires Google configuration; set USE_FAKE_AGENTS=true "
                "to run the credential-free demo",
                details={"missing": missing},
            )
        return self

    @property
    def is_agent_worker(self) -> bool:
        return bool(self.firstdue_agent)

    def serves_agent(self, agent_id: str) -> bool:
        """Whether this process is allowed to run work for an agent."""
        return not self.firstdue_agent or self.firstdue_agent == agent_id

    @property
    def serves_slow_loop(self) -> bool:
        return self.firstdue_loop in (ServiceRole.ALL, ServiceRole.SLOW)

    @property
    def serves_incident_loop(self) -> bool:
        return self.firstdue_loop in (ServiceRole.ALL, ServiceRole.INCIDENT)

    @property
    def is_fake_mode(self) -> bool:
        return self.use_fake_agents

    @property
    def mode_label(self) -> str:
        return "fake" if self.use_fake_agents else "live"

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def resolved_internal_push_token(self) -> str | None:
        """The bearer token the push endpoint accepts in fake mode.

        Derived from ``DEMO_SEED`` when unset, so the demo needs no secret in
        any file and still refuses unauthenticated pushes. Live mode returns
        ``None``: there the endpoint verifies a Google-issued OIDC token, and a
        shared secret would be a weaker door standing next to a stronger one.
        """
        if not self.use_fake_agents:
            return None
        if self.internal_push_token:
            return self.internal_push_token
        material = f"firstdue-internal-push|{self.demo_seed}".encode()
        return hashlib.sha256(material).hexdigest()[:32]

    @property
    def resolved_callback_secret(self) -> str | None:
        """The secret signed callbacks are verified against.

        Derived from ``DEMO_SEED`` in fake mode so the demo verifies real
        signatures without a secret existing in any file. Live mode requires an
        explicit one: deriving a production secret from a seed that ships in the
        repository would be worse than having none.
        """
        if self.callback_secret:
            return self.callback_secret
        if not self.use_fake_agents:
            return None
        material = f"firstdue-callback|{self.demo_seed}".encode()
        return hashlib.sha256(material).hexdigest()[:32]

    @property
    def vertex_configured(self) -> bool:
        """Whether the Vertex settings needed for a live model call are present."""
        return bool(self.gcp_project_id and self.vertex_location and self.gemini_model)

    @property
    def uses_firestore(self) -> bool:
        return self.storage_backend is StorageBackend.FIRESTORE

    @property
    def uses_pubsub(self) -> bool:
        return self.event_backend is EventBackend.PUBSUB


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()


def clear_settings_cache() -> None:
    """Used by tests that need a different environment."""
    get_settings.cache_clear()
