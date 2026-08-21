"""Dead letters that survive a restart.

Phase 2 shipped an in-memory dead-letter store and recorded the gap in its own
risk table: "they survive a request but not a restart." On Cloud Run that means
a dead letter lives until the next instance swap, which is the same as saying
the queue's failures are invisible.

This publishes each one to a dedicated Pub/Sub dead-letter topic. The envelope
travels as its own message so the record is complete, and the reason travels as
an attribute so an operator can filter without decoding payloads.

Pub/Sub also has its own dead-letter policy, set in Terraform on every
subscription. The two are complementary rather than redundant: Pub/Sub's fires
when *delivery* keeps failing, this one fires when delivery succeeded and the
*handler* refused. A message the fleet declined to process is not the same
failure as a message the fleet never received.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from firstdue.domain.events import Topic
from firstdue.eventing.deadletter import DeadLetterRecord, InMemoryDeadLetterStore
from firstdue.eventing.pubsub_codec import encode
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

#: Appended to the topic prefix. Terraform creates the matching topic.
DEAD_LETTER_SUFFIX: Final[str] = "dead-letter"


def dead_letter_topic(prefix: str) -> str:
    return f"{prefix}-{DEAD_LETTER_SUFFIX}"


class PubSubDeadLetterStore:
    """Publishes dead letters, and keeps the recent ones readable in-process.

    Both, deliberately. The Pub/Sub topic is the durable record; the in-memory
    tail is what the operator endpoint serves, so listing dead letters does not
    require a subscription pull on the request path.
    """

    def __init__(
        self,
        *,
        project_id: str,
        topic_prefix: str,
        publisher: Any | None = None,
    ) -> None:
        self._project_id = project_id
        self._topic = dead_letter_topic(topic_prefix)
        self._publisher = publisher
        self._recent = InMemoryDeadLetterStore()
        self.published = 0
        self.publish_failures = 0

    def _client(self) -> Any:  # pragma: no cover - live mode only
        if self._publisher is None:
            from firstdue.adapters.pubsub.bus import build_publisher

            self._publisher = build_publisher(enable_ordering=False)
        return self._publisher

    async def add(self, record: DeadLetterRecord) -> DeadLetterRecord:
        """Record a dead letter durably, and keep it readable.

        A failure to publish does not raise. The message already failed; losing
        the *notification* about it must not also fail the caller that was
        trying to record it. The in-memory copy still holds, and the failure is
        counted and logged.
        """
        await self._recent.add(record)
        try:  # pragma: no cover - live mode only
            client = self._client()
            message = encode(record.envelope)
            client.publish(
                client.topic_path(self._project_id, self._topic),
                message.data,
                reason=record.reason,
                subscriber=record.subscriber,
                failure_class=str(record.failure_class),
                attempts=str(record.attempts),
            )
            self.published += 1
        except Exception as exc:
            self.publish_failures += 1
            logger.error(
                "dead_letter_publish_failed",
                extra={"topic": self._topic, "error_type": type(exc).__name__},
            )
        return record

    async def list_all(
        self, *, subscriber: str | None = None, topic: Topic | None = None
    ) -> Sequence[DeadLetterRecord]:
        return await self._recent.list_all(subscriber=subscriber, topic=topic)

    @property
    def records(self) -> Sequence[DeadLetterRecord]:
        return self._recent.records

    @property
    def count(self) -> int:
        return self._recent.count

    def clear(self) -> None:
        self._recent.clear()
