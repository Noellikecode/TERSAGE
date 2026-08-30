"""The cost model: what the arithmetic does with what the fleet measured.

The search itself is tested next door against graphs whose optimum is obvious.
These tests are about pricing -- that a hot wall forces a detour, that a wall
nobody flew is dearer than a wall measured warm, that a barrier removes a leg
rather than making it expensive, and that a building the data cannot support a
route through produces a refusal rather than a route.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    Obstruction,
    ObstructionType,
    RoofSegment,
    collapse_zone_radius,
)
from firstdue.domain.keys import Keys
from firstdue.domain.values import BooleanValue
from firstdue.incident.entrypath import (
    STAIR_LANDING_M,
    STAIR_PITCH_DEG,
    THERMAL_BARRIER_C,
    GeoOrigin,
    NavEdge,
    NavGraph,
    _centroid,
    _contains,
    _core_point,
    build_graph,
    compute_entry_path,
)
from firstdue.incident.fusion import THERMAL_CAVEAT, FaceCoverage, VoidObservation

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)
GROUND = (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)


def spec(
    *,
    levels: int = 2,
    storey_m: float = 3.5,
    disputed_from: int | None = None,
    obstruction_azimuth: float | None = None,
    footprint: tuple[tuple[float, float], ...] | None = None,
) -> GeometrySpec:
    """A twenty-metre square, counter-clockwise, with real storeys on it.

    The footprint is overridable because a square has no long axis, so nothing
    about stair orientation can be asserted on the default shape.
    """
    height = levels * storey_m
    segments = (
        (RoofSegment(pitch_deg=15.0, azimuth_deg=obstruction_azimuth),)
        if obstruction_azimuth is not None
        else ()
    )
    return GeometrySpec(
        address_id="sf-test-square",
        generated_at=NOW,
        footprint=footprint or ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)),
        levels=tuple(
            Level(
                height_m=storey_m,
                provenance=SourceType.PERMIT,
                status=(
                    AssertionStatus.DISPUTED
                    if disputed_from is not None and index >= disputed_from
                    else AssertionStatus.CONFIRMED
                ),
                fact_id=f"fact-level-{index}",
            )
            for index in range(levels)
        ),
        roof_segments=segments,
        obstructions=(
            (
                Obstruction(
                    type=ObstructionType.SOLAR_ARRAY,
                    segment_index=0,
                    provenance=SourceType.SOLAR_API,
                ),
            )
            if obstruction_azimuth is not None
            else ()
        ),
        faces=tuple(Face(label=label) for label in GROUND),
        collapse_zone_radius_m=collapse_zone_radius(height),
    )


def measured(face: FaceLabel, peak: float, *, coverage: float = 1.0) -> FaceCoverage:
    return FaceCoverage(
        face=face,
        scanned=True,
        observed_at=NOW,
        peak_c=peak,
        coverage=coverage,
        render=f"{peak:.0f} C peak surface temperature. {THERMAL_CAVEAT}",
    )


def unscanned(face: FaceLabel) -> FaceCoverage:
    return FaceCoverage(face=face, scanned=False, render=f"UNSCANNED - no coverage. {face}")


def coverage_of(**peaks: float) -> tuple[FaceCoverage, ...]:
    """Coverage for all four faces; a face named with ``None`` is UNSCANNED."""
    return tuple(
        measured(label, peaks[str(label)])
        if peaks.get(str(label)) is not None
        else unscanned(label)
        for label in GROUND
    )


def plan(coverage: tuple[FaceCoverage, ...], **kwargs: object) -> object:
    return compute_entry_path(
        incident_id="inc-1",
        spec=kwargs.pop("spec", None) or spec(),  # type: ignore[arg-type]
        coverage=coverage,
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------ the ordinary case


@pytest.mark.invariant
def test_a_cool_building_routes_through_the_face_the_apparatus_is_parked_on() -> None:
    """Nothing costs anything, so the answer is the shortest walk.

    Staging sits off Alpha -- the address side by the convention the geometry
    module states -- so with every wall priced identically the crew goes in
    through the wall they are standing at. It is the baseline the detour tests
    below are measured against.
    """
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(ALPHA=30.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0),
    )
    assert not result.refused
    assert result.entry is not None
    assert result.entry_face == "ALPHA"
    assert result.entry.waypoints[0].node_id == "staging"
    assert result.entry.waypoints[-1].node_id == "core:L0"
    assert [w.kind for w in result.entry.waypoints] == [
        "staging",
        "approach",
        "door",
        "interior",
        "core",
    ]


@pytest.mark.invariant
def test_every_edge_costs_at_least_its_own_length() -> None:
    """The precondition the straight-line heuristic depends on.

    If any weight could go negative the heuristic would stop being admissible
    and A* could return a route that is not the cheapest -- silently. Asserted
    over a graph built from real coverage rather than argued about.
    """
    graph = build_graph(
        spec(disputed_from=1),
        coverage=coverage_of(ALPHA=None, BRAVO=120.0, CHARLIE=200.0, DELTA=60.0),
        voids=(
            VoidObservation(
                face=FaceLabel.CHARLIE, region_index=1, delta_c=40.0, peak_c=200.0, observed_at=NOW
            ),
        ),
    )
    assert graph.edges
    for edge in graph.edges:
        assert edge.multiplier >= 1.0
        assert edge.cost >= edge.length_m


# ------------------------------------------------------------------ the detour


@pytest.mark.invariant
def test_a_hot_wall_forces_the_route_round_to_a_cooler_one() -> None:
    """The measured peak on Alpha makes the nearest wall the wrong one."""
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(ALPHA=260.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0),
    )
    assert not result.refused
    assert result.entry is not None
    assert result.entry_face != "ALPHA"
    # Longer on the ground than the route through Alpha would have been, which
    # is what a detour is.
    direct = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(ALPHA=30.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0),
    )
    assert direct.entry is not None
    assert result.entry.total_distance_m > direct.entry.total_distance_m


def _entry_multiplier(coverage: tuple[FaceCoverage, ...], face: str) -> float:
    graph = build_graph(spec(), coverage=coverage)
    edge = next(
        e for e in graph.edges if e.from_id == f"approach:{face}" and e.to_id == f"door:{face}"
    )
    return edge.multiplier


@pytest.mark.invariant
def test_an_unflown_wall_is_dearer_than_a_warm_one_and_cheaper_than_a_hot_one() -> None:
    """The ordering this module states in its own docstring, asserted.

    "Nobody looked" sits between "measured warm" and "measured hot" on purpose:
    above warm because an unmeasured wall could be anything, below hot because a
    wall somebody measured at 250 C is a known problem rather than a possible
    one. Compared as multipliers on the same leg of the same building, so the
    only thing that differs between the three numbers is the coverage.
    """
    warm = coverage_of(ALPHA=100.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0)
    unknown = coverage_of(ALPHA=None, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0)
    hot = coverage_of(ALPHA=250.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0)
    assert (
        _entry_multiplier(warm, "ALPHA")
        < _entry_multiplier(unknown, "ALPHA")
        < _entry_multiplier(hot, "ALPHA")
    )
    # And the term is named on the leg, so a detour caused by it explains itself.
    graph = build_graph(spec(), coverage=unknown)
    alpha_entry = next(
        e for e in graph.edges if e.from_id == "approach:ALPHA" and e.to_id == "door:ALPHA"
    )
    term = next(t for t in alpha_entry.terms if t.term_id == "thermal.unscanned")
    assert "ALPHA" in term.refs
    assert "UNSCANNED is unknown, not safe" in term.detail


@pytest.mark.invariant
def test_an_unscanned_wall_can_send_the_route_round_to_a_measured_one() -> None:
    """The penalty is not decorative: it moves the answer.

    Alpha is the wall the apparatus is parked at and has no coverage at all;
    the other three were flown and came back cool. The crew goes round.
    """
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(ALPHA=None, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0),
    )
    assert not result.refused
    assert result.entry is not None
    assert result.entry_face != "ALPHA"
    assert "ALPHA" in result.unscanned_faces


def test_a_partly_flown_wall_is_priced_for_the_part_nobody_measured() -> None:
    graph = build_graph(
        spec(),
        coverage=(
            measured(FaceLabel.ALPHA, 50.0, coverage=0.4),
            *(measured(label, 50.0) for label in GROUND[1:]),
        ),
    )
    alpha = next(
        e for e in graph.edges if e.from_id == "approach:ALPHA" and e.to_id == "door:ALPHA"
    )
    bravo = next(
        e for e in graph.edges if e.from_id == "approach:BRAVO" and e.to_id == "door:BRAVO"
    )
    assert any(term.term_id == "thermal.partial-coverage" for term in alpha.terms)
    assert not any(term.term_id == "thermal.partial-coverage" for term in bravo.terms)
    assert alpha.multiplier > bravo.multiplier


def test_a_roof_obstruction_is_attributed_to_the_wall_it_sits_over() -> None:
    """Alpha's outward normal on this footprint is due north, so a segment
    facing north is the one above Alpha and no other wall pays for it."""
    graph = build_graph(
        spec(obstruction_azimuth=0.0), coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0))
    )
    alpha = next(
        e for e in graph.edges if e.from_id == "approach:ALPHA" and e.to_id == "door:ALPHA"
    )
    charlie = next(
        e for e in graph.edges if e.from_id == "approach:CHARLIE" and e.to_id == "door:CHARLIE"
    )
    assert any(term.term_id == "obstruction.on-face" for term in alpha.terms)
    assert not any(term.term_id == "obstruction.on-face" for term in charlie.terms)


GROUND_NAMES = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")


# ----------------------------------------------------------------- the barrier


@pytest.mark.invariant
def test_a_wall_at_the_barrier_temperature_produces_no_leg_at_all() -> None:
    """Not an expensive edge. No edge, and a stated reason it does not exist."""
    coverage = coverage_of(ALPHA=THERMAL_BARRIER_C + 40.0, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0)
    graph = build_graph(spec(), coverage=coverage)
    assert "door:ALPHA" not in graph.by_id
    assert not any(edge.to_id == "door:ALPHA" for edge in graph.edges)
    assert any("ALPHA" in barrier.refs for barrier in graph.barriers)

    result = compute_entry_path(incident_id="inc-1", spec=spec(), coverage=coverage)
    assert not result.refused
    assert result.entry_face != "ALPHA"
    assert result.barriers


@pytest.mark.invariant
def test_a_building_barred_on_every_face_refuses_rather_than_routing() -> None:
    """No route is the finding. A cheapest-of-four-impossible-walls is not."""
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, THERMAL_BARRIER_C + 10.0)),
    )
    assert result.refused
    assert result.entry is None
    assert "no leg" in result.refusal_reason
    assert len(result.barriers) == 4


# ---------------------------------------------------------------- refusals


@pytest.mark.invariant
def test_no_pre_incident_geometry_refuses_with_the_cold_start_reason() -> None:
    result = compute_entry_path(incident_id="inc-1", spec=None)
    assert result.refused
    assert result.entry is None
    assert "no pre-incident geometry" in result.refusal_reason


def test_a_massing_model_with_no_storeys_refuses() -> None:
    result = compute_entry_path(incident_id="inc-1", spec=spec(levels=0))
    assert result.refused
    assert "no storeys" in result.refusal_reason


def test_a_storey_the_model_does_not_have_refuses_rather_than_inventing_one() -> None:
    result = compute_entry_path(incident_id="inc-1", spec=spec(levels=2), target_level=7)
    assert result.refused
    assert "invents a floor" in result.refusal_reason


# ------------------------------------------------------ structure and storeys


def test_a_confirmed_lightweight_truss_prices_every_interior_leg(make_fact) -> None:
    truss = make_fact(key=Keys.LIGHTWEIGHT_TRUSS, value=BooleanValue(boolean=True))
    graph = build_graph(
        spec(),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
        facts={Keys.LIGHTWEIGHT_TRUSS: truss},
    )
    interior = [edge for edge in graph.edges if edge.from_id.startswith(("interior:", "core:"))]
    assert interior
    priced = [
        e for e in interior if any(t.term_id == "structure.lightweight-truss" for t in e.terms)
    ]
    assert priced
    # Cited by the fact it came from, never asserted bare.
    term = next(t for t in priced[0].terms if t.term_id == "structure.lightweight-truss")
    assert truss.fact_id in term.refs


def test_a_truss_attribute_that_was_checked_and_found_absent_costs_nothing(make_fact) -> None:
    """``False`` is a checked answer. Only ``True`` is a hazard."""
    graph = build_graph(
        spec(),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
        facts={
            Keys.LIGHTWEIGHT_TRUSS: make_fact(
                key=Keys.LIGHTWEIGHT_TRUSS, value=BooleanValue(boolean=False)
            )
        },
    )
    assert not any(
        term.term_id == "structure.lightweight-truss" for edge in graph.edges for term in edge.terms
    )


def _flights(graph: NavGraph, level: int) -> list[NavEdge]:
    """The two legs that climb from ``core:L{level}`` to the storey above it."""
    landing = f"stair:L{level}:mid"
    up = next(e for e in graph.edges if e.from_id == f"core:L{level}" and e.to_id == landing)
    over = next(e for e in graph.edges if e.from_id == landing and e.to_id == f"core:L{level + 1}")
    return [up, over]


def test_a_disputed_storey_is_priced_on_both_flights_that_climb_into_it() -> None:
    graph = build_graph(
        spec(levels=3, disputed_from=2), coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0))
    )
    # The climb into a storey is a switchback now, so "the leg that climbs into
    # it" is two legs -- and a disputed storey has to price both, or half the
    # ascent into a floor the records disagree exists comes out free.
    for flight in _flights(graph, 1):
        assert any(term.term_id == "structure.disputed-level" for term in flight.terms)
    for flight in _flights(graph, 0):
        assert not any(term.term_id == "structure.disputed-level" for term in flight.terms)


def test_the_climb_is_the_stair_a_crew_walks_not_the_rise_it_gains() -> None:
    """The correction itself: a storey costs its stair, not its height.

    A lift shaft covers a 3.5 m storey in 3.5 m. A stair at the steepest pitch
    the code permits covers it in `3.5 / sin(32.47 deg)` plus a landing, which is
    more than twice as far -- and the old model reported the shaft number to a
    crew deciding what air they needed to reach a floor.
    """
    graph = build_graph(spec(levels=3), coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)))
    rise = 3.5
    travelled = sum(flight.length_m for flight in _flights(graph, 0))

    half = rise / 2.0
    run = half / math.tan(math.radians(STAIR_PITCH_DEG)) + STAIR_LANDING_M / 2.0
    assert travelled == pytest.approx(2 * math.hypot(run, half))
    # The claim in human terms, which is the one that matters on a fireground.
    assert travelled > 2 * rise


def test_the_landing_stands_off_the_shaft_at_half_the_rise() -> None:
    """A flight is a diagonal, so the landing is neither in the shaft nor level.

    Both halves matter. A landing left at the core's own x/y would give a
    zero-run flight -- a plumb line again, wearing a longer number. A landing at
    the full storey height would put the turn at the floor above it.
    """
    graph = build_graph(spec(levels=3), coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)))
    by_id = {node.node_id: node for node in graph.nodes}
    core, landing, above = by_id["core:L0"], by_id["stair:L0:mid"], by_id["core:L1"]

    assert landing.kind == "stair"
    assert landing.z_m == pytest.approx((core.z_m + above.z_m) / 2.0)
    assert math.hypot(landing.x_m - core.x_m, landing.y_m - core.y_m) > 1.0
    # The shaft itself has not moved: the landing steps off it and back onto it.
    assert (above.x_m, above.y_m) == (core.x_m, core.y_m)


def test_the_stair_runs_along_the_longest_wall() -> None:
    """Which way the switchback steps, and why it is that way.

    Nobody surveyed this stair's orientation. The long axis is the only axis the
    measured outline gives any evidence for, so a landing that stepped across
    the short dimension would be an assumption with less behind it -- and on a
    narrow building it would step through a wall.
    """
    # Ten metres wide, thirty deep: the long axis is y, and a landing that
    # stepped along x would be 5 m from the shaft in a building with 5 m to give.
    narrow = ((0.0, 0.0), (10.0, 0.0), (10.0, 30.0), (0.0, 30.0))
    graph = build_graph(
        spec(levels=2, footprint=narrow),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
    )
    by_id = {node.node_id: node for node in graph.nodes}
    core, landing = by_id["core:L0"], by_id["stair:L0:mid"]
    across = abs(landing.x_m - core.x_m)
    along = abs(landing.y_m - core.y_m)
    assert along > across
    assert across == pytest.approx(0.0)
    # And it stays inside the ten-metre width it stepped away from.
    assert _contains(narrow, (landing.x_m, landing.y_m))


def test_a_building_too_small_for_the_stair_keeps_its_landing_indoors() -> None:
    """The same defect the core guard fixes, committed again one function later.

    A switchback at the assumed pitch wants about 3.3 m of run for a 3.5 m
    storey. A six-metre outbuilding does not have that from its own centre, and
    the unclamped landing walked out through the wall to be published as WGS-84
    at seven decimals -- a node in the garden, on a route into a building.
    """
    shed = ((0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0))
    graph = build_graph(
        spec(levels=3, footprint=shed),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
    )
    by_id = {node.node_id: node for node in graph.nodes}
    landing = by_id["stair:L0:mid"]
    assert _contains(shed, (landing.x_m, landing.y_m))


def test_capping_the_run_does_not_shorten_the_climb() -> None:
    """What the cap buys is a landing indoors, not an easier ascent.

    A building too narrow to hold a code switchback still has to be climbed, and
    the crew still walks a slope. Letting the clamped geometry set the distance
    would hand back the plumb-line understatement on exactly the small, awkward
    buildings where a crew has least room to be wrong about it.
    """
    shed = ((0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0))
    graph = build_graph(
        spec(levels=3, footprint=shed),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
    )
    rise = 3.5
    travelled = sum(flight.length_m for flight in _flights(graph, 0))
    # The flight the pitch implies, which is the floor the cap may not go under.
    assert travelled == pytest.approx(rise / math.sin(math.radians(STAIR_PITCH_DEG)))
    assert travelled > rise


def test_a_landing_claims_no_storey_because_it_is_between_two() -> None:
    """`level` is the storey a renderer draws a waypoint on, and a landing is on
    neither of the ones it joins. Staging and the approach ring say None for the
    same reason; a landing claiming the floor below would be drawn on that
    floor's plan, floating half a storey over it.
    """
    graph = build_graph(spec(levels=3), coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)))
    by_id = {node.node_id: node for node in graph.nodes}
    assert by_id["stair:L0:mid"].level is None
    # The storeys either side of it still say which floor they are.
    assert by_id["core:L0"].level == 0
    assert by_id["core:L1"].level == 1


def test_a_courtyard_footprint_puts_its_core_inside_the_building() -> None:
    """The bug the centroid had, on a shape the live parcel feed really returns.

    A U-shaped block's area-weighted centre is in the courtyard. The old code
    used it as the stairwell core regardless: a node in open air, an interior leg
    measured to a point outside the structure, and coordinates handed to a
    renderer to seven decimals.
    """
    courtyard = (
        (0.0, 0.0),
        (30.0, 0.0),
        (30.0, 20.0),
        (22.0, 20.0),
        (22.0, 6.0),
        (8.0, 6.0),
        (8.0, 20.0),
        (0.0, 20.0),
    )
    assert not _contains(courtyard, _centroid(courtyard))
    assert _contains(courtyard, _core_point(courtyard))


def test_a_convex_footprint_keeps_the_centroid_it_always_had() -> None:
    """The guard is a guard, not a new placement rule.

    Every parcel in the seed is convex, so this is what keeps the fix from
    quietly moving every existing route.
    """
    rectangle = ((0.0, 0.0), (11.5, 0.0), (11.5, 22.0), (0.0, 22.0))
    assert _core_point(rectangle) == _centroid(rectangle)


def test_an_open_conflict_on_a_load_bearing_attribute_prices_the_interior() -> None:
    conflict = Conflict(
        conflict_id="conflict-1",
        address_id="sf-test-square",
        canonical_key=Keys.STORIES,
        rule_id="rule.storey-mismatch",
        severity=4,
        fact_ids=("fact-a", "fact-b"),
        summary="the permit says two storeys and the lidar measured three",
        detected_at=NOW,
    )
    graph = build_graph(
        spec(),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
        conflicts=(conflict,),
    )
    interior = next(
        e for e in graph.edges if e.from_id == "interior:ALPHA:L0" and e.to_id == "core:L0"
    )
    term = next(t for t in interior.terms if t.term_id == "structure.open-conflict")
    assert "conflict-1" in term.refs
    assert Keys.STORIES in term.refs


# ------------------------------------------------------------------- egress


@pytest.mark.invariant
def test_the_second_way_out_leaves_by_a_different_face() -> None:
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0)),
    )
    assert result.egress is not None
    assert result.egress.waypoints[0].node_id == "core:L0"
    exit_face = result.egress.waypoints[-1].face
    assert exit_face
    assert exit_face != result.entry_face


def test_a_building_with_one_usable_face_says_there_is_no_second_way_out() -> None:
    """Stated, not papered over. Three walls barred leaves one route."""
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=(
            measured(FaceLabel.ALPHA, 30.0),
            *(measured(label, THERMAL_BARRIER_C + 10.0) for label in GROUND[1:]),
        ),
    )
    assert not result.refused
    assert result.entry_face == "ALPHA"
    assert result.egress is None
    assert "no second way out" in result.egress_note


# -------------------------------------------------------------- the drawing


def test_waypoints_carry_footprint_metres_and_coordinates_when_an_origin_is_known() -> None:
    coverage = coverage_of(**dict.fromkeys(GROUND_NAMES, 30.0))
    without = compute_entry_path(incident_id="inc-1", spec=spec(), coverage=coverage)
    assert without.entry is not None
    assert all(w.longitude is None and w.latitude is None for w in without.entry.waypoints)

    origin = GeoOrigin(latitude=37.7764, longitude=-122.4241)
    with_origin = compute_entry_path(
        incident_id="inc-1", spec=spec(), coverage=coverage, origin=origin
    )
    assert with_origin.entry is not None
    for waypoint in with_origin.entry.waypoints:
        assert waypoint.longitude is not None
        assert waypoint.latitude is not None
        # A parcel is tens of metres across; the coordinates stay on it.
        assert abs(waypoint.latitude - origin.latitude) < 0.002
        assert abs(waypoint.longitude - origin.longitude) < 0.002
    # The core of storey one sits at ground level; nothing floats.
    assert with_origin.entry.waypoints[-1].z_m == pytest.approx(0.0)


def test_the_same_inputs_produce_a_byte_identical_plan() -> None:
    """Determinism, over the whole document rather than over the node list."""
    kwargs = {
        "incident_id": "inc-1",
        "spec": spec(disputed_from=1),
        "coverage": coverage_of(ALPHA=None, BRAVO=120.0, CHARLIE=30.0, DELTA=90.0),
        "origin": GeoOrigin(latitude=37.7764, longitude=-122.4241),
    }
    first = compute_entry_path(**kwargs)  # type: ignore[arg-type]
    for _ in range(5):
        assert compute_entry_path(**kwargs).model_dump(mode="json") == first.model_dump(  # type: ignore[arg-type]
            mode="json"
        )


def test_every_leg_states_why_it_was_chosen_and_what_it_was_weighed_against() -> None:
    result = compute_entry_path(
        incident_id="inc-1",
        spec=spec(),
        coverage=coverage_of(ALPHA=None, BRAVO=30.0, CHARLIE=30.0, DELTA=30.0),
    )
    assert result.entry is not None
    for leg in result.entry.legs:
        assert leg.chose_because
        assert f"{leg.distance_m:.1f} m" in leg.chose_because
    # The leg leaving staging had three other approaches to choose between.
    first = result.entry.legs[0]
    assert len(first.avoided) >= 3
