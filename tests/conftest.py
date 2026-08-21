"""Shared fixtures.

Everything here is deterministic: a fixed clock, a seeded id generator, and a
fixed epoch. A test that passes today passes identically in a replay.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.values import IntegerValue
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never inherit a developer's live-mode environment."""
    for key in list(os.environ):
        if key.startswith(("USE_FAKE", "GCP_", "GCS_", "VERTEX_", "FIRESTORE_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_sse_shutdown_event() -> Iterator[None]:
    """Let each test drive an SSE stream on its own event loop.

    ``sse-starlette`` caches a shutdown ``anyio.Event`` the first time a stream
    runs, and binds it to that loop. ``TestClient`` creates a fresh loop per
    request, so the second streaming request in a test session would fail with
    "bound to a different event loop". A long-lived server has one loop and
    never hits this; the test harness does, so the cache is cleared between
    tests rather than the production path being reshaped around it.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


@pytest.fixture
def epoch() -> datetime:
    return EPOCH


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(EPOCH)


@pytest.fixture
def ids() -> DeterministicIdGenerator:
    return DeterministicIdGenerator("test")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=True,
        fixtures_dir=REPO_ROOT / "fixtures",
        demo_state_dir=tmp_path / ".demo-state",
        log_json=False,
    )


@pytest.fixture
def make_fact(ids: DeterministicIdGenerator, epoch: datetime):
    """Factory for well-formed facts. Every field a test does not care about
    still gets a valid value, so a failure means the thing under test failed."""

    def _make(
        *,
        address_id: str = "sf-0450-hayes",
        key: str = Keys.STORIES,
        value: object | None = None,
        source_type: SourceType = SourceType.PERMIT,
        observed_at: datetime | None = None,
        confidence: float = 0.9,
        classification: Classification = Classification.PUBLIC,
        **overrides: object,
    ) -> StructuralFact:
        payload: dict[str, object] = {
            "fact_id": ids.new_id("fact"),
            "address_id": address_id,
            "canonical_key": key,
            "value": value if value is not None else IntegerValue(integer=2),
            "source_type": source_type,
            "source_ref": f"{source_type.value.lower()}/ref-1",
            "source_snapshot_id": "snapshot-1",
            "observed_at": observed_at or (epoch - timedelta(days=30)),
            "ingested_at": epoch - timedelta(days=29),
            "confidence": confidence,
            "classification": classification,
        }
        payload.update(overrides)
        return StructuralFact(**payload)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def app_client(settings: Settings) -> Iterator[object]:
    """A client authenticated as a chief.

    Every endpoint except the health probes requires a caller, so the default
    client carries the most privileged role -- tests about *what* an endpoint
    does should not be about authorization. The authorization matrix has its own
    suite, which drives the same endpoints with every role and with none.
    """
    from fastapi.testclient import TestClient

    from firstdue.api.app import create_app
    from firstdue.api.dependencies import Role, console_token

    token = console_token(settings, Role.CHIEF)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {token}"}) as client:
        yield client


@pytest.fixture
def client_factory(settings: Settings):
    """Build a client for a given role, or for no credential at all."""
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from firstdue.api.app import create_app
    from firstdue.api.dependencies import console_token

    app = create_app(settings)

    @contextmanager
    def _client(role: object | None = None, *, token: str | None = None):
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        elif role is not None:
            resolved = console_token(settings, role)  # type: ignore[arg-type]
            headers["Authorization"] = f"Bearer {resolved}"
        with TestClient(app, headers=headers) as client:
            yield client

    return _client
