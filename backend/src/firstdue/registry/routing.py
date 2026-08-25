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
    # Detection runs on every new fact, so a disagreement surfaces within one
    # pass rather than at the next sweep; ranking then re-runs on what it just
    # found. One agent, one reading of the corpus, so the severity and the rank
    # cannot disagree about what it said.
    "structure-watch": frozenset(
        {
            Topic.FACT_WRITTEN,
            Topic.FACT_OBSERVED,
            Topic.CONFLICT_DETECTED,
            Topic.PROFILE_MATERIALIZED,
        }
    ),
    # A referral is drafted from a queue row and filed when a human approves.
    "referral-clerk": frozenset({Topic.QUEUE_RANKED, Topic.APPROVAL_STAGED}),
    # The incident loop opens on a CAD dispatch, which arrives over HTTP. The
    # brief is then rebuilt on open and amended whenever anything observed
    # about the building changes -- one agent for both, because stage one and
    # the stages after it are the same document.
    "incident-interceptor": frozenset(
        {
            Topic.INCIDENT_OPENED,
            Topic.INCIDENT_CLOSED,
            Topic.FACT_OBSERVED,
            Topic.CONFLICT_DETECTED,
        }
    ),
    "sensor-fusion": frozenset({Topic.THERMAL_FRAME_RECEIVED}),
    # Woken by the incident head's plan, not by the incident opening.
    #
    # This subscribed to `incident.opened` and that was the bug: whether the
    # notifier runs is a *routing* decision -- `plan_handoffs` matches the rule's
    # required scopes against the incident grant and withholds the wake when the
    # grant cannot cover them. Started by Pub/Sub instead, it ran regardless, so
    # in the deployed topology a withheld handoff was a refusal recorded in the
    # log and contradicted by the transport. `agent.wake` carries the plan's
    # decision across the process boundary; `approval.staged` stays, because a
    # chief approving a shutoff is an announcement and not a routing decision.
    "agency-notifier": frozenset({Topic.AGENT_WAKE, Topic.APPROVAL_STAGED}),
    # The recorder subscribes to everything the incident produces, because the
    # log is meant to be complete rather than selective.
    # The recorder keeps its blanket subscription, deliberately. It is the
    # append-only log, and its job is completeness rather than selection: a log
    # that only recorded the incidents somebody routed it to would be a log with
    # holes exactly where a routing decision went wrong. It reads and writes the
    # log and nothing else, so there is no authority here for a plan to gate.
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
