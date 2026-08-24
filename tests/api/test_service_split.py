"""One image, three deployments -- and the variable that decides which.

Terraform has always set ``FIRSTDUE_LOOP`` per Cloud Run service. Nothing read
it, so both backend services ran the identical full app: the incident service
served district polls and the slow service served briefs. The split existed in
the infrastructure and nowhere in the process it configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from firstdue.api.app import create_app
from firstdue.api.dependencies import Role, console_token
from firstdue.settings import AppEnv, ServiceRole, Settings

PREFIX = "/api/v1"


#: The console API, named by the routes a deployed console actually calls. The
#: last two are the governance surface: the referral a captain files and the
#: dispatch that follows it.
CONSOLE_PATHS: tuple[str, ...] = (
    f"{PREFIX}/buildings/{{address_id}}",
    f"{PREFIX}/buildings/{{address_id}}/timeline",
    f"{PREFIX}/buildings/{{address_id}}/geometry",
    f"{PREFIX}/districts/{{district_id}}/stats",
    f"{PREFIX}/districts/{{district_id}}/queue",
    f"{PREFIX}/conflicts/{{conflict_id}}/referral",
    f"{PREFIX}/referrals/{{referral_id}}/approve",
    f"{PREFIX}/queue/{{entry_id}}/dispatch",
)

INCIDENT_PATH = f"{PREFIX}/incidents/{{incident_id}}/brief"


def _client(role: ServiceRole, *, agent: str = "") -> TestClient:
    settings = Settings(
        app_env=AppEnv.TEST,
        fixtures_dir=Path("fixtures"),
        firstdue_loop=role,
        firstdue_agent=agent,
    )
    token = console_token(settings, Role.CHIEF)
    return TestClient(create_app(settings), headers={"Authorization": f"Bearer {token}"})


def _paths(client: TestClient) -> set[str]:
    return {getattr(route, "path", "") for route in client.app.routes}


def test_the_default_process_serves_everything() -> None:
    """The demo and the test suite run one process with no split to reason about."""
    assert Settings(app_env=AppEnv.TEST).firstdue_loop is ServiceRole.ALL
    with _client(ServiceRole.ALL) as client:
        paths = _paths(client)
    assert f"{PREFIX}/districts/{{district_id}}/queue" in paths
    assert f"{PREFIX}/incidents/{{incident_id}}/brief" in paths


def test_the_slow_service_does_not_serve_the_incident_loop() -> None:
    with _client(ServiceRole.SLOW) as client:
        paths = _paths(client)
        response = client.get(f"{PREFIX}/incidents/inc-x/brief")
    assert f"{PREFIX}/districts/{{district_id}}/queue" in paths
    assert f"{PREFIX}/incidents/{{incident_id}}/brief" not in paths
    assert response.status_code == 404


def test_the_incident_service_serves_the_whole_console() -> None:
    """The console is not a loop surface, and gating it on the loop broke it.

    There is one console, behind one proxy, with one backend base URL --
    Terraform points it at the incident service. With the console router mounted
    on the slow loop, that service answered 404 for building profiles, district
    stats, the survey queue, the timeline, and the captain's referral approval:
    half the product, missing, in the deployment only. Nothing here caught it
    because the test app runs ``ServiceRole.ALL``, where every router is mounted
    and the split is invisible.
    """
    with _client(ServiceRole.INCIDENT) as client:
        paths = _paths(client)

    assert INCIDENT_PATH in paths
    assert set(CONSOLE_PATHS) <= paths


def test_the_slow_service_serves_the_console_too() -> None:
    """Either backend service can be the one the console is pointed at."""
    with _client(ServiceRole.SLOW) as client:
        paths = _paths(client)

    assert set(CONSOLE_PATHS) <= paths


@pytest.mark.parametrize("role", list(ServiceRole))
def test_an_agent_worker_exposes_no_console_at_all(role: ServiceRole) -> None:
    """A worker holds one agent's identity and has no operator in front of it.

    It mounted the console router anyway -- including the referral approval and
    the queue dispatch, the two writes the whole governance thesis rests on.
    Not remotely exploitable, because a worker is not publicly invokable, but an
    authorization surface nobody intended and one the agent-worker Terraform
    module already asserted did not exist. Parametrised over the loop roles
    because a worker inherits its agent's loop, and none of them is a console.
    """
    with _client(role, agent="records-watcher") as client:
        paths = _paths(client)

    assert not set(CONSOLE_PATHS) & paths


def test_a_slow_loop_worker_serves_neither_console_nor_incident_routes() -> None:
    """What Terraform actually deploys for a slow-loop agent: a worker that
    consumes events and answers probes, and offers no operator surface."""
    with _client(ServiceRole.SLOW, agent="records-watcher") as client:
        paths = _paths(client)

    assert not set(CONSOLE_PATHS) & paths
    assert INCIDENT_PATH not in paths
    assert "/healthz" in paths
    assert f"{PREFIX}/internal/events/push" in paths


@pytest.mark.parametrize("role", list(ServiceRole))
def test_every_service_answers_its_probes(role: ServiceRole) -> None:
    """A load balancer probes every service, whatever it serves."""
    with _client(role) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code in (200, 503)


@pytest.mark.parametrize("role", list(ServiceRole))
def test_every_service_carries_the_registry_and_the_internal_surfaces(
    role: ServiceRole,
) -> None:
    """Both loops publish and consume events, and both are catalogued."""
    with _client(role) as client:
        paths = _paths(client)
    assert f"{PREFIX}/registry/agents" in paths
    assert f"{PREFIX}/internal/events/push" in paths
    assert f"{PREFIX}/system/status" in paths


async def test_an_agent_worker_refuses_another_agents_work() -> None:
    """A worker runs its agent and nothing else.

    Each worker holds one agent's service account. Quietly running work
    addressed to a different agent would execute it under the wrong identity --
    the exact confusion the per-agent identities exist to prevent, and one that
    would look like success in every log.
    """
    from datetime import UTC, datetime, timedelta

    import pytest as _pytest

    from firstdue.adapters.clock import DeterministicIdGenerator, SteppingClock
    from firstdue.adapters.fake.runtime import FakeRuntime
    from firstdue.adapters.memory.audit import InMemoryAuditSink
    from firstdue.adapters.memory.repositories import (
        InMemoryAgentRunRepository,
        InMemoryGrantRepository,
        InMemoryRegistryRepository,
    )
    from firstdue.agents.fleet import FleetRunner
    from firstdue.errors import ConfigurationError
    from firstdue.services.grants import GrantService

    clock = SteppingClock(datetime(2026, 8, 20, tzinfo=UTC), step=timedelta(milliseconds=50))
    ids = DeterministicIdGenerator("worker-test")
    runner = FleetRunner(
        runtime=FakeRuntime(clock=clock, ids=ids),
        registry=InMemoryRegistryRepository(),
        grants=GrantService(
            grants=InMemoryGrantRepository(), clock=clock, ids=ids, audit=InMemoryAuditSink()
        ),
        runs=InMemoryAgentRunRepository(),
        clock=clock,
        ids=ids,
        only_agent="records-watcher",
    )

    # Its own agent runs.
    own = await runner.run("records-watcher", correlation_id="corr_1")
    assert own.completed

    # Another agent's work is refused, not absorbed.
    with _pytest.raises(ConfigurationError):
        await runner.run("geometry-watcher", correlation_id="corr_1")


def test_a_process_with_no_agent_set_runs_the_whole_fleet() -> None:
    """The demo and the test suite run one process with no restriction."""
    settings = Settings(app_env=AppEnv.TEST)
    assert settings.firstdue_agent == ""
    assert settings.is_agent_worker is False
    assert settings.serves_agent("records-watcher")
    assert settings.serves_agent("incident-recorder")


def test_a_worker_serves_only_its_own_agent() -> None:
    settings = Settings(app_env=AppEnv.TEST, firstdue_agent="sensor-fusion")
    assert settings.is_agent_worker is True
    assert settings.serves_agent("sensor-fusion")
    assert not settings.serves_agent("records-watcher")
