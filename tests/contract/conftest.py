"""One contract, two backends.

Every test in this package runs twice: once against the in-memory repositories
and once against the Firestore ones. That is the whole point -- the in-memory
adapters are not stubs standing in for Firestore, they are a second
implementation of the same contract, and a behavioural difference between them
is a bug in one of them rather than a property of the backend.

**Which Firestore.** Two targets satisfy this suite, and the tests do not care
which they got:

* ``FIRESTORE_EMULATOR_HOST`` -- a local emulator, no credentials, what
  ``make up && make test-emulator`` runs;
* ``FIRESTORE_TEST_PROJECT`` -- a real Firestore database, reached through
  Application Default Credentials, which is what CI runs.

The real project is the stronger evidence. An emulator is a reimplementation of
Firestore's semantics, and the things this suite actually asserts -- that a
transaction serialises a read-compare-write, that `create` on an existing
document fails at the database, that a fence counter survives a release -- are
exactly the semantics an emulator is most likely to approximate.

Neither configured means **skip, loudly**. A skipped backend has proved
nothing, and CI fails the job if this suite reports one.

Isolation and cleanup are per test. Every test gets its own collection
namespace, so parallel runs cannot see each other's documents, and against a
real project the namespace is deleted afterwards -- an emulator forgets when it
stops, a real database does not.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest

from firstdue.adapters.firestore.client import COLLECTION_NAMES
from firstdue.container import Stores, build_firestore_stores, build_memory_stores
from firstdue.settings import AppEnv, Settings, StorageBackend

EMULATOR_ENV = "FIRESTORE_EMULATOR_HOST"
PROJECT_ENV = "FIRESTORE_TEST_PROJECT"

#: The emulator accepts any project id; this one names where the data came from.
EMULATOR_PROJECT = "firstdue-local"

#: Captured at import, because the root ``_clean_env`` fixture strips every
#: ``FIRESTORE_*`` variable so no test inherits a developer's live environment.
#: Here they are deliberate, so they are put back for these tests only.
EMULATOR_HOST = os.environ.get(EMULATOR_ENV)
REAL_PROJECT = os.environ.get(PROJECT_ENV)

#: Which database `FIRESTORE_TEST_DATABASE` names. Empty means the default one.
REAL_DATABASE = os.environ.get("FIRESTORE_TEST_DATABASE") or "(default)"


def firestore_target() -> tuple[str, str] | None:
    """``(project_id, database)`` for whichever Firestore is configured.

    The emulator wins when both are set: a developer with live credentials in
    their shell who starts an emulator meant to use the emulator, and writing
    a test suite's throwaway documents into a real project by accident is not a
    mistake that should be possible to make quietly.
    """
    if EMULATOR_HOST:
        return (EMULATOR_PROJECT, "(default)")
    if REAL_PROJECT:
        return (REAL_PROJECT, REAL_DATABASE)
    return None


@pytest.fixture(autouse=True)
def _restore_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if EMULATOR_HOST:
        monkeypatch.setenv(EMULATOR_ENV, EMULATOR_HOST)
    if REAL_PROJECT:
        monkeypatch.setenv(PROJECT_ENV, REAL_PROJECT)


def _firestore_available() -> bool:
    if firestore_target() is None:
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
            f"neither {EMULATOR_ENV} nor {PROJECT_ENV} is set, or "
            "google-cloud-firestore is missing; run `make up && make test-emulator` "
            "for the emulator, or set FIRESTORE_TEST_PROJECT for a real database"
        )
    param: str = request.param
    return param


@pytest.fixture
def stores(backend: str) -> Iterator[Stores]:
    """Durable repositories for one backend, isolated per test."""
    if backend == "memory":
        yield build_memory_stores()
        return

    target = firestore_target()
    assert target is not None  # guarded by the `backend` fixture
    project_id, database = target

    # A fresh collection namespace per test, so a failure cannot leak state into
    # the next test and a parallel run cannot see this one's documents.
    namespace = f"t{uuid.uuid4().hex[:10]}_"
    settings = Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=True,
        storage_backend=StorageBackend.FIRESTORE,
        gcp_project_id=project_id,
        firestore_database=database,
        firestore_namespace=namespace,
        log_json=False,
    )
    try:
        yield build_firestore_stores(settings)
    finally:
        # An emulator forgets when it stops. A real database does not, and a
        # suite that left a namespace behind on every run would turn a test
        # project into a landfill and eventually into a bill.
        if not EMULATOR_HOST:
            _purge(project_id, database, namespace)


def _purge(project_id: str, database: str, namespace: str) -> None:
    """Delete every document this test wrote.

    Best effort, and deliberately so: a cleanup failure must not turn a passing
    contract test into a failing one. What it must not do is fail *silently*,
    so it warns -- an accumulating namespace is a thing somebody should notice.
    """
    try:
        asyncio.run(_purge_async(project_id, database, namespace))
    except Exception as exc:  # pragma: no cover - cleanup is best effort
        import warnings

        warnings.warn(
            f"could not purge Firestore namespace {namespace}: {type(exc).__name__}",
            stacklevel=2,
        )


async def _purge_async(project_id: str, database: str, namespace: str) -> None:
    from google.cloud.firestore import AsyncClient

    client = AsyncClient(project=project_id, database=database)
    try:
        for name in COLLECTION_NAMES:
            collection = client.collection(f"{namespace}{name}")
            # Batched: a per-document round trip over ~23 collections is slower
            # than the test it is cleaning up after.
            batch = client.batch()
            pending = 0
            async for document in collection.stream():
                batch.delete(document.reference)
                pending += 1
                if pending == 400:  # Firestore caps a batch at 500 writes.
                    await batch.commit()
                    batch = client.batch()
                    pending = 0
            if pending:
                await batch.commit()
    finally:
        client.close()
