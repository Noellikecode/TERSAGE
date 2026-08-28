"""Pull delivery, so a laptop can run on the real event fabric.

**The problem this solves is deployment shape, not semantics.** In production
every subscription is push: Pub/Sub posts each envelope to a Cloud Run service's
authenticated ``/internal/events/push`` endpoint, and
:meth:`~firstdue.adapters.pubsub.bus.PubSubEventBus.handle_push` routes it to the
local subscribers. Google cannot post to ``localhost``, so a process running on a
laptop with ``EVENT_BACKEND=pubsub`` publishes real events and receives none --
agents are never woken, and the fleet looks broken while the bus works perfectly.

The choice that produces is a bad one: run the demo on an in-memory bus and
claim Pub/Sub, or run on Pub/Sub and watch nothing arrive. This is the third
option. It **pulls** from its own subscriptions and hands each envelope to the
same ``handle_push`` the push endpoint calls, so the delivery policy -- dedupe,
retry classification, breakers, dead-lettering -- is the one already under test.
Nothing about how an event is *handled* changes; only how it arrives.

**Its subscriptions are its own, and they are disposable.** It never attaches to
the deployed push subscriptions -- a subscription is push or pull, and
converting one would stop delivering to the Cloud Run service that owns it.
Instead it creates ``{prefix}-{topic}`` pull subscriptions, which are additive:
Pub/Sub fans a topic out to every subscription, so a local run observes the same
events the deployed fleet does without taking any of them away from it.

**It is off unless asked for.** A background loop that quietly consumed a
production topic would be the kind of thing nobody notices until an event goes
missing from the service that was supposed to get it. It requires
``PUBSUB_PULL_BRIDGE=true`` *and* ``EVENT_BACKEND=pubsub``, and the demo target
is the only thing that sets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final, Protocol, runtime_checkable

from firstdue.adapters.pubsub.bus import PubSubEventBus
from firstdue.domain.events import Topic
from firstdue.errors import ConfigurationError
from firstdue.eventing.pubsub_codec import decode, topic_name
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

#: How long a pull waits for messages before returning empty. Long enough that
#: an idle bus is not a busy loop, short enough that shutdown is not a wait.
DEFAULT_PULL_TIMEOUT_S: Final[float] = 5.0

#: How many envelopes one pull may return. A dispatch fan-out is a handful.
DEFAULT_MAX_MESSAGES: Final[int] = 25

#: Redelivery window. Generous next to an agent's own budget, because a message
#: redelivered while its first delivery is still working is a duplicate the
#: dispatcher then has to dedupe -- correct, and wasted work.
DEFAULT_ACK_DEADLINE_S: Final[int] = 120


@runtime_checkable
class PubSubSubscriber(Protocol):
    """The slice of the Pub/Sub subscriber client this bridge uses."""

    def subscription_path(self, project: str, subscription: str) -> str: ...

    def topic_path(self, project: str, topic: str) -> str: ...

    def create_subscription(self, request: dict[str, Any]) -> Any: ...

    def pull(self, request: dict[str, Any], timeout: float | None = None) -> Any: ...

    def acknowledge(self, request: dict[str, Any]) -> Any: ...


def build_subscriber() -> PubSubSubscriber:
    """The real client, imported here so nothing else needs the dependency."""
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:  # pragma: no cover - exercised by the live path
        raise ConfigurationError(
            "the Pub/Sub pull bridge requires google-cloud-pubsub; install the "
            "'google' extra, or leave PUBSUB_PULL_BRIDGE unset"
        ) from exc
    client: PubSubSubscriber = pubsub_v1.SubscriberClient()
    return client


class PubSubPullBridge:
    """Pulls from its own subscriptions and delivers through the bus.

    One task per topic. Topics are independent and a slow handler on one must
    not hold up another -- which is the same reason the deployed fleet gives
    each agent its own push subscription rather than one endpoint that
    demultiplexes.
    """

    def __init__(
        self,
        *,
        bus: PubSubEventBus,
        project_id: str,
        subscriber: PubSubSubscriber,
        prefix: str,
        topic_prefix: str = "",
        pull_timeout_s: float = DEFAULT_PULL_TIMEOUT_S,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        ack_deadline_s: int = DEFAULT_ACK_DEADLINE_S,
    ) -> None:
        if not project_id:
            raise ConfigurationError("the Pub/Sub pull bridge requires GCP_PROJECT_ID")
        if not prefix:
            raise ConfigurationError(
                "the Pub/Sub pull bridge requires a subscription prefix; an "
                "unprefixed name could collide with a deployed subscription"
            )
        self._bus = bus
        self._project_id = project_id
        self._subscriber = subscriber
        self._prefix = prefix
        self._topic_prefix = topic_prefix
        self._pull_timeout_s = pull_timeout_s
        self._max_messages = max_messages
        self._ack_deadline_s = ack_deadline_s
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        #: Counters, for the log line on shutdown. A bridge that delivered
        #: nothing is worth knowing about, and silence would not say so.
        self.delivered = 0
        self.undecodable = 0

    def subscription_id(self, topic: Topic) -> str:
        """The name of this bridge's own subscription for a topic."""
        return f"{self._prefix}-{topic_name(self._topic_prefix, topic)}"

    async def start(self, topics: Sequence[Topic]) -> None:
        """Ensure a subscription per topic and begin pulling.

        Idempotent: a subscription that already exists is reused rather than
        recreated, so restarting the demo does not lose messages that arrived
        between runs.
        """
        for topic in topics:
            await asyncio.to_thread(self._ensure_subscription, topic)
        for topic in topics:
            self._tasks.append(asyncio.create_task(self._run(topic), name=f"pull:{topic}"))
        logger.info(
            "pubsub_pull_bridge_started",
            extra={"topics": len(topics), "prefix": self._prefix},
        )

    async def stop(self) -> None:
        """Stop pulling and wait for the loops to unwind."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        # Cancelled tasks are awaited rather than abandoned: a pull in flight
        # holds a connection, and leaving it to be collected produces the
        # "Task was destroyed but it is pending" noise that hides real errors.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info(
            "pubsub_pull_bridge_stopped",
            extra={"delivered": self.delivered, "undecodable": self.undecodable},
        )

    # ------------------------------------------------------------ internals

    def _ensure_subscription(self, topic: Topic) -> None:
        """Create this bridge's subscription, or accept that it already exists."""
        path = self._subscriber.subscription_path(self._project_id, self.subscription_id(topic))
        try:
            self._subscriber.create_subscription(
                request={
                    "name": path,
                    "topic": self._subscriber.topic_path(
                        self._project_id, topic_name(self._topic_prefix, topic)
                    ),
                    "ack_deadline_seconds": self._ack_deadline_s,
                }
            )
            logger.info("pubsub_pull_subscription_created", extra={"subscription": path})
        except Exception as exc:
            # `AlreadyExists` is the ordinary case on a second run and is not an
            # error. Anything else is: a bridge that could not subscribe would
            # sit silently and look exactly like a quiet bus.
            if type(exc).__name__ != "AlreadyExists":
                raise

    async def _run(self, topic: Topic) -> None:
        path = self._subscriber.subscription_path(self._project_id, self.subscription_id(topic))
        while not self._stopping.is_set():
            try:
                response = await asyncio.to_thread(self._pull_once, path)
            except asyncio.CancelledError:
                raise
            except Exception:
                # An outage on one topic must not end the loop: the bus is
                # already the thing that reports delivery failure, and a bridge
                # that exited on the first blip would go quiet for the rest of
                # the run without saying so.
                logger.warning("pubsub_pull_failed", extra={"topic": str(topic)}, exc_info=True)
                await asyncio.sleep(1.0)
                continue

            received = list(getattr(response, "received_messages", []) or [])
            if not received:
                continue

            ack_ids: list[str] = []
            for message in received:
                if await self._deliver(topic, message):
                    ack_ids.append(message.ack_id)
            if ack_ids:
                await asyncio.to_thread(self._ack, path, ack_ids)

    def _pull_once(self, path: str) -> Any:
        return self._subscriber.pull(
            request={"subscription": path, "max_messages": self._max_messages},
            timeout=self._pull_timeout_s,
        )

    def _ack(self, path: str, ack_ids: list[str]) -> None:
        self._subscriber.acknowledge(request={"subscription": path, "ack_ids": ack_ids})

    async def _deliver(self, topic: Topic, message: Any) -> bool:
        """Hand one message to the bus. ``True`` when it may be acked.

        An undecodable message is acked, not retried. Redelivering bytes that
        will not parse is an infinite loop with a cost attached, and the
        envelope cannot reach a dead-letter store that is keyed by envelope.
        """
        data = getattr(getattr(message, "message", None), "data", b"")
        try:
            envelope = decode(data)
        except Exception:
            self.undecodable += 1
            logger.warning("pubsub_pull_undecodable", extra={"topic": str(topic)})
            return True

        # `subscriber=None` fans out to every local subscriber of the topic.
        # The deployed push path names one because Pub/Sub pushes per
        # subscription; this bridge holds one subscription per *topic*, so the
        # process's own registrations are what decide who gets it.
        await self._bus.handle_push(envelope)
        self.delivered += 1
        return True
