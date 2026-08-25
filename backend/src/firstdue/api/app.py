"""Application factory.

One function builds the whole HTTP surface, so tests, the CLI, and the container
entrypoint all get an identical app. Nothing is configured at import time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from firstdue import __version__
from firstdue.api.auth import InternalPushAuthenticator
from firstdue.api.dependencies import ConsoleAuthenticator
from firstdue.api.errors import install_error_handlers
from firstdue.api.middleware import RequestContextMiddleware
from firstdue.api.routes import console, health, incidents, internal, registry, system
from firstdue.container import Container, build_container
from firstdue.demo.loader import load_demo_state
from firstdue.lifecycle import Lifecycle
from firstdue.observability.logging import configure_logging, get_logger
from firstdue.registry.seed import RegistrySeedResult, seed_registry, verify_registry
from firstdue.security.limits import RequestLimitsMiddleware, TokenBucketLimiter
from firstdue.settings import AppEnv, Settings, StorageBackend, get_settings

logger = get_logger(__name__)

DESCRIPTION = """
Municipal structural intelligence as an institutional agent fleet.

A decision-support prototype. It delivers information and performs clerical
execution. It never issues tactical recommendations, evacuation orders, crew
assignments, or fire-behaviour predictions.
""".strip()


def _lifespan_factory(settings: Settings) -> object:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container: Container = app.state.container
        lifecycle: Lifecycle = app.state.lifecycle

        # Model Armor's first call pays ~1.1 s of channel setup, and charging
        # that to the first ingested document pushed screens past their 2-second
        # budget under the slow loop's concurrency -- which fail-closes and
        # withholds the document, so a district ingested nothing while the logs
        # said the screen was unavailable. Paid here instead, with nothing
        # waiting on it. Never raises; a cold screen still screens.
        warm = getattr(container.screen, "warm", None)
        if warm is not None:
            await warm()

        loaded = await load_demo_state(container)
        # The catalog is settled before traffic is accepted: an agent that
        # cannot be resolved to a pinned version must not be able to run.
        #
        # Who *publishes* it is a different question from who depends on it.
        # Publishing is a write, and a per-agent worker holds only the roles its
        # declared scopes earn it -- for two of the nine that is read-only
        # Firestore, by design. So a worker verifies its own entry and the
        # services that own the catalog publish it. See `verify_registry`.
        # A worker defers to someone else's publication only when there *is*
        # someone else. An in-memory registry is process-local, so a worker
        # holding one would wait forever for a publication that can never
        # arrive -- which is why fake mode and the test suite are untouched by
        # any of this and still seed exactly as before.
        catalog_is_shared = settings.storage_backend is not StorageBackend.MEMORY
        if settings.firstdue_agent and catalog_is_shared:
            await verify_registry(container.registry, agent_id=settings.firstdue_agent)
            seeded_registry = RegistrySeedResult()
        else:
            seeded_registry = await seed_registry(container.registry, now=container.clock.now())

        # One pass, before readiness, when the operator asked for it. The
        # console's own framing is that months of survey work are already
        # state -- so it must open on a ranked queue rather than on an empty
        # district nobody can act on. Failure here is logged and shrugged off:
        # a demo that could not prime is still a working console, and refusing
        # to start over it would turn a convenience into an outage.
        primed = 0
        if settings.demo_prime_slow_loop:
            from firstdue.demo.scenario import run_slow_loop

            try:
                for district_id in container.city.list_districts():
                    report = await run_slow_loop(container, district_id=district_id, approve=False)
                    primed += report.queue_size
            except Exception as exc:  # pragma: no cover - see docstring above
                logger.warning("demo_prime_failed", extra={"error_type": type(exc).__name__})

        lifecycle.mark_started(container.clock.now())
        lifecycle.note("mode", container.mode)
        lifecycle.note("municipality", container.city.municipality_id)
        lifecycle.note("seeded_profiles", str(loaded))
        lifecycle.note("primed_queue", str(primed))
        lifecycle.note("storage_backend", container.storage_label)
        lifecycle.note("event_backend", container.event_label)
        lifecycle.note(
            "published_agents",
            str(seeded_registry.published + seeded_registry.already_published),
        )
        logger.info(
            "startup",
            extra={
                "mode": container.mode,
                "municipality": container.city.municipality_id,
                "environment": str(settings.app_env),
                "version": __version__,
                "seeded_profiles": loaded,
                "storage_backend": container.storage_label,
                "event_backend": container.event_label,
                "published_agents": seeded_registry.published,
            },
        )
        try:
            yield
        finally:
            # SIGTERM path: stop advertising readiness first so the load
            # balancer drains us, then let in-flight requests finish.
            lifecycle.begin_drain()
            logger.info("shutdown_draining", extra={"grace_s": settings.shutdown_grace_seconds})
            if settings.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION):
                await asyncio.sleep(settings.shutdown_grace_seconds)
            logger.info("shutdown_complete")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)

    container = build_container(resolved)

    app = FastAPI(
        title="TERSAGE",
        version=__version__,
        description=DESCRIPTION,
        lifespan=_lifespan_factory(resolved),  # type: ignore[arg-type]
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.container = container
    app.state.lifecycle = Lifecycle()
    app.state.settings = resolved
    app.state.push_auth = InternalPushAuthenticator(resolved)
    app.state.console_auth = ConsoleAuthenticator(resolved)

    # Size and rate limits sit outermost, so a body too large or a caller in a
    # retry storm is refused before any of the work below runs. Health probes
    # are exempt: rate-limiting readiness pulls a healthy instance out of
    # rotation during exactly the spike the limit exists to survive.
    app.add_middleware(
        RequestLimitsMiddleware,
        clock=container.clock,
        limiter=TokenBucketLimiter(
            rate_per_second=resolved.rate_limit_per_second,
            burst=resolved.rate_limit_burst,
        ),
        max_body_bytes=resolved.max_request_bytes,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Correlation-ID"],
    )

    install_error_handlers(app)

    # Health, status, the registry, and the internal surfaces are on every
    # service: a load balancer probes all of them, and both loops publish and
    # consume events.
    app.include_router(health.router)
    app.include_router(system.router, prefix=resolved.api_prefix)
    app.include_router(registry.router, prefix=resolved.api_prefix)
    app.include_router(internal.router, prefix=resolved.api_prefix)
    # The console is on both backend services and on neither agent worker; it is
    # not a loop surface. See :attr:`Settings.serves_console` -- mounting it on
    # the slow loop meant the service the console proxy points at served none of
    # it.
    if resolved.serves_console:
        app.include_router(console.router, prefix=resolved.api_prefix)
    if resolved.serves_incident_loop:
        app.include_router(incidents.router, prefix=resolved.api_prefix)

    return app


def get_openapi_schema() -> dict[str, object]:
    """Generate the OpenAPI document. Used by ``make schema`` and CI."""
    from firstdue.settings import Settings as _Settings

    app = create_app(_Settings(app_env=AppEnv.TEST))
    schema: dict[str, object] = app.openapi()
    return schema
