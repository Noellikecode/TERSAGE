"""One contract, two backends.

Every test in this package runs twice: once against the in-memory repositories
and once against the Firestore ones. That is the whole point -- the in-memory
adapters are not stubs standing in for Firestore, they are a second
implementation of the same contract, and a behavioural difference between them
is a bug in one of them rather than a property of the backend.

The Firestore parametrisation skips unless an emulator is reachable
(``FIRESTORE_EMULATOR_HOST``) and the client library is installed. It skips
loudly rather than passing quietly: a skipped backend has proved nothing.

Run both:

    make up                       # firestore + pubsub emulators
    make test-emulator            # FIRESTORE_EMULATOR_HOST=localhost:8081 pytest
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from firstdue.container import Stores, build_firestore_stores, build_memory_stores
from firstdue.settings import AppEnv, Settings, StorageBackend

EMULATOR_ENV = "FIRESTORE_EMULATOR_HOST"
#: The emulator accepts any project id; this one names where the data came from.
EMULATOR_PROJECT = "firstdue-local"

#: Captured at import, because the root ``_clean_env`` fixture strips every
#: ``FIRESTORE_*`` variable so no test inherits a developer's live environment.
#: Here the emulator host is deliberate, so it is put back for these tests only.
EMULATOR_HOST = os.environ.get(EMULATOR_ENV)


@pytest.fixture(autouse=True)
def _restore_emulator_host(monkeypatch: pytest.MonkeyPatch) -> None:
    if EMULATOR_HOST:
        monkeypatch.setenv(EMULATOR_ENV, EMULATOR_HOST)


def _firestore_available() -> bool:
    if not EMULATOR_HOST:
        return False
    try:
        import google.cloud.firestore  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(params=["memory", "firestore"])
def backend(request: pytest.FixtureRequest) -> str:
    if request.param == "firestore" and not _firestore_available():
        pytest.skip(
            f"{EMULATOR_ENV} is not set or google-cloud-firestore is missing; "
            "run `make up` then `make test-emulator`"
        )
    param: str = request.param
    return param


@pytest.fixture
def stores(backend: str) -> Iterator[Stores]:
    """Durable repositories for one backend, isolated per test."""
    if backend == "memory":
        yield build_memory_stores()
        return

    # A fresh collection namespace per test, so a failure cannot leak state into
    # the next test and a parallel run cannot see this one's documents.
    namespace = f"t{uuid.uuid4().hex[:10]}_"
    settings = Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=True,
        storage_backend=StorageBackend.FIRESTORE,
        gcp_project_id=EMULATOR_PROJECT,
        firestore_namespace=namespace,
        log_json=False,
    )
    yield build_firestore_stores(settings)
