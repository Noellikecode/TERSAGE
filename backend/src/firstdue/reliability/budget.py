"""One budget arithmetic, shared by both agent runtimes.

A deadline policy that lives in one runtime is a deadline policy the other one
gets wrong -- the same argument that put one ``EventDispatcher`` behind both
event transports. What fake mode proves about how long an agent may run is now
literally what the live path enforces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from firstdue.domain.registry import AgentDescriptor

#: Used when a descriptor declares no latency target. Every shipped descriptor
#: does declare one; this exists so an externally published agent cannot run
#: unbounded just because its publisher left the field out.
FALLBACK_DEADLINE_MS: Final[int] = 30_000


def budget_seconds(
    descriptor: AgentDescriptor, deadline: datetime | None, started: datetime
) -> float:
    """The tighter of the caller's deadline and the descriptor's own budget.

    A descriptor declares ``latency_target_ms`` and that is a promise the
    catalog makes about the agent. Letting a run exceed it because nobody
    passed a deadline would make the catalog a decoration.
    """
    declared = float(descriptor.latency_target_ms or FALLBACK_DEADLINE_MS) / 1000.0
    if deadline is None:
        return declared
    remaining = max(0.0, (deadline - started).total_seconds())
    return min(declared, remaining) or declared
