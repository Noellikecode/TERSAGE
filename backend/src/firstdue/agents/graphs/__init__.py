"""Reasoning graphs for the two slow-loop agents that have to look things up.

``hazard-watcher`` cross-checks federal registries against each other until a
facility's identity is settled; ``records-watcher`` follows municipal filings to
the filings they cite until nothing is outstanding. Different data, one loop --
which is why :mod:`firstdue.agents.graphs.base` exists and why neither of the
two modules beside it owns a budget, a step bound, a span, or a trace.

Nothing in this package writes a fact. The graphs decide what to read and when
to stop; the deterministic paths in
:mod:`firstdue.agents.hazard_watcher` and :mod:`firstdue.agents.records_watcher`
turn what they read into facts, unchanged, with their spans and provenance
intact.
"""

from __future__ import annotations

from firstdue.agents.graphs.base import (
    BudgetGuard,
    FixedOrderPlanner,
    GraphCassette,
    GraphRun,
    GraphSpec,
    GraphState,
    GraphStop,
    GraphTrace,
    NodeRecord,
    NodeResult,
    ReasoningPlanner,
    VertexReasoningPlanner,
    graph_budget,
    park,
    run_graph,
)
from firstdue.agents.graphs.hazard import (
    FacilityAmbiguity,
    HazardCrossCheck,
    HazardGraphState,
    detect_ambiguities,
    normalize_facility_name,
)
from firstdue.agents.graphs.records import (
    RecordsGraphState,
    RecordsRetrieval,
    references_in,
)

__all__ = [
    "BudgetGuard",
    "FacilityAmbiguity",
    "FixedOrderPlanner",
    "GraphCassette",
    "GraphRun",
    "GraphSpec",
    "GraphState",
    "GraphStop",
    "GraphTrace",
    "HazardCrossCheck",
    "HazardGraphState",
    "NodeRecord",
    "NodeResult",
    "ReasoningPlanner",
    "RecordsGraphState",
    "RecordsRetrieval",
    "VertexReasoningPlanner",
    "detect_ambiguities",
    "graph_budget",
    "normalize_facility_name",
    "park",
    "references_in",
    "run_graph",
]
