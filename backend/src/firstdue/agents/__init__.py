"""The slow-loop fleet.

Four agents run continuously against a district: three watchers that turn
sources into facts, and a ranker that turns accumulated facts into the queue a
battalion chief actually works from. None of them decides anything -- detection,
scoring, and merge order all come out of the pure engines in ``domain/``.
"""

from __future__ import annotations

from firstdue.agents.geometry_watcher import GeometryWatcher, GeometryWatchResult
from firstdue.agents.hazard_watcher import HazardWatcher
from firstdue.agents.ranker import DeltaRanker, RankedQueue
from firstdue.agents.records_watcher import RecordsWatcher, WatchResult

__all__ = [
    "DeltaRanker",
    "GeometryWatchResult",
    "GeometryWatcher",
    "HazardWatcher",
    "RankedQueue",
    "RecordsWatcher",
    "WatchResult",
]
