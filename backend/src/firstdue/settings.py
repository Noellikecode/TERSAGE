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
from typing import Final, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from firstdue.errors import ConfigurationError

#: Role names a console binding may name.
#:
#: Duplicated from :class:`firstdue.api.dependencies.Role` deliberately. Every
#: module in the API layer imports these settings, so naming the enum here would
#: be an import cycle, and settings must not depend upwards to answer a question
#: about its own input. The two are pinned together by a test, and the console
#: still refuses a role name the enum does not know rather than trusting this
#: copy -- so a drift between them denies authority instead of inventing it.
CONSOLE_ROLE_NAMES: Final[frozenset[str]] = frozenset({"viewer", "captain", "chief"})


def parse_service_accounts(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated list of service-account emails.

    Order is preserved and duplicates are kept out, but neither matters to the
    caller: this is a membership test, and the comparison against it is
    constant-time.

    An entry that is not an email address is a startup failure rather than a
    principal that can never match. The silent version of that mistake is an
    internal caller that authenticates perfectly and is refused anyway, which is
    exactly how a scheduled slow loop stops running without anyone noticing.
    """
    accounts: list[str] = []
    for entry in (raw or "").split(","):
        account = entry.strip()
        if not account:
            # A trailing or doubled comma is a formatting artifact.
            continue
        if "@" not in account:
            raise ConfigurationError(
                "internal caller service accounts must be email addresses, comma-separated",
                details={"entry": account},
            )
        if account not in accounts:
            accounts.append(account)
    return tuple(accounts)


def parse_console_role_bindings(raw: str) -> dict[str, str]:
    """Parse ``CONSOLE_ROLE_BINDINGS`` into ``{email: role name}``.

    The format is a comma-separated list of ``email:role`` pairs. Parsing is
    strict on purpose: a binding that cannot be read is authority somebody
    believes they granted and did not, and the failure mode of guessing is an
    officer who is silently a viewer at the moment they reach for an approval.
    So an unreadable entry stops the process rather than resolving to a default
    that looks like it worked.

    Emails are lower-cased on both sides of the comparison, because an identity
    provider is free to vary the case of a local part it considers one person.
    """
    bindings: dict[str, str] = {}
    for entry in raw.split(","):
        binding = entry.strip()
        if not binding:
            # A trailing or doubled comma is a formatting artifact, not an
            # ambiguous grant; there is nothing to be strict about.
            continue
        principal, separator, role_name = binding.partition(":")
        principal = principal.strip().lower()
        role_name = role_name.strip().lower()
        if not separator or not principal or not role_name or "@" not in principal:
            raise ConfigurationError(
                "CONSOLE_ROLE_BINDINGS entries must be 'email:role', comma-separated",
                details={"binding": binding},
            )
        if role_name not in CONSOLE_ROLE_NAMES:
            raise ConfigurationError(
                "CONSOLE_ROLE_BINDINGS names a role that does not exist",
                details={"binding": binding, "roles": sorted(CONSOLE_ROLE_NAMES)},
            )
        existing = bindings.get(principal)
        if existing is not None and existing != role_name:
            # Last-wins would decide silently which of the two an officer holds.
            raise ConfigurationError(
                "CONSOLE_ROLE_BINDINGS binds one principal to two roles",
                details={"principal": principal, "roles": sorted({existing, role_name})},
            )
        bindings[principal] = role_name
    return bindings


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


class ImageryProvider(StrEnum):
    """Where the building photograph comes from.

    Its own setting for the same reason :class:`WorkspaceWrites` is: the flag it
    would otherwise ride on decides six unrelated things at once. Maps Platform
    authenticates with a plain API key rather than Application Default
    Credentials, so a machine can hold a perfectly good Maps key and no Vertex
    access, or the reverse, and `USE_FAKE_AGENTS` can express neither.

    It also unblocks the demo. The credential-free console is the default and
    must stay hermetic -- `make demo` on a machine with no key touches no
    network and renders the same placeholder every time. But a team that *has* a
    key should be able to show real Street View and a real satellite tile
    without turning Vertex, Firestore and every source live at the same moment,
    which is what flipping fake mode does.

    ``google`` without a key does not fall back to the placeholder. It reports
    the refusal, because a drawing standing in for a photograph of a building a
    crew is about to enter is the failure this project refuses everywhere else.
    """

    FAKE = "fake"
    GOOGLE = "google"


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
    #:
    #: **Defaulted from ``app_env`` rather than to a constant.** A bare
    #: ``true`` here meant a deployed process that forgot one environment
    #: variable would come up serving fixtures -- with real dispatches arriving,
    #: a real console in front of an officer, and synthetic permits behind it.
    #: Nothing would look wrong. ``staging`` and ``production`` therefore
    #: default to **live**, and a deployment that wants fake has to say so out
    #: loud; ``local`` and ``test`` still default to fake, so ``make demo`` and
    #: the suite need no credentials.
    #:
    #: Set explicitly, it is always honoured -- see
    #: :meth:`_default_fake_mode_from_environment`.
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
    #: Where building photographs come from. Independent of fake mode because
    #: Maps Platform uses an API key, not Application Default Credentials, and
    #: because a real photograph is worth showing in an otherwise fake demo.
    imagery_provider: ImageryProvider = ImageryProvider.FAKE

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
    #: Run one slow-loop pass at startup so the console opens on a district
    #: that has already been surveyed -- profiles, a ranked queue, and open
    #: conflicts present as accumulated state rather than narrated.
    #:
    #: It exists because fake mode holds state in memory *per process*: a pass
    #: run by the CLI writes to a store the server never sees, so without this
    #: the console starts with an empty queue, nothing to select, and therefore
    #: no way to dispatch. Off by default so the test suite and any live
    #: deployment start cold; `make demo` and `make serve` turn it on.
    demo_prime_slow_loop: bool = False
    demo_epoch: str = "2026-08-20T08:00:00+00:00"
    #: Synthetic fixtures and real public reference data used in fake mode.
    fixtures_dir: Path = Path("fixtures")
    #: Where `make reset` writes the deterministic demo state.
    demo_state_dir: Path = Path(".demo-state")

    # ------------------------------------------------------------- google ---
    gcp_project_id: str | None = None
    gcp_region: str = "us-central1"
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
    #: The service accounts allowed to call the internal endpoints, as a
    #: comma-separated list. Live mode only.
    #:
    #: More than one because more than one Google service calls in, as separate
    #: IAM identities on purpose: Pub/Sub pushes events as the push service
    #: account, and Cloud Scheduler ticks the slow loop as its own. A single
    #: value here rejected every scheduled tick -- a 401 nobody sees, and a slow
    #: loop that has quietly not run for a week. Collapsing the two identities in
    #: IAM instead would have solved it by removing the separation.
    #:
    #: Parsed by :func:`parse_service_accounts`; an unusable value is a startup
    #: failure and an empty one refuses traffic. There is no wildcard.
    internal_push_service_account: str | None = None

    # ----------------------------------------------------- console auth ------
    #: OIDC audience the console requires. Live mode only.
    #:
    #: Deliberately not ``internal_push_audience``: that one is Pub/Sub calling
    #: the fleet, this one is an officer's browser calling the incident service.
    #: They are separate settings because they are separate trust boundaries,
    #: and each must be set on its own even where they resolve to the same
    #: string. In the current topology they do coincide on the incident service
    #: and differ everywhere else: the push audience is per-service, so the slow
    #: service and every agent worker verify pushes against their own, while the
    #: console audience is the incident service's for all of them. Reusing one
    #: setting for both would have made that arrangement unexpressible.
    #:
    #: Both are stable Cloud Run custom audiences rather than service URLs, so
    #: they survive a service being torn down and recreated at a different URL.
    console_audience: str | None = None
    #: Which authenticated principals hold which role, as ``email:role`` pairs.
    #:
    #: Live mode has no other source for this. A Google-issued ID token carries
    #: no custom claims, so a role has to be bound to the verified email out of
    #: band, and this is that binding. An unbound principal is a viewer.
    #:
    #: Parsed by :func:`parse_console_role_bindings` and validated at startup:
    #: see :meth:`_check_console_role_bindings_are_readable`.
    console_role_bindings: str = ""

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

    # ------------------------------------------------- reasoning and memory --
    # ------------------------------------------------------ fire activity --
    #: NASA FIRMS map key. Free, rate-limited, read-only.
    firms_map_key: str | None = None
    #: The area the fire-activity map covers, as ``west,south,east,north``.
    #:
    #: Regional, not municipal, and measured rather than assumed: over FIRMS'
    #: maximum five-day window San Francisco proper returns **zero** detections
    #: and Northern California returns hundreds. VIIRS pixels are ~375 m and
    #: built for wildfire, so a structure fire never registers. A city-only box
    #: would be a permanently empty panel; the region is what carries signal a
    #: department acts on -- mutual-aid demand, air quality, red-flag posture.
    fire_activity_region: str = "-124.5,36.5,-119.5,40.5"
    #: The city drawn inside that region, and counted separately. Must be
    #: enclosed by the region; a box that is not fails at startup.
    fire_activity_city_bounds: str = "-122.55,37.70,-122.35,37.84"

    #: Google Search grounding for :class:`GroundingService`. Off by default for
    #: the same two reasons Vector Search is: it bills per request, and it is the
    #: only path in the fleet that reaches the public web. A deployment that
    #: wants it says so. With it off, live mode routes grounding to the
    #: deterministic double in its unavailable state, which declines every
    #: reference with a reason rather than answering from a digest -- a live
    #: process must not emit a binding that looks retrieved and was arithmetic.
    grounding_search_enabled: bool = False
    #: LangGraph as the graph executor. The nodes and the router are ours either
    #: way; this chooses who drives them, and the built-in driver is what fake
    #: mode and the test suite run. Two tests assert the two drivers produce
    #: byte-identical reasoning chains, which is what makes this switchable at
    #: all rather than a fork.
    langgraph_enabled: bool = False
    #: Hard ceiling on graph steps, checked in the router alongside the wall
    #: clock. A budget that only bounds time still lets a cheap loop spin.
    agent_graph_max_steps: int = Field(default=24, ge=1, le=200)
    #: Durable agent working memory. Container-level only: nothing inside the
    #: bank reads settings, and there is no path that silently degrades
    #: durability -- either the fleet has a memory or it does not.
    memory_bank_enabled: bool = True
    #: The Vertex AI Agent Engine instance whose Memory Bank holds the prose
    #: half of a thread. Unset means the in-memory index serves recall, which is
    #: what fake mode runs and what the credential-free demo needs; the record
    #: is in Firestore either way, so an unset engine costs findability rather
    #: than memory. Unlike Vector Search this needs no provisioned serving node
    #: -- it bills per operation -- so it is not gated behind a cost switch.
    memory_bank_engine_id: str | None = None
    #: The region that engine lives in, and deliberately **not**
    #: ``vertex_location``. That one is ``global`` because the Gemini models
    #: answer only there; an Agent Engine instance is a regional resource and
    #: has no global endpoint, so reusing the model location would build a
    #: parent path pointing at nothing. Two settings because they are two
    #: different facts that happen to both be Vertex.
    memory_bank_location: str = "us-central1"
    #: Serve the municipal sources from the central database in Firestore.
    #:
    #: The department's own records -- permits, the assessor's roll, fire
    #: inspections, violations, Tier II filings -- answered from
    #: ``firstdue.central`` rather than from a fixture file or a public feed.
    #: Public federal and geospatial sources are unaffected and stay live, so a
    #: deployment reads real EPA, real Solar and real elevation over a generated
    #: municipal corpus.
    #:
    #: Requires ``STORAGE_BACKEND=firestore``: there is no in-memory central
    #: database, because a corpus that vanished on restart would not be a
    #: database. Refused at startup rather than degraded silently.
    central_database_enabled: bool = False

    # ------------------------------------------------------- referral email --
    #: Resend delivers the inter-agency referral once a captain has approved it.
    #: Absent, the referral is still drafted, staged, and filed -- it simply is
    #: not emailed, which is the fake-mode default and why the demo is unchanged.
    resend_api_key: str | None = None
    #: Required whenever the key is set; must be a Resend-verified sender domain.
    resend_from_address: str | None = None
    #: Where an approved referral is sent. Empty means file but do not email.
    building_department_emails: str = ""

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
    def _default_fake_mode_from_environment(self) -> Self:
        """A deployed environment defaults to live, a developer's to fake.

        Only when nobody said. ``use_fake_agents`` set explicitly -- in the
        environment, in ``.env``, or by a test -- wins in both directions, so a
        staging deployment can still be brought up in fake mode deliberately
        and will say so on the console.
        """
        if "use_fake_agents" in self.model_fields_set:
            return self
        if self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION):
            # `model_fields_set` is not updated, so a later re-validation of the
            # same object does not treat this as an explicit choice.
            object.__setattr__(self, "use_fake_agents", False)
        return self

    @model_validator(mode="after")
    def _check_console_role_bindings_are_readable(self) -> Self:
        """A binding nobody can read is authority nobody was granted.

        Parsed and discarded here rather than only on the request that needs it:
        a typo in ``CONSOLE_ROLE_BINDINGS`` must stop the process at startup, not
        leave a captain as a viewer until the first referral she cannot file.
        """
        parse_console_role_bindings(self.console_role_bindings)
        return self

    @model_validator(mode="after")
    def _check_internal_callers_are_readable(self) -> Self:
        """Same reason: an unreadable principal list is a 401 waiting to happen.

        Parsed at startup so a malformed entry stops the process, rather than
        surfacing as a scheduled tick that has been refused since the deploy.
        """
        parse_service_accounts(self.internal_push_service_account)
        return self

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
        if self.serves_console and not self.console_audience:
            # Without this the console cannot verify a single caller and refuses
            # every request -- at request time, on a fireground, rather than at
            # startup where a missing setting belongs.
            #
            # Scoped to the processes that actually serve the console, on the
            # same principle the loop split already encodes: a process is
            # required to configure what it serves and nothing else. An agent
            # worker has no console, and holding nine of them hostage to an
            # audience they never verify against is a deployment that will not
            # come up for a reason that is not true.
            missing.append("CONSOLE_AUDIENCE")
        if self.event_backend is EventBackend.PUBSUB:
            # A push endpoint that cannot verify who called it is an open door
            # into the fleet's event stream. Live mode will not start without
            # the identity it is supposed to check for.
            missing.extend(
                name
                for name, value in (
                    ("INTERNAL_PUSH_AUDIENCE", self.internal_push_audience),
                    # The parsed list, not the raw string: a value of ", " is
                    # set, is unusable, and would otherwise start a process that
                    # refuses every internal caller it has.
                    (
                        "INTERNAL_PUSH_SERVICE_ACCOUNT",
                        ",".join(self.internal_caller_service_accounts),
                    ),
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

    @model_validator(mode="after")
    def _check_referral_email_is_whole(self) -> Self:
        """A half-configured Resend is worse than an unconfigured one.

        No key at all is a documented state: the referral is drafted, staged,
        approved, and filed, and simply not emailed. A key with no sender, or a
        sender with no key, is something else -- a deployment that believes it
        is notifying the building department and is not. That failure is silent
        at the only moment it matters, months later, when someone asks why the
        referral was never actioned.

        Recipients are deliberately *not* required. Filing without emailing is
        the demo's own default, and it stays legitimate.
        """
        if bool(self.resend_api_key) != bool(self.resend_from_address):
            raise ConfigurationError(
                "referral email needs both RESEND_API_KEY and RESEND_FROM_ADDRESS, " "or neither",
                details={
                    "missing": (
                        ["RESEND_FROM_ADDRESS"] if self.resend_api_key else ["RESEND_API_KEY"]
                    )
                },
            )
        return self

    @property
    def internal_caller_service_accounts(self) -> tuple[str, ...]:
        """The identities allowed to call the internal endpoints.

        Empty means nobody, which is how it must fail: an internal endpoint that
        accepts anyone is an open door into the fleet's event stream.
        """
        return parse_service_accounts(self.internal_push_service_account)

    @property
    def console_roles(self) -> dict[str, str]:
        """The parsed principal-to-role bindings, keyed by lower-cased email.

        Re-parsed on access rather than cached: the authenticator reads it once
        when the app is built, and a settings object that answered from a cache
        would be one more thing that can disagree with its own input.
        """
        return parse_console_role_bindings(self.console_role_bindings)

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
    def serves_console(self) -> bool:
        """Whether this process serves the console API.

        Both backend services do, and the loop role has nothing to do with it.
        The console is one human-facing surface, behind one proxy, with one
        backend base URL -- so gating it on the loop split it in half: the
        service that proxy points at answered 404 for building profiles,
        district stats, the survey queue, the timeline, and the captain's
        referral approval. Which loop produced the state a screen renders is the
        wrong axis to gate a read surface on.

        What is actually true is that an **agent worker is not a console**. It
        holds one agent's service-account identity, is not publicly invokable,
        and has no operator in front of it. Until this property existed it
        mounted the console router anyway -- including the referral approval and
        the dispatch write -- which is an authorization surface nobody intended
        and one the agent-worker Terraform module already asserted did not
        exist.
        """
        return not self.is_agent_worker

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
    def referral_recipients(self) -> tuple[str, ...]:
        """Where an approved referral is emailed.

        Parsed rather than stored as a list because it arrives as one Cloud Run
        environment variable. An entry without an ``@`` is dropped rather than
        raised on: a malformed recipient must not stop a captain filing a
        referral, and the empty tuple already means "file but do not email".
        """
        return tuple(
            entry.strip()
            for entry in self.building_department_emails.split(",")
            if entry.strip() and "@" in entry
        )

    @property
    def referral_email_configured(self) -> bool:
        """Whether an approved referral can actually reach the building department."""
        return bool(self.resend_api_key and self.resend_from_address and self.referral_recipients)

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
