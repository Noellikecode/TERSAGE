"""A* over a weighted graph. No I/O, no clock, no domain.

This module knows nothing about buildings. It holds points in three dimensions,
edges between them, and a shortest-path search -- which is exactly why it can be
tested against graphs whose optimum is obvious by inspection, and why a bug in
the cost model cannot hide behind a bug in the search.

**Why A\\*, and why this heuristic.** Every edge here costs at least its own
straight-line length: :attr:`NavEdge.cost` is ``length_m * (1 + sum of weights)``
and every weight is non-negative, so the multiplier is never below one. The
straight-line distance from a node to the goal is therefore never more than the
cheapest remaining path to it -- the definition of an admissible heuristic --
and because the same bound holds edge by edge it is also consistent, so no node
is ever reopened. Dijkstra is the same algorithm with ``h = 0``; a test runs both
over the same graph and asserts the same total cost, which is the check that the
heuristic is really admissible rather than merely believed to be.

The heuristic is a *lower* bound on purpose. A weighted heuristic would find a
path faster and could find a worse one, and "faster" is worth nothing on a graph
of a few dozen nodes while "worse" means a crew walking past the wall that was
measured cool.

**Ties are broken, not left to chance.** The frontier is ordered by
``(f, g, node_id)`` and every node's neighbours are relaxed in sorted id order,
so two runs over the same graph return the same path -- not merely two paths of
the same cost. An entry plan that changed between the screen and the printout
would be indistinguishable from one that had been amended.

**Refusal is a value.** A disconnected graph returns :class:`PathRefusal`
naming what was unreachable. There is no code path that returns a partial path,
a nearest-node fallback, or a straight line: a route to somewhere the crew did
not ask to go is worse than being told the data does not support a route.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Ceiling on how many nodes one search may expand. A fireground graph is a few
#: dozen nodes; anything past this is a graph that was built wrong, and running
#: it to exhaustion would be a slow failure rather than a fast one.
MAX_EXPANSIONS: Final[int] = 20_000


class NavNode(BaseModel):
    """One place a crew can stand, in metres.

    Coordinates are whatever frame the caller builds the graph in -- this module
    only ever subtracts them from each other. ``level`` is carried through
    untouched so a renderer can draw the path on the right storey.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1, max_length=120)
    #: Metres, east / north / up.
    x_m: float
    y_m: float
    z_m: float = 0.0
    #: What kind of place this is -- ``staging``, ``approach``, ``door``,
    #: ``interior``, ``core``. Free text to this module; the builder gives it
    #: meaning.
    kind: str = Field(min_length=1, max_length=40)
    #: The face this node belongs to, where it belongs to one.
    face: str = Field(default="", max_length=40)
    #: Storey index, zero-based, or ``None`` for anything outside the building.
    level: int | None = Field(default=None, ge=0)

    def distance_to(self, other: NavNode) -> float:
        """Straight-line distance in three dimensions."""
        return math.sqrt(
            (self.x_m - other.x_m) ** 2 + (self.y_m - other.y_m) ** 2 + (self.z_m - other.z_m) ** 2
        )


class CostTerm(BaseModel):
    """One reason a leg costs more than its length.

    A term is a *multiplier contribution*, not an absolute penalty: it scales
    with the distance travelled through the condition it describes, which is
    what makes a short hop past a hot wall cheaper than a long one and stops a
    fixed penalty from dominating the arithmetic on a small building.

    ``refs`` are fact ids, canonical keys, face labels and conflict ids -- the
    same rule the incident log keeps. A term that carried a measurement would be
    a second, uncited copy of a reading that already has a provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    term_id: str = Field(min_length=1, max_length=60)
    weight: float = Field(ge=0.0)
    #: One sentence an officer reads. States what was measured and where from.
    detail: str = Field(min_length=1, max_length=300)
    refs: tuple[str, ...] = ()


class NavEdge(BaseModel):
    """A traversable leg, and every reason it costs what it costs.

    Undirected in effect: the builder adds both directions, because a crew walks
    back out the way the arithmetic said to walk in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_id: str = Field(min_length=1, max_length=120)
    to_id: str = Field(min_length=1, max_length=120)
    #: Real metres, from real coordinates. Never a proxy or a hop count.
    length_m: float = Field(gt=0.0)
    terms: tuple[CostTerm, ...] = ()

    @property
    def multiplier(self) -> float:
        """One plus every risk weight. Never below one, which is what keeps
        the straight-line heuristic admissible."""
        return 1.0 + sum(term.weight for term in self.terms)

    @property
    def cost(self) -> float:
        return self.length_m * self.multiplier


