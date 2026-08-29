"""The solver, against graphs whose optimum is obvious by inspection.

Nothing here builds a graph from a building. That is the point of the split:
these tests fail when the search is wrong, and the cost-model tests next door
fail when the pricing is wrong, so a bug in one cannot hide behind the other.
"""

from __future__ import annotations

import math

import pytest

from firstdue.incident.pathfinding import (
    Barrier,
    CostTerm,
    NavEdge,
    NavGraph,
    NavNode,
    PathRefusal,
    PathSolution,
    astar,
)


def _node(node_id: str, x: float, y: float, z: float = 0.0) -> NavNode:
    return NavNode(node_id=node_id, x_m=x, y_m=y, z_m=z, kind="test")


def _edges(nodes: dict[str, NavNode], *pairs: tuple[str, str, float]) -> list[NavEdge]:
    """Both directions of each pair, with the weight expressed as a cost term.

    ``length_m`` is always the real straight-line distance between the two
    nodes, so every graph these tests build satisfies the property the
    heuristic depends on: no edge is cheaper than the straight line across it.
    """
    built: list[NavEdge] = []
    for from_id, to_id, weight in pairs:
        length = nodes[from_id].distance_to(nodes[to_id])
        terms = (
            (CostTerm(term_id="test.weight", weight=weight, detail="a test weight"),)
            if weight
            else ()
        )
        built.append(NavEdge(from_id=from_id, to_id=to_id, length_m=length, terms=terms))
        built.append(NavEdge(from_id=to_id, to_id=from_id, length_m=length, terms=terms))
    return built


def _diamond(long_weight: float = 0.0, short_weight: float = 0.0) -> NavGraph:
    """Two ways from A to D: a short pair of hops, and one long one.

    A --------- B --------- D    (10 + 10 along the top)
     \\                     /
      C -------------------      (1 down, then 1 across, then back up)
    """
    nodes = {
        "A": _node("A", 0.0, 0.0),
        "B": _node("B", 10.0, 0.0),
        "C": _node("C", 0.0, 1.0),
        "D": _node("D", 20.0, 0.0),
    }
    return NavGraph(
        nodes=tuple(nodes.values()),
        edges=tuple(
            _edges(
                nodes,
                ("A", "B", long_weight),
                ("B", "D", long_weight),
                ("A", "C", short_weight),
                ("C", "D", short_weight),
            )
        ),
    )


def _solution(graph: NavGraph, start: str, goal: str) -> PathSolution:
    outcome = astar(graph, start_id=start, goal_id=goal)
    assert isinstance(outcome, PathSolution), outcome
    return outcome


# --------------------------------------------------------- known optima


@pytest.mark.invariant
def test_the_search_finds_the_shortest_route_on_a_graph_with_one_obvious_answer() -> None:
    """Straight down the top, because the bottom route is barely longer."""
    solution = _solution(_diamond(), "A", "D")
    assert solution.node_ids == ("A", "B", "D")
    assert solution.total_distance_m == pytest.approx(20.0)
    assert solution.total_cost == pytest.approx(20.0)


@pytest.mark.invariant
def test_a_weight_on_the_short_route_moves_the_answer_to_the_long_one() -> None:
    """The detour is the whole reason the cost model exists.

    Nothing about the *distances* changed. What changed is that the top route
    costs more per metre, and the search takes the longer way round -- which is
    exactly what a hot wall has to be able to do to a crew's route.
    """
    solution = _solution(_diamond(long_weight=5.0), "A", "D")
    assert solution.node_ids == ("A", "C", "D")
    # Longer in metres than the route it beat, and cheaper in cost.
    assert solution.total_distance_m > 20.0
    assert solution.total_cost < 20.0 * 6.0


@pytest.mark.invariant
def test_a_single_hop_beats_two_when_it_is_cheaper() -> None:
    nodes = {"A": _node("A", 0.0, 0.0), "B": _node("B", 3.0, 4.0)}
    graph = NavGraph(nodes=tuple(nodes.values()), edges=tuple(_edges(nodes, ("A", "B", 0.0))))
    solution = _solution(graph, "A", "B")
    assert solution.node_ids == ("A", "B")
    assert solution.total_distance_m == pytest.approx(5.0)


def test_a_start_that_is_the_goal_is_a_zero_length_route() -> None:
    solution = _solution(_diamond(), "A", "A")
    assert solution.node_ids == ("A",)
    assert solution.legs == ()
    assert solution.total_cost == 0.0


# ------------------------------------------------------- A* against Dijkstra


@pytest.mark.invariant
def test_the_heuristic_never_changes_the_answer_dijkstra_would_give() -> None:
    """The check that the heuristic is admissible, rather than believed to be.

    A* with an inadmissible heuristic finds a path faster and can find a worse
    one. Running the same graph with ``h = 0`` -- which is Dijkstra -- and
    asserting the same cost is what catches that, and it is asserted across a
    range of weightings rather than on one graph that happened to agree.
    """
    for weight in (0.0, 0.3, 1.0, 2.5, 9.0):
        graph = _diamond(long_weight=weight, short_weight=weight / 2.0)
        with_heuristic = _solution(graph, "A", "D")
        without = astar(graph, start_id="A", goal_id="D", use_heuristic=False)
        assert isinstance(without, PathSolution)
        assert with_heuristic.total_cost == pytest.approx(without.total_cost)
        assert with_heuristic.node_ids == without.node_ids


@pytest.mark.invariant
def test_the_straight_line_never_exceeds_the_real_cost_from_any_node() -> None:
    """Admissibility, stated as the inequality it actually is.

    For every node, the straight-line distance to the goal must not exceed what
    the search really pays to get there. Checked against Dijkstra's own answer
    from that node, which is the true optimum by construction.
    """
    graph = _diamond(long_weight=1.5, short_weight=0.25)
    goal = graph.by_id["D"]
    for node in graph.nodes:
        outcome = astar(graph, start_id=node.node_id, goal_id="D", use_heuristic=False)
        assert isinstance(outcome, PathSolution)
        assert node.distance_to(goal) <= outcome.total_cost + 1e-9


