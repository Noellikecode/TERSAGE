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


def _client(role: ServiceRole) -> TestClient:
    settings = Settings(app_env=AppEnv.TEST, fixtures_dir=Path("fixtures"), firstdue_loop=role)
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


def test_the_incident_service_does_not_serve_the_slow_loop() -> None:
    with _client(ServiceRole.INCIDENT) as client:
        paths = _paths(client)
        response = client.post(f"{PREFIX}/districts/sffd-district-03/poll")
    assert f"{PREFIX}/incidents/{{incident_id}}/brief" in paths
    assert f"{PREFIX}/districts/{{district_id}}/queue" not in paths
    assert response.status_code == 404


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
