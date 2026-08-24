"""The live authentication paths, which production was otherwise the first
thing to run.

Both authenticators verify a Google-issued OIDC token, and both were marked
``pragma: no cover -- live mode only``. A mistake in either would have surfaced
as every Pub/Sub push and every console request being refused, in the
deployment, with nothing between the change and the fireground. So the token
verifier is injectable, and these tests drive the real code with a stub standing
in for Google.

What is checked here is what the two boundaries actually decide: which audience
each one demands, which principals it accepts, and that every way of failing to
resolve authority ends as a refusal rather than an internal error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from firstdue import settings as settings_module
from firstdue.api.auth import InternalPushAuthenticator, verify_google_oidc
from firstdue.api.dependencies import ConsoleAuthenticator, Role
from firstdue.domain.enums import Scope
from firstdue.errors import ConfigurationError, NotAuthorizedError
from firstdue.settings import (
    CONSOLE_ROLE_NAMES,
    AppEnv,
    Settings,
    parse_console_role_bindings,
    parse_service_accounts,
)

pytestmark = pytest.mark.authorization

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The stable Cloud Run custom audiences the deployed topology uses. They are
#: different values for the console and the push endpoint because they are
#: different trust boundaries -- an officer's browser calling the incident
#: service, and Pub/Sub calling the fleet.
CONSOLE_AUDIENCE = "https://firstdue-incident"
PUSH_AUDIENCE = "https://firstdue-slow"

PUSH_SA = "firstdue-pubsub-push@firstdue-test.iam.gserviceaccount.com"
SCHEDULER_SA = "firstdue-scheduler@firstdue-test.iam.gserviceaccount.com"
STRANGER_SA = "someone-else@another-project.iam.gserviceaccount.com"

CAPTAIN = "captain@sffd.example.org"
CHIEF = "chief@sffd.example.org"
UNBOUND = "curious@sfgov.example.org"

GOOD = "a-token-google-would-vouch-for"


def _live_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """A fully configured live-mode settings object.

    Live mode is the point: every branch under test is the one fake mode does
    not take.
    """
    payload: dict[str, Any] = {
        "app_env": AppEnv.TEST,
        "use_fake_agents": False,
        "gcp_project_id": "firstdue-test",
        "gcs_plans_bucket": "firstdue-test-plans",
        "callback_secret": "callback-secret-value",
        "console_audience": CONSOLE_AUDIENCE,
        "console_role_bindings": f"{CAPTAIN}:captain, {CHIEF}:chief",
        "internal_push_audience": PUSH_AUDIENCE,
        "internal_push_service_account": f"{PUSH_SA},{SCHEDULER_SA}",
        "fixtures_dir": REPO_ROOT / "fixtures",
        "demo_state_dir": tmp_path / ".demo-state",
        "log_json": False,
    }
    payload.update(overrides)
    return Settings(**payload)


def _request(token: str = GOOD) -> Request:
    """The smallest ASGI scope an authenticator reads."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


class StubVerifier:
    """Google's token endpoint, without Google.

    It vouches for one token, for one audience, with one set of claims, and
    refuses everything else the way ``google-auth`` does: by raising. It also
    records the audience it was asked about, because *which* audience each
    authenticator demands is itself under test.
    """

    def __init__(self, *, audience: str, **claims: Any) -> None:
        self._audience = audience
        self._claims: dict[str, Any] = {"email_verified": True, **claims}
        self.audiences_asked: list[str] = []

    def __call__(self, token: str, audience: str) -> dict[str, Any]:
        self.audiences_asked.append(audience)
        if token != GOOD:
            raise ValueError("Token could not be verified.")
        if audience != self._audience:
            raise ValueError("Token has wrong audience.")
        return self._claims


def _console(settings: Settings, verifier: Any) -> ConsoleAuthenticator:
    return ConsoleAuthenticator(settings, verifier=verifier)


def _push(settings: Settings, verifier: Any) -> InternalPushAuthenticator:
    return InternalPushAuthenticator(settings, verifier=verifier)


# ------------------------------------------------------- console: identity ---


def test_a_bound_captain_can_reach_the_referral_gate(tmp_path: Path) -> None:
    """The defect this file exists for: in live mode nobody could.

    Every caller resolved to a viewer, so ``write:referral`` was held by no one
    and the human approval the system is built around could not be given.
    """
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CAPTAIN)

    caller = _console(settings, verifier).authenticate(_request())

    assert caller.subject == CAPTAIN
    assert caller.role is Role.CAPTAIN
    assert caller.holds(Scope.WRITE_REFERRAL)
    assert not caller.holds(Scope.REQUEST_UTILITY_SHUTOFF)


def test_a_bound_chief_can_reach_the_shutoff_tap(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF)

    caller = _console(settings, verifier).authenticate(_request())

    assert caller.role is Role.CHIEF
    assert caller.holds(Scope.REQUEST_UTILITY_SHUTOFF)


