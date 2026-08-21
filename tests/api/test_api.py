"""API surface: health, readiness, error envelope, request ids, schema."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from firstdue.api.app import create_app, get_openapi_schema
from firstdue.errors import ErrorCode


def test_liveness_is_available(app_client: TestClient) -> None:
    response = app_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["app"] == "firstdue"


def test_readiness_reports_every_component(app_client: TestClient) -> None:
    response = app_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["mode"] == "fake"
    names = {check["name"] for check in body["checks"]}
    assert {"lifecycle", "city-adapter", "clock", "demo-state"} <= names


def test_draining_process_stops_advertising_readiness(app_client: TestClient) -> None:
    """After SIGTERM the process is alive but must not receive an incident."""
    app_client.app.state.lifecycle.begin_drain()  # type: ignore[attr-defined]
    response = app_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "draining"
    assert app_client.get("/healthz").status_code == 200


def test_system_status_declares_mode_and_capabilities(app_client: TestClient) -> None:
    body = app_client.get("/api/v1/system/status").json()
    assert body["mode"] == "fake"
    assert body["municipality_id"] == "san-francisco-ca"
    assert "sffd-district-03" in body["districts"]
    assert body["instant_brief_budget_ms"] == 500
    # Every phase is now built, so nothing is PLANNED. The manifest still
    # spans all five phases, and the console renders what it reports rather
    # than assuming -- an unbuilt surface would appear here as PLANNED.
    assert body["capabilities"]
    assert all(c["status"] == "AVAILABLE" for c in body["capabilities"])
    assert {c["phase"] for c in body["capabilities"]} == {1, 2, 3, 4, 5}


def test_status_carries_the_honest_disclosure(app_client: TestClient) -> None:
    disclosure = app_client.get("/api/v1/system/status").json()["disclosure"]
    assert "not a certified public-safety system" in disclosure
    assert "synthetic" in disclosure


def test_unknown_route_uses_the_error_envelope(app_client: TestClient) -> None:
    response = app_client.get("/api/v1/nope")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == ErrorCode.NOT_FOUND
    assert error["request_id"]
    assert error["correlation_id"]


def test_every_response_carries_a_request_id(app_client: TestClient) -> None:
    response = app_client.get("/healthz")
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Correlation-ID"]


def test_inbound_correlation_id_is_honoured(app_client: TestClient) -> None:
    """A CAD dispatch and everything it causes share one causal chain."""
    response = app_client.get("/healthz", headers={"X-Correlation-ID": "cad-corr-77"})
    assert response.headers["X-Correlation-ID"] == "cad-corr-77"


def test_request_ids_are_unique_per_request(app_client: TestClient) -> None:
    first = app_client.get("/healthz").headers["X-Request-ID"]
    second = app_client.get("/healthz").headers["X-Request-ID"]
    assert first != second


def test_unhandled_errors_do_not_leak_internals(settings) -> None:
    app = create_app(settings)

    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("gs://internal-plans/secret-doc.pdf could not be read")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/_boom")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert "internal-plans" not in json.dumps(error)


def test_domain_errors_map_to_their_status(settings) -> None:
    from firstdue.errors import StaleVersionError

    app = create_app(settings)

    @app.get("/api/v1/_stale")
    async def stale() -> None:
        raise StaleVersionError(expected=3, actual=5, entity="profile sf-0450-hayes")

    with TestClient(app) as client:
        response = client.get("/api/v1/_stale")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == ErrorCode.STALE_VERSION
    assert error["details"]["expected_version"] == 3


def test_openapi_schema_generates() -> None:
    schema = get_openapi_schema()
    assert schema["openapi"].startswith("3.")
    assert "/healthz" in schema["paths"]
    assert "/api/v1/system/status" in schema["paths"]
    # The document must be JSON-serialisable for `make schema` and the client.
    assert json.loads(json.dumps(schema))


def test_live_mode_without_credentials_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live-mode process must never quietly fall back to fake adapters."""
    from firstdue.errors import ConfigurationError
    from firstdue.settings import Settings

    with pytest.raises(ConfigurationError):
        Settings(use_fake_agents=False)
