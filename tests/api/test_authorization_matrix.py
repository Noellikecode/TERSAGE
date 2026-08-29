"""The authorization matrix: every endpoint, every role, and no credential.

The table below is the API's authorization contract. It is written out rather
than derived, so adding an endpoint without deciding who may call it fails the
completeness test at the bottom -- which is the point.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from firstdue.api.dependencies import Role


class Endpoint(NamedTuple):
    """One route, and the least role that may call it."""

    method: str
    path: str
    #: None means public -- the health probes, and nothing else.
    least_role: Role | None
    body: dict[str, Any] | None = None


PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"
ADDRESS = "sf-0450-hayes"

#: Health probes are public because a load balancer cannot hold a credential,
#: and an unauthenticated liveness probe leaks nothing an attacker could not
#: learn by observing that the port is open.
PUBLIC: tuple[Endpoint, ...] = (
    Endpoint("GET", "/healthz", None),
    Endpoint("GET", "/readyz", None),
)

READ_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("GET", f"{PREFIX}/system/status", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/districts/{DISTRICT}/stats", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/districts/{DISTRICT}/queue", Role.VIEWER),
    # Regional fire activity is district situational awareness and decides
    # nothing, so it carries the same scope as the stats and queue it sits
    # beside on the same screen.
    Endpoint("GET", f"{PREFIX}/districts/{DISTRICT}/fire-activity", Role.VIEWER),
    # Why the fleet panel is drawing what it is drawing: correlation ids, per
    # agent event counts, and the newest instant in the audit log. Viewer,
    # because it carries no fact value and no record contents -- only counts and
    # ids the audit sink had already redacted -- and because the person who
    # needs it is the person watching the console say nothing.
    Endpoint("GET", f"{PREFIX}/districts/{DISTRICT}/slow-loop/diagnostics", Role.VIEWER),
    # The ground plane under that map is the same awareness at the same scope.
    # It carries map imagery rather than department records, and the box it
    # covers is read off the fire-activity answer rather than supplied by the
    # caller, so there is nothing here a viewer may not already see.
    Endpoint("GET", f"{PREFIX}/districts/{DISTRICT}/fire-activity/basemap", Role.VIEWER),
    # One square of the terrain mesh. Same awareness, same scope: it carries
    # public elevation and licensed map imagery, never department records, and
    # the proxy refuses anything outside the district's own region.
    Endpoint("GET", f"{PREFIX}/terrain/imagery/7/20/49", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}/timeline", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}/narratives?q=stairwell", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}/geometry", Role.VIEWER),
    # Imagery renders beside the massing model and carries the same scope: it
    # is the other half of the same pane.
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}/imagery", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/buildings/{ADDRESS}/surveys", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/registry/agents", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/registry/agents/records-watcher/1.0.0", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/registry/subscriptions", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/registry/subscriptions/fire/records-watcher/resolved", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/internal/audit/events", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/internal/audit/decisions", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/internal/audit/incidents/inc-x/replay", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/internal/metrics", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/brief", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/log", Role.VIEWER),
    # The same record, pushed. Same scope as the document it streams -- a
    # commander who may read the log may watch it arrive.
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/log/stream", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/stream", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/brief/stream-enriched", Role.VIEWER),
    # Entry packages are documents in the incident log. Reading one -- as JSON
    # or as the printed sheet -- carries the same scope as reading the log it
    # lives in; composing one and signing it are writes, below.
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/entry-packages", Role.VIEWER),
    # Why the loop has or has not composed one. A viewer, because it reports
    # decisions about the fleet -- switches, timers, counts, an error type --
    # and nothing about the building or the people in it.
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/entry-packages/diagnostics", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/entry-packages/pkg-1", Role.VIEWER),
    Endpoint("GET", f"{PREFIX}/incidents/inc-x/entry-packages/pkg-1/pdf", Role.VIEWER),
)

WRITE_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("POST", f"{PREFIX}/districts/{DISTRICT}/poll", Role.CAPTAIN),
    Endpoint(
        "POST",
        f"{PREFIX}/buildings/{ADDRESS}/surveys",
        Role.CAPTAIN,
        {"company": "E-05", "surveyor": "capt-alvarez", "outcome": "NO_ACCESS"},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/queue/queue_{DISTRICT}_{ADDRESS}/dispatch",
        Role.CAPTAIN,
        {"company": "E-05", "crew_email": "e05@sffd.example"},
    ),
    Endpoint("POST", f"{PREFIX}/conflicts/conflict-x/referral", Role.CAPTAIN),
    Endpoint(
        "POST",
        f"{PREFIX}/referrals/ref-x/approve",
        Role.CAPTAIN,
        {"approved_by": "capt-alvarez"},
    ),
    # The incident loop. Opening an incident mints a grant and reads a
    # snapshot, so it is a write; so is anything that amends the brief.
    Endpoint(
        "POST",
        f"{PREFIX}/incidents",
        Role.CAPTAIN,
        {"address": "sf-0450-hayes", "cad_ref": "CAD-0001"},
    ),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/brief/enrich", Role.CAPTAIN),
    # Reading a 911 narrative amends the brief and appends to the log, so it is
    # a write. A viewer may read what a caller said; only a captain may put it
    # in front of a commander.
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/intake",
        Role.CAPTAIN,
        {"narrative": "Caller reports smoke on the third floor."},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/resolutions",
        Role.CAPTAIN,
        {"conflict_id": "c-1", "observed_value": "3", "resolved_by": "bc-9"},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/thermal",
        Role.CAPTAIN,
        {"face": "ALPHA", "region_temps_c": [20.0]},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/frames",
        Role.CAPTAIN,
        {"image_base64": "aGVsbG8=", "camera_bearing_deg": 180.0},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/drone-sweep",
        Role.CAPTAIN,
        {},
    ),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/resources",
        Role.CAPTAIN,
        {"kind_id": "water-supply"},
    ),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/approvals/apr-1", Role.CAPTAIN),
    # Assessing readiness and solving a path both write to the incident log --
    # every criterion and every solve leaves a line under the agent that did it,
    # which is what the console's activity stream reads. A viewer may read those
    # lines; only a captain may put them there.
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/readiness", Role.CAPTAIN),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/entry-path", Role.CAPTAIN, {}),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/entry-packages", Role.CAPTAIN, {}),
    # Signing a half and sending the package are the two human decisions. Same
    # scope as approving a staged resource request, which is the other place in
    # this API where a person takes responsibility for what an agent drafted.
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/entry-packages/pkg-1/approvals/entry-path",
        Role.CAPTAIN,
    ),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/entry-packages/pkg-1/dispatch", Role.CAPTAIN),
    Endpoint("POST", f"{PREFIX}/incidents/inc-x/benchmarks/ARRIVAL", Role.CAPTAIN),
    Endpoint(
        "POST",
        f"{PREFIX}/incidents/inc-x/close",
        Role.CAPTAIN,
        {"closed_by": "bc-9"},
    ),
)

#: Endpoints that authenticate a *service*, not a console session. They take a
#: bearer token or a signature, and a console role is not one of them.
SERVICE_ENDPOINTS: tuple[str, ...] = (
    f"{PREFIX}/internal/events/push",
    f"{PREFIX}/internal/events/dead-letters",
    # Cloud Scheduler authenticates as a service, not as a console session.
    f"{PREFIX}/internal/scheduler/tick",
)

#: Authenticated by HMAC signature rather than by a bearer identity, because the
#: caller is a receiving system replying to a write we made. Listed separately so
#: "this route has no Caller parameter" is a deliberate entry rather than a gap.
SIGNATURE_AUTHENTICATED: tuple[str, ...] = (f"{PREFIX}/internal/callbacks/write",)

ALL_GUARDED = READ_ENDPOINTS + WRITE_ENDPOINTS


def _call(client: Any, endpoint: Endpoint) -> Any:
    if endpoint.method == "GET":
        return client.get(endpoint.path)
    return client.post(endpoint.path, json=endpoint.body or {})


# ------------------------------------------------------------ no credential


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", ALL_GUARDED, ids=lambda e: f"{e.method} {e.path}")
def test_every_guarded_endpoint_refuses_an_anonymous_caller(
    client_factory: Any, endpoint: Endpoint
) -> None:
    """No endpoint treats a missing credential as a viewer."""
    with client_factory(None) as client:
        response = _call(client, endpoint)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", ALL_GUARDED, ids=lambda e: f"{e.method} {e.path}")
def test_every_guarded_endpoint_refuses_a_forged_token(
    client_factory: Any, endpoint: Endpoint
) -> None:
    with client_factory(None, token="not-a-real-token") as client:  # noqa: S106
        response = _call(client, endpoint)
    assert response.status_code == 403


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", PUBLIC, ids=lambda e: f"{e.method} {e.path}")
def test_health_probes_stay_public(client_factory: Any, endpoint: Endpoint) -> None:
    with client_factory(None) as client:
        response = _call(client, endpoint)
    assert response.status_code == 200


# --------------------------------------------------------------- by role


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", READ_ENDPOINTS, ids=lambda e: e.path)
def test_a_viewer_may_read(client_factory: Any, endpoint: Endpoint) -> None:
    """Reads succeed or 404 on missing data -- never 403 for a viewer."""
    with client_factory(Role.VIEWER) as client:
        response = _call(client, endpoint)
    assert response.status_code != 403


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", WRITE_ENDPOINTS, ids=lambda e: e.path)
def test_a_viewer_may_not_write(client_factory: Any, endpoint: Endpoint) -> None:
    """The whole point of the role split: reading is not writing."""
    with client_factory(Role.VIEWER) as client:
        response = _call(client, endpoint)
    assert response.status_code == 403
    assert "missing_scopes" in response.json()["error"]["details"]


@pytest.mark.authorization
@pytest.mark.parametrize("endpoint", WRITE_ENDPOINTS, ids=lambda e: e.path)
def test_a_captain_may_write(client_factory: Any, endpoint: Endpoint) -> None:
    with client_factory(Role.CAPTAIN) as client:
        response = _call(client, endpoint)
    assert response.status_code != 403


@pytest.mark.authorization
def test_role_tokens_are_distinct(settings: Any) -> None:
    from firstdue.api.dependencies import console_token

    tokens = {console_token(settings, role) for role in Role}
    assert len(tokens) == len(list(Role))
    assert all(token for token in tokens)


# ------------------------------------------------------------ completeness


@pytest.mark.authorization
def test_every_route_declares_a_caller_dependency(app_client: Any) -> None:
    """Authorization is declared on the route, not remembered in the handler.

    Walks every route and asserts its handler takes an authenticated caller.
    Adding an endpoint without one fails here rather than shipping open.
    """
    import inspect

    from firstdue.api.auth import InternalCaller
    from firstdue.api.dependencies import Caller

    public_paths = {e.path for e in PUBLIC}
    unguarded: list[str] = []

    for route in app_client.app.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not path.startswith(("/api", "/healthz", "/readyz")):
            continue
        if path in public_paths:
            continue
        if path in SIGNATURE_AUTHENTICATED:
            # Verified by signature inside the handler; asserted below.
            assert "verify_signature" in inspect.getsource(endpoint), path
            continue
        annotations = [
            param.annotation for param in inspect.signature(endpoint).parameters.values()
        ]
        rendered = " ".join(str(a) for a in annotations)
        if "Caller" not in rendered and "InternalCaller" not in rendered:
            unguarded.append(f"{path} ({endpoint.__name__})")

    assert not unguarded, f"endpoints with no caller dependency: {unguarded}"
    # And the two caller types are distinct: a console session is not a service.
    assert Caller is not InternalCaller


@pytest.mark.authorization
def test_the_matrix_exercises_every_guarded_route(app_client: Any) -> None:
    """Every route template has at least one row in the table above."""
    import re

    # A row may carry a query string so the request it makes is valid; the
    # route template it covers is the path alone.
    covered = [
        candidate.split("?", 1)[0]
        for candidate in (
            [e.path for e in ALL_GUARDED + PUBLIC]
            + list(SERVICE_ENDPOINTS)
            + list(SIGNATURE_AUTHENTICATED)
        )
    ]
    missing: list[str] = []

    for route in app_client.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(("/api", "/healthz", "/readyz")):
            continue
        pattern = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")
        if not any(pattern.match(candidate) for candidate in covered):
            missing.append(path)

    assert not missing, f"routes missing from the authorization matrix: {missing}"


@pytest.mark.authorization
def test_service_endpoints_do_not_accept_a_console_role(client_factory: Any) -> None:
    """A console session is not a service identity, and vice versa."""
    with client_factory(Role.CHIEF) as client:
        response = client.post(f"{PREFIX}/internal/events/push", json={"message": {"data": "e30="}})
    assert response.status_code == 403