def test_an_unbound_principal_is_a_viewer(tmp_path: Path) -> None:
    """Least authority for a caller nobody vouched for -- but still a caller."""
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=UNBOUND)

    caller = _console(settings, verifier).authenticate(_request())

    assert caller.role is Role.VIEWER
    assert caller.holds(Scope.READ_PROFILE)
    assert not caller.holds(Scope.WRITE_REFERRAL)


def test_a_binding_matches_whatever_case_the_provider_sends(tmp_path: Path) -> None:
    """An identity provider may vary the case of a local part it considers one
    person; the officer's authority must not depend on that."""
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email="Captain@SFFD.Example.ORG")

    caller = _console(settings, verifier).authenticate(_request())

    assert caller.role is Role.CAPTAIN
    assert caller.subject == CAPTAIN


def test_nobody_bound_means_nobody_is_more_than_a_viewer(tmp_path: Path) -> None:
    """A deployment that forgot the bindings is a console with no approvals.

    Refusing to start would take the read-only console down with it, so this is
    allowed -- but it is the state the operator has to be able to recognise.
    """
    settings = _live_settings(tmp_path, console_role_bindings="")
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF)

    caller = _console(settings, verifier).authenticate(_request())

    assert caller.role is Role.VIEWER


# -------------------------------------------------------- console: refusal ---


def test_a_token_for_another_audience_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience="https://somebody-elses-service", email=CHIEF)

    with pytest.raises(NotAuthorizedError):
        _console(settings, verifier).authenticate(_request())


def test_the_console_verifies_against_its_own_audience(tmp_path: Path) -> None:
    """Not the push endpoint's.

    They were the same setting, which meant the console could only be deployed
    behind the audience Pub/Sub was configured for -- and in the deployed
    topology those are different values, so one of the two would have been wrong.
    """
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF)

    _console(settings, verifier).authenticate(_request())

    assert verifier.audiences_asked == [CONSOLE_AUDIENCE]
    assert PUSH_AUDIENCE not in verifier.audiences_asked


def test_an_unverified_email_is_refused(tmp_path: Path) -> None:
    """An unverified claim is an assertion by the holder, not by Google."""
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF, email_verified=False)

    with pytest.raises(NotAuthorizedError):
        _console(settings, verifier).authenticate(_request())


def test_a_token_with_no_email_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, sub="1234567890")

    with pytest.raises(NotAuthorizedError):
        _console(settings, verifier).authenticate(_request())


def test_a_token_the_verifier_will_not_vouch_for_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF)

    with pytest.raises(NotAuthorizedError):
        _console(settings, verifier).authenticate(_request("forged"))


def test_a_role_name_the_enum_does_not_know_is_a_refusal_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two role vocabularies drifting must deny authority, not crash.

    Settings refuse an unknown role name at startup, so reaching this needs the
    settings-side vocabulary to know a name the enum does not -- which is
    precisely the drift the duplication invites. Before, the conversion raised
    ``ValueError`` and the caller got a 500: an authorization failure rendered
    as an internal error is one nobody reads as an authorization failure.
    """
    monkeypatch.setattr(
        settings_module, "CONSOLE_ROLE_NAMES", CONSOLE_ROLE_NAMES | {"arson-investigator"}
    )
    settings = _live_settings(tmp_path, console_role_bindings=f"{CHIEF}:arson-investigator")
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=CHIEF)

    with pytest.raises(NotAuthorizedError):
        _console(settings, verifier).authenticate(_request())


def test_a_console_with_no_audience_refuses_every_request(tmp_path: Path) -> None:
    """It must not fall back to the push endpoint's audience, or to none."""
    settings = _live_settings(tmp_path).model_copy(update={"console_audience": None})
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=CHIEF)

    authenticator = _console(settings, verifier)

    assert authenticator.is_configured is False
    with pytest.raises(ConfigurationError):
        authenticator.authenticate(_request())


