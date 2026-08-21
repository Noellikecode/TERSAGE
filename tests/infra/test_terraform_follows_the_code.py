"""Terraform has to say what the code says.

Three drifts are possible between an application and its infrastructure, and
all three are silent until an incident:

* a Firestore collection with no composite index -- the query works locally
  against the emulator, which needs no index, and fails in staging;
* an agent whose IAM is wider than its declared scopes -- the gateway denies
  and IAM would have allowed, so the second line of defence is not there;
* a topic with no subscription -- events publish successfully into nothing.

These tests read the checked-in policy files that Terraform consumes and
compare them against the code that is the actual source of truth. They need no
cloud access, which is the point: the infrastructure claims are checkable on a
laptop with no credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from firstdue.adapters.firestore.client import COLLECTION_NAMES
from firstdue.domain.enums import Scope
from firstdue.domain.events import Topic
from firstdue.registry.descriptors import FLEET

TERRAFORM = Path(__file__).resolve().parents[2] / "infra" / "terraform"
POLICY = TERRAFORM / "policy"


def _load(name: str) -> dict:
    return json.loads((POLICY / name).read_text())


# --------------------------------------------------------------- firestore ---


def test_every_firestore_collection_is_accounted_for() -> None:
    """All 23 collections, each either indexed or explicitly not needing one."""
    collections = _load("firestore.json")["collections"]

    assert set(collections) == set(COLLECTION_NAMES)


def test_a_collection_without_indexes_says_why() -> None:
    """ "No index" and "forgot the index" must not look the same."""
    collections = _load("firestore.json")["collections"]

    for name, spec in collections.items():
        if not spec["indexes"]:
            assert spec.get("reason"), f"{name} has no indexes and no reason"


def test_every_index_names_an_order_per_field() -> None:
    """The Terraform module zips fields against orders by position."""
    for name, spec in _load("firestore.json")["collections"].items():
        for index in spec["indexes"]:
            assert len(index["fields"]) == len(index["order"]), name


# ------------------------------------------------------------------- topics ---


def test_every_topic_has_terraform() -> None:
    declared = set(_load("topics.json")["topics"])

    assert declared == {topic.value for topic in Topic}


# ---------------------------------------------------------------------- iam ---


def test_every_agent_has_a_service_account_entry() -> None:
    agents = _load("agents.json")["agents"]

    assert set(agents) == {descriptor.agent_id for descriptor in FLEET}


def test_agent_scopes_match_their_descriptors() -> None:
    """IAM is derived from the contract, not maintained beside it.

    Widening an agent's cloud permissions has to start by widening its
    descriptor, where the gateway will also see the change.
    """
    agents = _load("agents.json")["agents"]

    for descriptor in FLEET:
        assert agents[descriptor.agent_id]["scopes"] == sorted(
            scope.value for scope in descriptor.required_scopes
        ), descriptor.agent_id


def test_every_scope_maps_to_roles() -> None:
    """A scope with no entry would silently contribute no roles at all."""
    policy = _load("agents.json")
    mapped = set(policy["scope_roles"])

    for descriptor in FLEET:
        for scope in descriptor.required_scopes:
            assert scope.value in mapped, f"{scope.value} has no role mapping"


def test_an_agents_roles_come_only_from_its_own_scopes() -> None:
    """The acceptance criterion, checked without a cloud project.

    Two agents differ in what they may do because they differ in what they
    declared. So if an agent receives a role that *only* another agent's scopes
    map to, something granted it sideways -- and that is precisely one agent
    holding another agent's permissions.
    """
    policy = _load("agents.json")
    scope_roles = policy["scope_roles"]

    effective = {
        agent_id: {role for scope in spec["scopes"] for role in scope_roles[scope]}
        | set(spec["pubsub"])
        for agent_id, spec in policy["agents"].items()
    }

    for agent_id, spec in policy["agents"].items():
        own_scopes = set(spec["scopes"])
        for other_id, other in policy["agents"].items():
            if other_id == agent_id:
                continue
            exclusive_scopes = set(other["scopes"]) - own_scopes
            exclusive_roles = {
                role
                for scope in exclusive_scopes
                for role in scope_roles[scope]
                # A role two different scopes both imply is not exclusive.
                if not any(role in scope_roles[mine] for mine in own_scopes)
            }
            trespass = effective[agent_id] & exclusive_roles
            assert (
                trespass == set()
            ), f"{agent_id} holds {trespass}, which only {other_id}'s scopes imply"


def test_agent_bindings_are_derived_and_not_hand_written() -> None:
    """One place grants roles to agents, and it reads from the scope map.

    The previous test compares data. This one checks that the Terraform is
    still the thing that consumes it -- a hand-written extra binding beside the
    derived ones would pass a data comparison and still widen an identity.
    """
    hcl = _without_comments((TERRAFORM / "modules" / "iam" / "main.tf").read_text())

    # Exactly two: one derived binding for agents, one for services. A third
    # would be a role granted outside the scope map.
    assert hcl.count('resource "google_project_iam_member"') == 2
    assert "for_each = local.agent_bindings" in hcl
    assert "flatten([for s in spec.scopes : local.scope_roles[s]])" in hcl


def _without_comments(text: str) -> str:
    """Comments explain what is *not* granted; only the code grants anything."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_service_account_can_impersonate_another() -> None:
    """Correct roles do not help if an SA can simply become a different one."""
    hcl = _without_comments((TERRAFORM / "modules" / "iam" / "main.tf").read_text())
    policy_text = _without_comments((POLICY / "agents.json").read_text())

    for forbidden in (
        "serviceAccountTokenCreator",
        "serviceAccountUser",
        "roles/owner",
        "roles/editor",
        "roles/iam.serviceAccountAdmin",
    ):
        assert forbidden not in hcl, f"IAM module grants {forbidden}"
        assert forbidden not in policy_text, f"IAM policy grants {forbidden}"