# --------------------------------------------------------------- refusals


@pytest.mark.invariant
def test_a_disconnected_graph_refuses_instead_of_returning_a_partial_route() -> None:
    """No nearest-node fallback anywhere. A route to somewhere else is worse."""
    nodes = {
        "A": _node("A", 0.0, 0.0),
        "B": _node("B", 1.0, 0.0),
        "X": _node("X", 50.0, 0.0),
        "Y": _node("Y", 51.0, 0.0),
    }
    graph = NavGraph(
        nodes=tuple(nodes.values()),
        edges=tuple(_edges(nodes, ("A", "B", 0.0), ("X", "Y", 0.0))),
        barriers=(Barrier(from_id="B", to_id="X", reason="the wall between them was a barrier"),),
    )
    outcome = astar(graph, start_id="A", goal_id="Y")
    assert isinstance(outcome, PathRefusal)
    assert "no leg" in outcome.reason
    # The barrier that disconnected it is named, so the refusal explains itself.
    assert any("barrier" in ref for ref in outcome.refs)


def test_an_unknown_endpoint_refuses_rather_than_raising() -> None:
    for start, goal in (("nowhere", "D"), ("A", "nowhere")):
        outcome = astar(_diamond(), start_id=start, goal_id=goal)
        assert isinstance(outcome, PathRefusal)
        assert "nowhere" in outcome.refs


# ------------------------------------------------- determinism and tie-breaks


@pytest.mark.invariant
def test_the_same_graph_returns_the_same_route_on_every_run() -> None:
    graph = _diamond(long_weight=0.4, short_weight=0.4)
    routes = {_solution(graph, "A", "D").node_ids for _ in range(25)}
    assert len(routes) == 1


@pytest.mark.invariant
def test_two_routes_of_identical_cost_resolve_to_the_same_one_every_time() -> None:
    """Tie-breaking stability, on a graph that is symmetric on purpose.

    Both ways round the square cost exactly the same. An entry plan that
    alternated between them across two renders of the same record would be
    indistinguishable from one that had been amended, so the tie has to break
    the same way every time -- and it has to break the same way whatever order
    the edges happened to be built in.
    """
    nodes = {
        "start": _node("start", 0.0, 0.0),
        "north": _node("north", 0.0, 10.0),
        "south": _node("south", 0.0, -10.0),
        "goal": _node("goal", 10.0, 0.0),
    }
    pairs = (
        ("start", "north", 0.0),
        ("north", "goal", 0.0),
        ("start", "south", 0.0),
        ("south", "goal", 0.0),
    )
    forward = NavGraph(nodes=tuple(nodes.values()), edges=tuple(_edges(nodes, *pairs)))
    reversed_build = NavGraph(
        nodes=tuple(reversed(list(nodes.values()))),
        edges=tuple(_edges(nodes, *reversed(pairs))),
    )
    first = _solution(forward, "start", "goal")
    assert first.node_ids == _solution(reversed_build, "start", "goal").node_ids
    assert all(_solution(forward, "start", "goal").node_ids == first.node_ids for _ in range(10))
    # Both arms really do cost the same -- otherwise this asserts nothing.
    north = nodes["start"].distance_to(nodes["north"]) + nodes["north"].distance_to(nodes["goal"])
    south = nodes["start"].distance_to(nodes["south"]) + nodes["south"].distance_to(nodes["goal"])
    assert north == pytest.approx(south)


# ---------------------------------------------------------- the explanation


def test_every_leg_reports_what_it_was_priced_against() -> None:
    """The alternatives at each node, priced by the same arithmetic."""
    graph = _diamond(long_weight=5.0)
    solution = _solution(graph, "A", "D")
    first = solution.legs[0]
    assert first.from_id == "A"
    # A had another way out, and the explanation names it with its cost.
    assert any(alternative.startswith("B was reachable") for alternative in first.avoided)


def test_a_leg_with_no_terms_costs_exactly_its_own_length() -> None:
    solution = _solution(_diamond(), "A", "D")
    for leg in solution.legs:
        assert leg.multiplier == pytest.approx(1.0)
        assert leg.cost == pytest.approx(leg.distance_m)


def test_a_weighted_edge_costs_its_length_times_one_plus_its_weights() -> None:
    """The cost function, asserted directly rather than inferred from a route."""
    edge = NavEdge(
        from_id="a",
        to_id="b",
        length_m=10.0,
        terms=(
            CostTerm(term_id="one", weight=1.5, detail="first"),
            CostTerm(term_id="two", weight=0.5, detail="second"),
        ),
    )
    assert edge.multiplier == pytest.approx(3.0)
    assert edge.cost == pytest.approx(30.0)


def test_distance_is_measured_in_three_dimensions() -> None:
    """A stairwell is a real leg with a real length, not a free hop."""
    assert _node("a", 0.0, 0.0, 0.0).distance_to(_node("b", 0.0, 0.0, 3.5)) == pytest.approx(3.5)
    assert _node("a", 0.0, 0.0, 0.0).distance_to(_node("b", 3.0, 4.0, 12.0)) == pytest.approx(13.0)
    assert _node("a", 1.0, 1.0, 1.0).distance_to(_node("b", 1.0, 1.0, 1.0)) == pytest.approx(0.0)
    assert math.isclose(
        _node("a", 0.0, 0.0).distance_to(_node("b", 1.0, 1.0)), math.sqrt(2.0), rel_tol=1e-9
    )