def test_a_console_without_google_auth_installed_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's problem, and it must not read as the caller's.

    A refusal would send an officer hunting for a credential that is fine.
    """
    monkeypatch.setitem(sys.modules, "google.oauth2", None)
    settings = _live_settings(tmp_path)

    with pytest.raises(ConfigurationError):
        _console(settings, verify_google_oidc).authenticate(_request())


# ---------------------------------------------------------- internal push ---


def test_the_pushing_service_account_is_accepted(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=PUSH_SA)

    caller = _push(settings, verifier).verify(_request())

    assert caller.subject == PUSH_SA
    assert caller.method == "oidc"


def test_the_scheduler_service_account_is_accepted(tmp_path: Path) -> None:
    """Cloud Scheduler ticks as itself, not as the push identity.

    With one name configured, every scheduled tick was a 401 -- a slow loop that
    silently stops running, which nobody notices for a week. The two identities
    stay separate in IAM; the endpoint knows both.
    """
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=SCHEDULER_SA)

    caller = _push(settings, verifier).verify(_request())

    assert caller.subject == SCHEDULER_SA


def test_an_unrelated_service_account_is_refused(tmp_path: Path) -> None:
    """Knowing two identities is not the same as knowing any."""
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=STRANGER_SA)

    with pytest.raises(NotAuthorizedError):
        _push(settings, verifier).verify(_request())


def test_an_unverified_push_identity_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=PUSH_SA, email_verified=False)

    with pytest.raises(NotAuthorizedError):
        _push(settings, verifier).verify(_request())


def test_a_push_token_for_another_audience_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=CONSOLE_AUDIENCE, email=PUSH_SA)

    with pytest.raises(NotAuthorizedError):
        _push(settings, verifier).verify(_request())


def test_the_push_endpoint_verifies_against_the_push_audience(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=PUSH_SA)

    _push(settings, verifier).verify(_request())

    assert verifier.audiences_asked == [PUSH_AUDIENCE]


def test_a_push_token_the_verifier_will_not_vouch_for_is_refused(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=PUSH_SA)

    with pytest.raises(NotAuthorizedError):
        _push(settings, verifier).verify(_request("forged"))


def test_no_configured_identity_refuses_everyone_rather_than_anyone(tmp_path: Path) -> None:
    """The fail-closed end of the list. There is no wildcard."""
    settings = _live_settings(tmp_path).model_copy(update={"internal_push_service_account": ""})
    verifier = StubVerifier(audience=PUSH_AUDIENCE, email=PUSH_SA)

    authenticator = _push(settings, verifier)

    assert authenticator.is_configured is False
    with pytest.raises(ConfigurationError):
        authenticator.verify(_request())


def test_a_push_endpoint_without_google_auth_installed_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "google.oauth2", None)
    settings = _live_settings(tmp_path)

    with pytest.raises(ConfigurationError):
        _push(settings, verify_google_oidc).verify(_request())


# ---------------------------------------------------------------- settings ---


def test_live_mode_will_not_start_without_a_console_audience(tmp_path: Path) -> None:
    """A missing setting belongs at startup, not at request time on a fireground."""
    with pytest.raises(ConfigurationError) as raised:
        _live_settings(tmp_path, console_audience=None)

    assert "CONSOLE_AUDIENCE" in raised.value.details["missing"]


def test_live_mode_will_not_start_with_an_unusable_internal_caller_list(tmp_path: Path) -> None:
    """Set, and still nobody: a value that parses to nothing is not configured."""
    with pytest.raises(ConfigurationError) as raised:
        _live_settings(tmp_path, event_backend="pubsub", internal_push_service_account=" , ")

    assert "INTERNAL_PUSH_SERVICE_ACCOUNT" in raised.value.details["missing"]


def test_a_typo_in_the_bindings_stops_the_process(tmp_path: Path) -> None:
    """Not the request that needed the role it was meant to grant."""
    for broken in (CHIEF, f"{CHIEF}:", ":chief", "not-an-email:chief"):
        with pytest.raises(ConfigurationError):
            _live_settings(tmp_path, console_role_bindings=broken)


def test_a_binding_to_a_role_that_does_not_exist_stops_the_process(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _live_settings(tmp_path, console_role_bindings=f"{CHIEF}:admin")


def test_one_principal_cannot_be_bound_to_two_roles(tmp_path: Path) -> None:
    """Last-wins would decide silently which authority the officer holds."""
    with pytest.raises(ConfigurationError):
        _live_settings(tmp_path, console_role_bindings=f"{CHIEF}:chief,{CHIEF}:viewer")

    # The same role twice is a duplicate, not a conflict.
    assert parse_console_role_bindings(f"{CHIEF}:chief, {CHIEF}:chief") == {CHIEF: "chief"}


def test_bindings_tolerate_the_formatting_and_not_the_content() -> None:
    assert parse_console_role_bindings("") == {}
    assert parse_console_role_bindings(" , ") == {}
    assert parse_console_role_bindings(f"  {CHIEF} : CHIEF ,") == {CHIEF: "chief"}


def test_the_binding_vocabulary_is_the_role_enum() -> None:
    """Settings sit below the API layer and cannot import the enum, so the two
    lists are pinned here. A fourth role must not be bindable in one place and
    unknown in the other."""
    assert {role.value for role in Role} == CONSOLE_ROLE_NAMES


def test_service_accounts_must_be_addresses_something_could_authenticate_as() -> None:
    """A principal that can never match is a 401 that looks like a config that
    is present."""
    with pytest.raises(ConfigurationError):
        parse_service_accounts(f"{PUSH_SA},firstdue-scheduler")

    assert parse_service_accounts(None) == ()
    assert parse_service_accounts(f" {PUSH_SA} ,, {SCHEDULER_SA},{PUSH_SA} ") == (
        PUSH_SA,
        SCHEDULER_SA,
    )
