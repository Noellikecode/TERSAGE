"""Publishing the fleet into Google Cloud Agent Registry.

The catalog in :mod:`firstdue.registry.descriptors` stays the source of truth --
Terraform derives topics, service accounts and workers from it, and
``tests/infra/test_iam_matches_descriptors.py`` fails if the two drift. Nothing
here changes that. What this adds is *discovery*: the same nine agents published
into the managed registry, so an operator browsing Google Cloud sees the fleet
rather than having to read this repository.

**The interchange format is an A2A Agent Card.** The registry accepts an
``agentSpec`` of type ``A2A_AGENT_CARD``, so an agent's public description
crosses into Google's catalog as the open Agent2Agent card shape. Worth being
precise about what that does and does not mean: the fleet does *not* speak A2A
between its own agents, and adopting it there would cost more than it bought --
A2A messages carry content parts, and this system's envelopes carry identifiers
and nothing else, which is what makes a redelivered event safe to replay. A card
is a description of an agent. It is not a channel.

**What a card may say.** Only what the catalog already publishes: the agent's
id, version, publisher, role, declared capabilities, and the scopes it requires.
No endpoint that would let a caller invoke an agent outside the gateway, and
nothing about an incident. Registration is a directory entry, not a door.
"""

from __future__ import annotations

from typing import Any, Final

from firstdue.domain.registry import AgentDescriptor
from firstdue.registry.descriptors import ACTIVE_FLEET

#: The registry's own spec type for an Agent2Agent card.
AGENT_CARD_SPEC: Final[str] = "A2A_AGENT_CARD"

#: Cards are capped at 10KB by the API. Ours are a few hundred bytes; the cap is
#: named so a future addition that blows it fails here rather than at the wire.
MAX_CARD_BYTES: Final[int] = 10_000

#: The A2A card version the registry validates against, found by bisecting the
#: live API rather than from a doc page -- its own error messages disagree.
#:
#: ``0.2.9`` is refused as "only supported for v0.3.x", which suggests a v1.x
#: shape; but the v1.x fields that message recommends (``supportedInterfaces``)
#: come back as an unknown field, and omitting ``protocolVersion`` entirely is
#: refused as required. ``0.3.0`` with a top-level ``url`` is what the validator
#: actually accepts.
#:
#: Two of those rejections arrive *inside* a long-running operation -- the POST
#: returns 200 and the create fails a second later -- which is why the publisher
#: waits for `done`. Reading the 200 as success reported nine agents published
#: when the registry held none.
CARD_VERSION: Final[str] = "0.3.0"


def service_id_for(descriptor: AgentDescriptor) -> str:
    """The registry resource id for one agent.

    Derived from the agent id, so republishing updates the same entry rather
    than accumulating one per deployment -- the same reason every other id in
    this system is derived rather than minted.
    """
    return descriptor.agent_id


def worker_url(descriptor: AgentDescriptor, *, base_url: str) -> str:
    """Where this agent actually runs.

    Each scheduled agent has its own Cloud Run worker on its own service
    account, so the card names that rather than the incident service -- which is
    both truer and required: the registry refuses two services advertising the
    same interface URL, and nine cards pointing at one host is nine claims that
    the same process is nine agents.

    The URL is not an open door. Every worker's invoker list is the push and
    scheduler identities; a reader of the catalog cannot call it, and every read
    and write it performs still decides at the gateway.
    """
    # The incident host is `firstdue-incident-<hash>-<region>.a.run.app`; a
    # worker is the same hash and region under its own service name.
    host = base_url.split("//", 1)[-1]
    tail = host.split("-", 2)[-1] if host.startswith("firstdue-incident-") else host
    return f"https://firstdue-agent-{descriptor.agent_id}-{tail}"


def agent_card(descriptor: AgentDescriptor, *, base_url: str) -> dict[str, Any]:
    """One descriptor as an A2A Agent Card.

    The skill list is the agent's *declared capabilities*, not a menu of things
    a caller may ask for. Which agents run on an incident is decided by
    :func:`~firstdue.incident.handoff.plan_handoffs` against the incident grant,
    and a card cannot widen that: it describes authority the agent holds, and
    the gateway still decides every read and write.
    """
    return {
        "protocolVersion": CARD_VERSION,
        "name": descriptor.agent_id,
        "description": descriptor.role_summary,
        "version": descriptor.version,
        "url": worker_url(descriptor, base_url=base_url),
        # `provider.url` is required at 0.3.0 and rejected by the v1.0 proto.
        # The two validators disagree; this targets the one that accepts a card.
        "provider": {
            "organization": f"{descriptor.publisher_department} department",
            "url": base_url,
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": descriptor.agent_id,
                "name": descriptor.agent_id,
                "description": descriptor.role_summary,
                # The declared scopes, published as tags. An officer reading the
                # catalog can see what an agent is allowed to touch without
                # opening the code, which is the whole point of publishing a
                # catalog cross-department.
                "tags": sorted(str(scope) for scope in descriptor.required_scopes),
            }
        ],
    }


def service_body(descriptor: AgentDescriptor, *, base_url: str) -> dict[str, Any]:
    """The registry ``Service`` payload wrapping one agent's card."""
    return {
        "displayName": f"{descriptor.agent_id} {descriptor.version}",
        "description": descriptor.role_summary,
        "agentSpec": {
            "type": AGENT_CARD_SPEC,
            "content": agent_card(descriptor, base_url=base_url),
        },
        # No `interfaces`: the API refuses them alongside an A2A card, because
        # the card already carries `url` and `preferredTransport`. One place
        # says where an agent is, which is the right answer -- two would be two
        # things to keep in step.
    }


def publishable() -> tuple[AgentDescriptor, ...]:
    """The agents worth publishing: the scheduled fleet, not the catalog.

    Four superseded descriptors stay resolvable in this system so a recorded run
    replays, but they are routed nowhere and given no worker. Publishing them
    into a discovery catalog would advertise agents nobody can run.
    """
    return ACTIVE_FLEET