class Barrier(BaseModel):
    """A leg that was not built, and why.

    Recorded rather than dropped. A face the arithmetic refused to cross is the
    single most useful thing on an entry plan, and an edge that simply does not
    exist explains nothing to the officer looking at the detour it caused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_id: str = Field(min_length=1, max_length=120)
    to_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=300)
    refs: tuple[str, ...] = ()


class NavGraph(BaseModel):
    """Nodes, edges, and the legs that were refused.

    Frozen and fully derived, like every other decision record in this system:
    rebuilding it from the same geometry and the same coverage produces the same
    graph, which is what a replay checks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[NavNode, ...] = ()
    edges: tuple[NavEdge, ...] = ()
    barriers: tuple[Barrier, ...] = ()

    @property
    def by_id(self) -> dict[str, NavNode]:
        return {node.node_id: node for node in self.nodes}

    @property
    def adjacency(self) -> dict[str, tuple[NavEdge, ...]]:
        """Outgoing edges per node, sorted by destination id.

        Sorted here rather than at relaxation time so the order is a property of
        the graph rather than of the search, and so two searches over one graph
        cannot disagree about which of two equal-cost neighbours came first.
        """
        buckets: dict[str, list[NavEdge]] = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.from_id in buckets:
                buckets[edge.from_id].append(edge)
        return {
            node_id: tuple(sorted(items, key=lambda e: e.to_id))
            for node_id, items in buckets.items()
        }


class PathLeg(BaseModel):
    """One step of a route, with why it was taken and what it was taken over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_id: str = Field(min_length=1, max_length=120)
    to_id: str = Field(min_length=1, max_length=120)
    distance_m: float = Field(ge=0.0)
    cost: float = Field(ge=0.0)
    multiplier: float = Field(ge=1.0)
    terms: tuple[CostTerm, ...] = ()
    #: What was rejected at this node, in the search's own arithmetic: the
    #: alternatives that were more expensive, and the legs that did not exist.
    avoided: tuple[str, ...] = ()


class PathSolution(BaseModel):
    """A route the search actually found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_ids: tuple[str, ...] = Field(min_length=1)
    legs: tuple[PathLeg, ...] = ()
    total_cost: float = Field(ge=0.0)
    total_distance_m: float = Field(ge=0.0)
    #: How many nodes the search settled. Reported because it is the one honest
    #: measure of how hard the answer was to find.
    expanded: int = Field(ge=0)


