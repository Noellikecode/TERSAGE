"""Pub/Sub wire format, as pure functions.

Kept separate from the Pub/Sub client so the encoding can be tested, replayed,
and reasoned about without credentials, a network, or the Google libraries
installed at all. The push endpoint imports this module; it never imports the
client.

Two conventions worth stating:

**Attributes duplicate a few envelope fields.** Pub/Sub filters and dashboards
read attributes, not payloads, so topic, correlation id, schema version, and
attempt are lifted out. They are a copy, never the source of truth -- the
decoded envelope is. The attribute is ``event_topic`` rather than ``topic``
because the publisher client takes attributes as keyword arguments, and an
attribute named ``topic`` collides with the client's own parameter -- a
``TypeError`` at publish time, on the live path only. :data:`RESERVED_ATTRIBUTE_NAMES`
makes that a validation error at encode time instead.

**Ordering keys are per-entity.** Pub/Sub guarantees order only within an
ordering key. The key is the primary entity the event concerns (an address, an
incident), so two events about one building arrive in order while the queue as a
whole still parallelises across 3,800 of them.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from firstdue.domain.events import EventEnvelope, Topic
from firstdue.errors import ValidationError

#: Pub/Sub message attributes are strings, and small. This bounds abuse.
MAX_ATTRIBUTE_LENGTH: Final[int] = 1024
#: Pub/Sub caps a message at 10 MiB; an id-only envelope is nowhere near it.
MAX_MESSAGE_BYTES: Final[int] = 256 * 1024

#: Parameter names the Pub/Sub publisher client uses itself. An attribute with
#: one of these names is passed as that parameter instead, which fails at
#: publish time rather than here. Checked at encode so it cannot reach the wire.
RESERVED_ATTRIBUTE_NAMES: Final[frozenset[str]] = frozenset(
    {"topic", "data", "ordering_key", "retry", "timeout"}
)

#: The order in which id keys are considered for the ordering key.
_ORDERING_ID_KEYS: Final[tuple[str, ...]] = (
    "incident_id",
    "address_id",
    "district_id",
    "source_id",
)


def topic_name(prefix: str, topic: Topic) -> str:
    """The Pub/Sub topic id for a domain topic, e.g. ``firstdue-fact-written``."""
    return f"{prefix}-{str(topic).replace('.', '-')}"


def subscription_name(prefix: str, topic: Topic, subscriber: str) -> str:
    """The push subscription id for one consumer of one topic."""
    return f"{topic_name(prefix, topic)}-{subscriber}"


def ordering_key(envelope: EventEnvelope) -> str:
    """The entity whose events must stay in order.

    Falls back to the correlation id, which keeps one causal chain ordered even
    for an event that names no entity.
    """
    for key in _ORDERING_ID_KEYS:
        value = envelope.ids.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, tuple) and value:
            return value[0]
    return envelope.correlation_id


def attributes_for(envelope: EventEnvelope) -> dict[str, str]:
    """Filterable metadata. A copy of envelope fields, never the source of truth."""
    attributes = {
        "event_topic": str(envelope.topic),
        "event_id": envelope.event_id,
        "schema_version": str(envelope.schema_version),
        "correlation_id": envelope.correlation_id,
        "producer": envelope.producer,
        "producer_version": envelope.producer_version,
        "idempotency_key": envelope.idempotency_key,
        "attempt": str(envelope.retry.attempt),
        "max_attempts": str(envelope.retry.max_attempts),
    }
    if envelope.causation_id:
        attributes["causation_id"] = envelope.causation_id
    collisions = RESERVED_ATTRIBUTE_NAMES & set(attributes)
    if collisions:  # pragma: no cover - a guard against a future edit
        raise ValidationError(
            "attribute name collides with a Pub/Sub client parameter",
            details={"names": sorted(collisions)},
        )
    return {k: v[:MAX_ATTRIBUTE_LENGTH] for k, v in attributes.items()}


class PubSubMessage(BaseModel):
    """One message as Pub/Sub carries it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: bytes
    attributes: dict[str, str] = Field(default_factory=dict)
    ordering_key: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def encode(envelope: EventEnvelope) -> PubSubMessage:
    """Serialize an envelope for publication."""
    payload = json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValidationError(
            "event envelope exceeds the message size limit",
            details={"bytes": len(payload), "max": MAX_MESSAGE_BYTES},
        )
    return PubSubMessage(
        data=payload,
        attributes=attributes_for(envelope),
        ordering_key=ordering_key(envelope),
    )


def decode(data: bytes) -> EventEnvelope:
    """Parse an envelope from message data.

    Raises:
        ValidationError: for anything unparseable. The dispatcher classifies
            that as a poison message and dead-letters it on the first attempt --
            redelivering bytes that are not an envelope cannot help.
    """
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValidationError(
            "event message exceeds the size limit",
            details={"bytes": len(data), "max": MAX_MESSAGE_BYTES},
        )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "event message is not valid JSON", details={"reason": type(exc).__name__}
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationError("event message is not an object")
    try:
        return EventEnvelope.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "event message is not a valid envelope",
            details={"fields": sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})},
        ) from exc


class PushEnvelope(BaseModel):
    """The body Pub/Sub POSTs to a push endpoint."""

    model_config = ConfigDict(extra="ignore")

    message: dict[str, Any]
    subscription: str = ""

    @property
    def message_id(self) -> str:
        value = self.message.get("messageId") or self.message.get("message_id") or ""
        return str(value)

    @property
    def delivery_attempt(self) -> int | None:
        """Pub/Sub's own attempt counter, present when a dead-letter policy is set."""
        raw = self.message.get("deliveryAttempt") or self.message.get("delivery_attempt")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def attributes(self) -> dict[str, str]:
        raw = self.message.get("attributes") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def decoded_data(self) -> bytes:
        """Base64-decode the message body.

        Raises:
            ValidationError: when the body is missing or not base64. Both are
                poison: nothing about redelivering them changes the outcome.
        """
        raw = self.message.get("data")
        if raw is None:
            raise ValidationError("push message carries no data")
        if isinstance(raw, bytes):
            return raw
        try:
            return base64.b64decode(str(raw), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(
                "push message data is not valid base64", details={"reason": type(exc).__name__}
            ) from exc


def decode_push(body: dict[str, Any]) -> EventEnvelope:
    """Parse a Pub/Sub push body into an envelope."""
    try:
        push = PushEnvelope.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError(
            "not a Pub/Sub push body",
            details={"fields": sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})},
        ) from exc
    return decode(push.decoded_data())


def encode_push(envelope: EventEnvelope, *, subscription: str = "") -> dict[str, Any]:
    """Build a push body for an envelope. Used by tests and the local harness."""
    message = encode(envelope)
    return {
        "message": {
            "data": base64.b64encode(message.data).decode("ascii"),
            "attributes": message.attributes,
            "messageId": envelope.event_id,
            "orderingKey": message.ordering_key,
        },
        "subscription": subscription,
    }
