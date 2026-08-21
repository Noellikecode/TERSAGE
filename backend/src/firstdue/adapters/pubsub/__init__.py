"""Pub/Sub-backed event transport.

Imports of the Google client are deferred to first use, so importing this
package -- and running the whole test suite against it -- costs nothing and
requires nothing. Fake mode never touches the client at all.
"""

from __future__ import annotations

from firstdue.adapters.pubsub.bus import PubSubEventBus, PubSubPublisher

__all__ = ["PubSubEventBus", "PubSubPublisher"]
