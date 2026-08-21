"""Process lifecycle state.

Liveness and readiness are different questions. The process can be alive and
still not ready to receive traffic -- during startup, or while draining after
SIGTERM. Conflating them causes a load balancer to send an incident to a
process that is on its way out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Lifecycle:
    """Tracks whether this process should receive new work."""

    started_at: datetime | None = None
    ready: bool = False
    draining: bool = False
    _notes: dict[str, str] = field(default_factory=dict)

    def mark_started(self, at: datetime) -> None:
        self.started_at = at
        self.ready = True
        self.draining = False

    def begin_drain(self) -> None:
        """SIGTERM received: stop advertising readiness, finish in-flight work."""
        self.draining = True
        self.ready = False

    @property
    def accepts_traffic(self) -> bool:
        return self.ready and not self.draining

    @property
    def state(self) -> str:
        if self.draining:
            return "draining"
        return "ready" if self.ready else "starting"

    def note(self, key: str, value: str) -> None:
        self._notes[key] = value

    @property
    def notes(self) -> dict[str, str]:
        return dict(self._notes)
