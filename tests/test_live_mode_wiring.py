"""Live mode must not inherit the demo's determinism.

Phase 7 found ``build_live_clock_and_ids`` written and never called: a live
process would have run on ``SteppingClock`` and ``DeterministicIdGenerator``.
Two Cloud Run instances sharing a seeded counter mint the same ``fact_000001``
for different facts, and derived identifiers (ADR 0005) turn that into silent
data loss rather than a visible error. These tests hold that shut.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from firstdue import container as container_module
from firstdue.adapters.clock import (
    DeterministicIdGenerator,
    RandomIdGenerator,
    SteppingClock,
    SystemClock,
)
from firstdue.container import build_container, build_live_clock_and_ids
from firstdue.settings import AppEnv, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.TEST,
        use_fake_agents=False,
        gcp_project_id="firstdue-test",
        gcs_plans_bucket="firstdue-test-plans",
        callback_secret="callback-secret-value",  # noqa: S106 - a test fixture, not a credential
        # Live mode screens every untrusted document through Model Armor, so a
        # live container without a template refuses to start. That is the
        # behaviour under test elsewhere; here it just has to be satisfied.
        model_armor_template="projects/firstdue-test/locations/us-central1/templates/t",
        fixtures_dir=REPO_ROOT / "fixtures",
        demo_state_dir=tmp_path / ".demo-state",
        log_json=False,
    )


@pytest.fixture
def _stub_google(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stand in for the adapters that need a cloud SDK.

    The point under test is which clock and id generator the live branch
    chooses, not whether Vertex is reachable. Substituting the three builders
    keeps the test runnable on a machine with no ``google`` extra installed,
    which is the same machine CI uses for the default job.
    """
    from firstdue.adapters.fake.model import FakeModelClient
    from firstdue.adapters.fake.office import FakeCalendar, FakeMailer, FakeObjectStore
    from firstdue.adapters.fake.runtime import FakeRuntime

    monkeypatch.setattr(container_module, "_build_model", lambda settings: FakeModelClient())
    monkeypatch.setattr(
        container_module,
        "_build_runtime",
        lambda settings, *, clock, ids: FakeRuntime(clock=clock, ids=ids),
    )
    monkeypatch.setattr(
        container_module,
        "_build_office",
        lambda settings, *, clock, ids, stores: (
            FakeCalendar(clock=clock, ids=ids),
            FakeMailer(clock=clock),
            FakeObjectStore(bucket="stub", clock=clock),
        ),
    )


def test_live_clock_and_ids_are_the_real_ones() -> None:
    clock, ids = build_live_clock_and_ids()
    assert isinstance(clock, SystemClock)
    assert isinstance(ids, RandomIdGenerator)


def test_live_container_never_holds_a_deterministic_id_generator(
    tmp_path: Path, _stub_google: None
) -> None:
    container = build_container(_live_settings(tmp_path))

    assert not isinstance(container.ids, DeterministicIdGenerator)
    assert not isinstance(container.clock, SteppingClock)
    assert isinstance(container.ids, RandomIdGenerator)
    assert isinstance(container.clock, SystemClock)


def test_live_ids_do_not_repeat_across_two_containers(tmp_path: Path, _stub_google: None) -> None:
    """The failure this guards against is cross-*instance*, not cross-call.

    A seeded generator restarted in a second replica repeats from the top, so
    building two containers and comparing their first ids is exactly the
    production shape reduced to one process.
    """
    first = build_container(_live_settings(tmp_path / "a"))
    second = build_container(_live_settings(tmp_path / "b"))

    assert first.ids.new_id("fact") != second.ids.new_id("fact")


def test_fake_mode_container_imports_no_google_module(settings: Settings) -> None:
    """The credential-free demo has to stay credential-free.

    A stray top-level ``from google.cloud import ...`` in an adapter would not
    fail any existing test -- the package is installed here -- it would just
    make ``make demo`` require an SDK on a machine that has none. Asserting on
    ``sys.modules`` catches it at the only moment it is observable.
    """
    for name in list(sys.modules):
        if name.startswith(("google.", "vertexai", "googleapiclient")):
            del sys.modules[name]

    build_container(settings)

    leaked = sorted(
        name
        for name in sys.modules
        if name.startswith(("google.cloud", "vertexai", "googleapiclient"))
    )
    assert leaked == [], f"fake mode imported cloud SDKs: {leaked}"
