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
from firstdue.container import (
    _build_office,
    build_container,
    build_live_clock_and_ids,
    build_memory_stores,
)
from firstdue.settings import AppEnv, Settings, WorkspaceWrites

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


class TestWorkspaceWritesIsItsOwnSwitch:
    """Calendar and Gmail do not authenticate the way the rest of live mode does.

    Every other Google integration in the fleet -- Firestore, Pub/Sub, Cloud
    Storage, Vertex -- authenticates as the deployment's own principal. Calendar
    and Gmail act *as a user*, which needs domain-wide delegation or an
    interactive consent. Binding all of them to ``USE_FAKE_AGENTS`` would mean a
    deployment with good credentials for four of them constructs two clients
    that raise on their first call, in the middle of a survey dispatch.

    So ``WORKSPACE_WRITES`` is separate, and these tests hold the seam open in
    both directions.
    """

    @staticmethod
    def _settings(tmp_path: Path, workspace: WorkspaceWrites) -> Settings:
        return _live_settings(tmp_path).model_copy(update={"workspace_writes": workspace})

    def test_it_defaults_to_fake_so_a_personal_account_cannot_be_assumed(
        self, tmp_path: Path
    ) -> None:
        """The safe default: nothing calls Workspace unless somebody said to."""
        assert _live_settings(tmp_path).workspace_writes is WorkspaceWrites.FAKE

    def test_fake_workspace_still_gets_a_real_plan_store(self, tmp_path: Path) -> None:
        """The three are built independently, so GCS does not follow Calendar down.

        This is the whole point of the split. A pre-incident plan is written by
        the deployment's own service account and has no user to act as.
        """
        from firstdue.adapters.fake.office import FakeCalendar, FakeMailer

        calendar, mail, plans = _build_office(
            self._settings(tmp_path, WorkspaceWrites.FAKE),
            clock=SystemClock(),
            ids=RandomIdGenerator(),
            stores=build_memory_stores(),
        )
        assert isinstance(calendar, FakeCalendar)
        assert isinstance(mail, FakeMailer)
        assert type(plans).__name__ == "GoogleObjectStore"

    def test_google_workspace_builds_the_real_two(self, tmp_path: Path) -> None:
        """And setting it to ``google`` genuinely reaches for the Google clients."""
        calendar, mail, plans = _build_office(
            self._settings(tmp_path, WorkspaceWrites.GOOGLE),
            clock=SystemClock(),
            ids=RandomIdGenerator(),
            stores=build_memory_stores(),
        )
        assert type(calendar).__name__ == "GoogleCalendarClient"
        assert type(mail).__name__ == "GmailClient"
        assert type(plans).__name__ == "GoogleObjectStore"

    def test_fake_mode_ignores_it_entirely(self, tmp_path: Path) -> None:
        """``WORKSPACE_WRITES=google`` must not drag a credential-free demo live.

        `make demo` runs with no Google auth of any kind. A stray environment
        variable in a developer's shell should not change that.
        """
        from firstdue.adapters.fake.office import FakeCalendar, FakeMailer, FakeObjectStore

        settings = _live_settings(tmp_path).model_copy(
            update={"use_fake_agents": True, "workspace_writes": WorkspaceWrites.GOOGLE}
        )
        calendar, mail, plans = _build_office(
            settings, clock=SystemClock(), ids=RandomIdGenerator(), stores=build_memory_stores()
        )
        assert isinstance(calendar, FakeCalendar)
        assert isinstance(mail, FakeMailer)
        assert isinstance(plans, FakeObjectStore)
