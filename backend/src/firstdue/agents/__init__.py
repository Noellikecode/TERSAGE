"""The slow-loop fleet.

Four agents run continuously against a district: three watchers that turn
sources into facts, and ``structure-watch``, which reads what they accumulated
and turns it into the queue a battalion chief actually works from. None of them
decides anything -- detection, scoring, and merge order all come out of the pure
engines in ``domain/``.

``structure-watch`` supersedes the split ``conflict-detector`` and
``survey-ranker``: a conflict's severity and a structure's rank now come from
one reading of one profile set, so they cannot disagree about what the corpus
said. See :mod:`firstdue.agents.structure_watch`.
"""

from __future__ import annotations

from firstdue.agents.geometry_watcher import GeometryWatcher, GeometryWatchResult
from firstdue.agents.hazard_watcher import HazardWatcher
from firstdue.agents.records_watcher import RecordsWatcher, WatchResult
from firstdue.agents.structure_watch import (
    DistrictReading,
    ProfileReading,
    RankedConflict,
    StructureWatch,
    StructureWatchResult,
)

__all__ = [
    "DistrictReading",
    "GeometryWatchResult",
    "GeometryWatcher",
    "HazardWatcher",
    "ProfileReading",
    "RankedConflict",
    "RecordsWatcher",
    "StructureWatch",
    "StructureWatchResult",
    "WatchResult",
]
