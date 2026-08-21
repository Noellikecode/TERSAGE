"""Which agent consumes which topic.

The handoff diagram in the architecture doc has always described this, and
until now nothing in the system held it as data. That was survivable while one
process ran the whole fleet and every subscription pushed to the same endpoint.
It stops being survivable the moment each agent is its own Cloud Run service:
a push subscription has to name *which* service receives it, and a subscription
pointed at a service that does not handle its topic is an event that dead-letters
forever while looking perfectly healthy.

Phase 2's notes recorded exactly this risk -- "in a fleet where each Cloud Run
service subscribes to different topics, the push subscription must name the
right service, which is Terraform's job and not yet written". This is the data
Terraform needs, held in code so a test can refuse to let the two disagree.

**Publishing is not routed.** Any agent may publish any topic it is authorised
to; only *consumption* needs a destination, because only consumption needs
somewhere to deliver to.
"""

from __future__ import annotations

from typing import Final

from firstdue.domain.events import Topic

#: Topic consumption, per agent. Empty means the agent is driven by something
#: other than the bus -- a scheduler tick, or an HTTP request from the console.
CONSUMES: Final[dict[str, frozenset[Topic]]] = {
    # Driven by Cloud Scheduler through /internal/scheduler/tick, not the bus.
    "records-watcher": frozenset({Topic.SOURCE_POLL}),
    "hazard-watcher": frozenset({Topic.SOURCE_POLL}),
    # Geometry re-derives when a permit invalidates what it measured, which is
    # the dependency that keeps the two watchers from both polling blindly.
    "geometry-watcher": frozenset({Topic.SOURCE_POLL, Topic.GEOMETRY_STALE}),
    # The engine runs on every new fact, which is what makes a disagreement
    # surface within one pass rather than at the next sweep.
    "conflict-detector": frozenset({Topic.FACT_WRITTEN, Topic.FACT_OBSERVED}),
    # Ranking is a sweep, and a new conflict is worth re-ranking for.
    "survey-ranker": frozenset({Topic.CONFLICT_DETECTED, Topic.PROFILE_MATERIALIZED}),
    # A referral is drafted from a queue row and filed when a human approves.
    "referral-clerk": frozenset({Topic.QUEUE_RANKED, Topic.APPROVAL_STAGED}),
    # The incident loop opens on a CAD dispatch, which arrives over HTTP.
    "incident-controller": frozenset({Topic.INCIDENT_OPENED, Topic.INCIDENT_CLOSED}),
    # The brief is rebuilt when the incident opens and amended when anything
    # observed about the building changes.
    "brief-reconciler": frozenset(
        {Topic.INCIDENT_OPENED, Topic.FACT_OBSERVED, Topic.CONFLICT_DETECTED}
    ),
    "sensor-fusion": frozenset({Topic.THERMAL_FRAME_RECEIVED}),
    "agency-notifier": frozenset({Topic.INCIDENT_OPENED, Topic.APPROVAL_STAGED}),
    # The recorder subscribes to everything the incident produces, because the
    # log is meant to be complete rather than selective.
    "incident-recorder": frozenset(
        {
            Topic.INCIDENT_OPENED,
            Topic.BRIEF_EMITTED,
            Topic.NOTIFICATION_SENT,
            Topic.FACT_OBSERVED,
            Topic.APPROVAL_STAGED,
            Topic.INCIDENT_CLOSED,
            Topic.RECORD_WRITTEN,
        }
    ),
}


def consumers_of(topic: Topic) -> tuple[str, ...]:
    """Every agent that consumes a topic, in a stable order."""
    return tuple(sorted(agent for agent, topics in CONSUMES.items() if topic in topics))


def topics_for(agent_id: str) -> tuple[str, ...]:
    """Every topic one agent consumes, in a stable order."""
    return tuple(sorted(str(topic) for topic in CONSUMES.get(agent_id, frozenset())))


def unconsumed_topics() -> tuple[str, ...]:
    """Topics nothing subscribes to.

    Not an error. ``agent.published`` is a catalog notification that exists for
    operators and other departments rather than for an agent in this fleet, and
    a topic with no consumer in *this* deployment is a normal thing for a
    cross-department bus to carry. It is surfaced so it is a decision rather
    than an oversight.
    """
    consumed = {topic for topics in CONSUMES.values() for topic in topics}
    return tuple(sorted(str(topic) for topic in Topic if topic not in consumed))


__all__ = ["CONSUMES", "consumers_of", "topics_for", "unconsumed_topics"]