def test_no_agent_has_standing_access_to_phi() -> None:
    """PHI is reachable through the gateway's DERIVE path, or not at all.

    ``read:ems-derived`` deliberately maps to no IAM role: an agent that could
    read person-level data at rest would make the IncidentGrant decorative.
    """
    policy = _load("agents.json")

    assert policy["scope_roles"][Scope.READ_EMS_DERIVED.value] == []

    # And nothing else hands out a role wide enough to reach it anyway.
    for roles in policy["scope_roles"].values():
        for role in roles:
            assert role not in {"roles/datastore.owner", "roles/storage.admin"}, role


def test_read_scopes_never_grant_write_roles() -> None:
    """The rule the gateway enforces, restated where IAM can break it."""
    scope_roles = _load("agents.json")["scope_roles"]

    write_roles = {
        "roles/datastore.user",
        "roles/storage.objectCreator",
        "roles/storage.objectAdmin",
        "roles/pubsub.admin",
    }
    for scope, roles in scope_roles.items():
        if scope.startswith("read:"):
            assert not (set(roles) & write_roles), f"{scope} grants a write role"


def test_the_console_service_account_holds_nothing() -> None:
    """The console calls the backend. It is not a cloud client."""
    services = _load("agents.json")["services"]

    assert services["firstdue-console"]["roles"] == ["roles/logging.logWriter"]


def test_the_ci_key_service_account_holds_nothing() -> None:
    """One SA's key lives in a GitHub secret, so its blast radius is the test.

    A leaked key buys an attacker the ability to call the staging service --
    the same thing an unauthenticated fireground console can do -- and nothing
    at rest.
    """
    services = _load("agents.json")["services"]

    assert services["firstdue-ci-smoke"]["roles"] == []


# ------------------------------------------------------------------ modules ---


@pytest.mark.parametrize(
    "module",
    [
        "project-services",
        "artifact-registry",
        "firestore",
        "pubsub",
        "storage",
        "cloud-run",
        "iam",
        "secrets",
        "scheduler",
        "observability",
        "budget",
        "vector-search",
    ],
)
def test_module_exists(module: str) -> None:
    assert (TERRAFORM / "modules" / module / "main.tf").is_file()


def test_cloud_run_bounds_are_hard_ceilings_not_defaults() -> None:
    """A budget alert arrives minutes late and cannot stop anything.

    ``max_instance_count`` is what actually bounds spend, so both backend
    services set it explicitly rather than inheriting a module default.
    """
    staging = (TERRAFORM / "envs" / "staging" / "main.tf").read_text()

    assert staging.count("max_instances") >= 3


def test_vector_search_is_off_by_default() -> None:
    """A running index endpoint cannot sit under a 50 USD cap."""
    variables = (TERRAFORM / "envs" / "staging" / "variables.tf").read_text()
    module = (TERRAFORM / "modules" / "vector-search" / "main.tf").read_text()

    assert 'variable "vector_search_enabled"' in variables
    assert re.search(r'variable "enabled" \{[^}]*default\s*=\s*false', module, re.S)


def test_no_secret_value_is_written_into_terraform() -> None:
    """Terraform creates secret containers. Versions are added out of band."""
    for path in TERRAFORM.rglob("*.tf"):
        text = path.read_text()
        assert "google_secret_manager_secret_version" not in text, path