class PathRefusal(BaseModel):
    """No route, and the reason there is none. Never an empty path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=400)
    #: Node ids, face labels, or barrier reasons -- whatever names the gap.
    refs: tuple[str, ...] = ()
    expanded: int = Field(default=0, ge=0)


PathOutcome = PathSolution | PathRefusal


def euclidean_heuristic(node: NavNode, goal: NavNode) -> float:
    """Straight-line distance. Admissible because no edge costs less than its
    own length, so no path can be shorter than the straight line."""
    return node.distance_to(goal)


def astar(
    graph: NavGraph,
    *,
    start_id: str,
    goal_id: str,
    use_heuristic: bool = True,
) -> PathOutcome:
    """Cheapest route from ``start_id`` to ``goal_id``, or a stated refusal.

    ``use_heuristic=False`` is Dijkstra -- the same search with ``h = 0``. It
    exists so a test can assert the two agree on the same graph, which is the
    only way to check an admissible heuristic rather than assume one.
    """
    nodes = graph.by_id
    if start_id not in nodes:
        return PathRefusal(reason="the start node is not in the graph", refs=(start_id,))
    if goal_id not in nodes:
        return PathRefusal(reason="the goal node is not in the graph", refs=(goal_id,))
    if start_id == goal_id:
        return PathSolution(
            node_ids=(start_id,), legs=(), total_cost=0.0, total_distance_m=0.0, expanded=0
        )

    adjacency = graph.adjacency
    goal = nodes[goal_id]

    def h(node_id: str) -> float:
        return euclidean_heuristic(nodes[node_id], goal) if use_heuristic else 0.0

    g_score: dict[str, float] = {start_id: 0.0}
    came_from: dict[str, NavEdge] = {}
    settled: set[str] = set()
    # ``(f, g, node_id)``. The g term prefers the frontier entry that has
    # already covered more ground when two share an f, and the id settles the
    # rest -- so the order is total and the same on every run.
    frontier: list[tuple[float, float, str]] = [(h(start_id), 0.0, start_id)]
    expanded = 0

    while frontier:
        _, current_g, current = heapq.heappop(frontier)
        if current in settled:
            continue
        settled.add(current)
        expanded += 1
        if current == goal_id:
            return _reconstruct(graph, adjacency, came_from, g_score, start_id, goal_id, expanded)
        if expanded > MAX_EXPANSIONS:  # pragma: no cover - a fireground graph is tiny
            return PathRefusal(
                reason="the search exceeded its expansion ceiling before reaching the goal",
                refs=(start_id, goal_id),
                expanded=expanded,
            )
        for edge in adjacency.get(current, ()):
            if edge.to_id in settled:
                continue
            tentative = current_g + edge.cost
            # Strict improvement only. An equal-cost rediscovery keeps the
            # first predecessor, which is the one reached through the
            # lower-ordered frontier entry -- the tie-break, made stable.
            if tentative < g_score.get(edge.to_id, math.inf):
                g_score[edge.to_id] = tentative
                came_from[edge.to_id] = edge
                heapq.heappush(frontier, (tentative + h(edge.to_id), tentative, edge.to_id))

    return PathRefusal(
        reason=(
            "no leg of the navigable graph connects the start to the goal; the "
            "cost model refused every route that would have"
        ),
        refs=(start_id, goal_id, *(barrier.reason for barrier in graph.barriers[:4])),
        expanded=expanded,
    )


def _reconstruct(
    graph: NavGraph,
    adjacency: Mapping[str, Sequence[NavEdge]],
    came_from: Mapping[str, NavEdge],
    g_score: Mapping[str, float],
    start_id: str,
    goal_id: str,
    expanded: int,
) -> PathSolution:
    """Walk the predecessors back, then explain each leg forwards.

    The explanation is generated here rather than in the builder because only
    the search knows what it *rejected*: the alternatives out of a node, priced
    by the same arithmetic that priced the leg taken. A reason written at build
    time would be a description of the cost model, not of the decision.
    """
    chain: list[NavEdge] = []
    cursor = goal_id
    while cursor != start_id:
        edge = came_from[cursor]
        chain.append(edge)
        cursor = edge.from_id
    chain.reverse()

    barriers_by_node: dict[str, list[Barrier]] = {}
    for barrier in graph.barriers:
        barriers_by_node.setdefault(barrier.from_id, []).append(barrier)

    legs: list[PathLeg] = []
    for edge in chain:
        avoided: list[str] = []
        for alternative in adjacency.get(edge.from_id, ()):
            if alternative.to_id == edge.to_id:
                continue
            avoided.append(
                f"{alternative.to_id} was reachable at {alternative.cost:.1f} "
                f"({alternative.length_m:.1f} m x {alternative.multiplier:.2f})"
            )
        avoided.extend(
            f"{barrier.to_id} was not traversable: {barrier.reason}"
            for barrier in barriers_by_node.get(edge.from_id, ())
        )
        legs.append(
            PathLeg(
                from_id=edge.from_id,
                to_id=edge.to_id,
                distance_m=round(edge.length_m, 2),
                cost=round(edge.cost, 3),
                multiplier=round(edge.multiplier, 3),
                terms=edge.terms,
                avoided=tuple(avoided[:8]),
            )
        )

    return PathSolution(
        node_ids=(start_id, *(edge.to_id for edge in chain)),
        legs=tuple(legs),
        total_cost=round(g_score[goal_id], 3),
        total_distance_m=round(sum(edge.length_m for edge in chain), 2),
        expanded=expanded,
    )


__all__ = [
    "MAX_EXPANSIONS",
    "Barrier",
    "CostTerm",
    "NavEdge",
    "NavGraph",
    "NavNode",
    "PathLeg",
    "PathOutcome",
    "PathRefusal",
    "PathSolution",
    "astar",
    "euclidean_heuristic",
]
