"""The entry and egress route, priced from what the fleet actually measured.

The search itself is in :mod:`firstdue.incident.pathfinding` and knows nothing
about buildings. This module is the other half: it turns one address's own
:class:`~firstdue.domain.geometry.GeometrySpec` into a navigable graph, prices
every leg from data the incident already holds, and hands the result to A\\*.

**Everything on a leg is a measurement or a refusal.** There is no default
temperature, no assumed door, and no invented corridor. The nodes come from the
footprint the Geometry Watcher measured; the storeys come from the levels it
derived; the heat comes from :meth:`SensorFusion.coverage`, which reports a
wall nobody flew as UNSCANNED and never as cool. Where a number does not exist,
the arithmetic pays a stated penalty for not knowing it -- which is the only
honest way to include an unmeasured wall in a route at all.

**Unknown is expensive, not free.** :data:`W_UNKNOWN_FACE` is set above the
thermal term's value at half the barrier temperature, so a wall nobody looked at
ranks as worse than a wall measured merely warm and better than one measured
hot. That ordering is a judgement and it is stated here so an officer can
disagree with it, the same way :data:`~firstdue.incident.fusion.VOID_DELTA_C` is.

**The barrier is a refusal, not a large number.** A face measured at or above
:data:`THERMAL_BARRIER_C` produces no edge at all, and the leg that would have
existed is recorded as a :class:`~firstdue.incident.pathfinding.Barrier` so the
detour it caused can be explained. A very large weight would let the search take
the leg anyway when everything else was worse, which is exactly the situation in
which nobody should be told to walk through it.

**Insufficient data refuses.** No pre-incident geometry, no levels, no face that
resolves to a wall of the measured footprint, or a goal the graph cannot reach:
each returns a plan with ``refused`` set and the reason stated. There is no
fallback route and no straight line.

Nothing in this module recommends a tactic. It reports the cheapest traverse of
a graph whose costs are printed beside it, and every leg says what it was
weighed against.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field

from firstdue.domain.conflicts import Conflict, ConflictStatus
from firstdue.domain.enums import AssertionStatus, FaceLabel
from firstdue.domain.facts import StructuralFact
from firstdue.domain.geometry import (
    FACE_BEARING_TOLERANCE_DEG,
    GROUND_FACES,
    GeometrySpec,
    Point2D,
    angular_distance_deg,
    edge_normal_bearing,
    face_geometries,
)
from firstdue.domain.keys import CanonicalKey, Keys
from firstdue.incident.documents import RecordedDocument
from firstdue.incident.fusion import VOID_FLOOR_C, FaceCoverage, VoidObservation
from firstdue.incident.pathfinding import (
    Barrier,
    CostTerm,
    NavEdge,
    NavGraph,
    NavNode,
    PathLeg,
    PathRefusal,
    PathSolution,
    astar,
)

# ------------------------------------------------------------------ thresholds

#: Below this a measured surface reading buys no penalty at all. Deliberately
#: the same number :mod:`~firstdue.incident.fusion` uses as its void floor: a
#: wall cooler than this is not distinguishable from a warm afternoon, and two
#: modules disagreeing about where "warm" starts would be worse than either
#: threshold being wrong.
THERMAL_BASELINE_C: Final[float] = VOID_FLOOR_C

#: At or above this, the leg is not built. A hard barrier, stated so an officer
#: can disagree with it.
#:
#: The number is an *exterior surface* temperature, which is the only thing a
#: thermal frame measures. Structural firefighting protective clothing is
#: certified against a thermal exposure well below sustained contact with a
#: surface at this temperature, so a route through it is not a route a crew
#: could take -- and pricing it high instead of removing it would let the search
#: choose it on a building where everything else was worse.
THERMAL_BARRIER_C: Final[float] = 300.0

#: How far off a wall the approach ring sits when the collapse zone is smaller
#: than this. A crew cannot stand on the wall, and a ring of zero radius would
#: make every perimeter move free.
MIN_STANDOFF_M: Final[float] = 6.0

#: How far beyond the approach ring apparatus stages, on the address side.
APPARATUS_SETBACK_M: Final[float] = 8.0

#: How far inside the wall the first interior node sits. Clamped so it never
#: passes the centroid on a narrow building.
INTERIOR_SETBACK_M: Final[float] = 2.0

#: The pitch of the stair the storey count implies, in degrees.
#:
#: A stairwell is not a lift shaft, and modelling it as one understates how far
#: a crew walks to reach a floor by more than half. Nobody surveyed this stair,
#: so its pitch is taken from the steepest one the code would pass: IBC 1011
#: caps a riser at 7 inches and floors a tread at 11 inches, which is
#: ``atan(7/11)`` -- 32.5 degrees. Steepest, deliberately: a shallower
#: assumption would make the climb longer and the arithmetic would be claiming
#: a margin it has not measured.
STAIR_PITCH_DEG: Final[float] = 32.47

#: The depth of the landing at each half-storey turn.
#:
#: IBC 1011.6 requires a landing at least as deep as the stair is wide, and
#: 1011.2 floors an egress stair's width at 44 inches. A switchback therefore
#: costs this much horizontal travel per storey on top of the flights.
STAIR_LANDING_M: Final[float] = 1.12

#: How far short of a wall a landing is allowed to stand.
#:
#: The landing is placed by assumption, not measurement, so it is kept off the
#: only thing the outline actually measured. Half a metre is roughly the depth
#: of a person in gear turning on it.
STAIR_WALL_CLEARANCE_M: Final[float] = 0.5

# --------------------------------------------------------------------- weights
#
# Every weight is a multiplier contribution: the leg costs its own length times
# one plus the weights that apply to it. Multipliers rather than flat penalties,
# because a flat penalty large enough to matter on a 40 m ring move would
# dominate a 3 m hop through a doorway, and the arithmetic would stop being
# about distance at all.
#
# The numbers are judgements about relative danger, not measurements. They are
# constants in one place so an officer can read the whole cost model on one
# screen and argue with it.

#: Full weight at the barrier temperature, scaled linearly from the baseline.
W_THERMAL_MAX: Final[float] = 6.0

#: A face that is UNSCANNED, or whose sensor is UNAVAILABLE. Above the thermal
#: term's value at the midpoint between baseline and barrier, so "nobody looked"
#: costs more than "measured warm" and less than "measured hot".
W_UNKNOWN_FACE: Final[float] = 3.0

#: Scaled by the fraction of the face a current frame did *not* cover. A wall
#: flown at 40% is 60% unmeasured, and the arithmetic says so.
W_COVERAGE_GAP: Final[float] = 2.0

#: Per void observation on the face, capped. A measured temperature difference
#: between adjacent regions is an observation about the surface, not a finding
#: about what is behind it -- it is priced as uncertainty, not as fire.
W_VOID: Final[float] = 1.0
MAX_VOIDS_COUNTED: Final[int] = 3

#: Per obstruction attributed to the face. Solar arrays, EV chargers and HVAC
#: plant are things a crew cannot cut through and, in two cases, cannot
#: de-energise from the panel.
W_OBSTRUCTION: Final[float] = 1.5
MAX_OBSTRUCTIONS_COUNTED: Final[int] = 2

#: Scaled by the fraction of the leg that lies inside the collapse zone -- the
#: 1.5x-height standard :func:`~firstdue.domain.geometry.collapse_zone_radius`
#: already applies to the measured height.
W_COLLAPSE_ZONE: Final[float] = 2.0

#: Entering a storey whose mass is DISPUTED: the records disagree that it is
#: there at all, so its floor, its stairs and its ceiling height are all
#: unconfirmed.
W_DISPUTED_LEVEL: Final[float] = 2.5

#: Every interior and vertical leg, when the profile carries a confirmed
#: lightweight truss. Cited by the fact id it came from.
W_LIGHTWEIGHT_TRUSS: Final[float] = 1.5

#: Per open conflict on a load-bearing attribute, capped. A disagreement about
#: how many storeys there are is a disagreement about where the crew ends up.
W_OPEN_CONFLICT: Final[float] = 1.0
MAX_CONFLICTS_COUNTED: Final[int] = 3

#: Attributes whose disagreement changes what an entry looks like. Narrower than
#: "every key": a conflict about the year built is real and does not move a
#: crew, and pricing it would make the number mean nothing.
LOAD_BEARING_KEYS: Final[frozenset[CanonicalKey]] = frozenset(
    {
        Keys.STORIES,
        Keys.HEIGHT_M,
        Keys.CONSTRUCTION_TYPE,
        Keys.FLOOR_SYSTEM,
        Keys.LIGHTWEIGHT_TRUSS,
        Keys.STAIRWELL_COUNT,
        Keys.EGRESS_OBSTRUCTION,
    }
)

#: Metres per degree of latitude. The WGS-84 meridian is not a circle, so this
#: is the mean; over a parcel the difference is millimetres.
METRES_PER_DEGREE_LATITUDE: Final[float] = 111_320.0


class GeoOrigin(BaseModel):
    """Where the footprint's local origin sits on the earth.

    The footprint is metres relative to the parcel centroid, which is fine for
    arithmetic and useless to a map. This converts one to the other with a local
    equirectangular approximation -- latitude scaled by a constant, longitude by
    the cosine of the origin latitude. Over a parcel of tens of metres the error
    is under a centimetre; it would not be over a district, and nothing here
    uses it over one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)

    def to_wgs84(self, x_m: float, y_m: float) -> tuple[float, float]:
        """Local ENU metres to ``(longitude, latitude)`` degrees."""
        latitude = self.latitude + y_m / METRES_PER_DEGREE_LATITUDE
        scale = METRES_PER_DEGREE_LATITUDE * max(1e-6, math.cos(math.radians(self.latitude)))
        return self.longitude + x_m / scale, latitude


class Waypoint(BaseModel):
    """One point on the route, in every frame a renderer might want it in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=40)
    face: str = Field(default="", max_length=40)
    #: Zero-based storey, or ``None`` outside the building.
    level: int | None = Field(default=None, ge=0)
    #: Footprint-local metres, the frame the geometry spec is already in.
    x_m: float
    y_m: float
    z_m: float
    #: WGS-84, present only when the caller supplied the parcel's coordinates.
    longitude: float | None = None
    latitude: float | None = None


class RouteLeg(BaseModel):
    """One leg, as an officer reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_id: str = Field(min_length=1, max_length=120)
    to_id: str = Field(min_length=1, max_length=120)
    distance_m: float = Field(ge=0.0)
    cost: float = Field(ge=0.0)
    multiplier: float = Field(ge=1.0)
    #: Why this leg costs what it does. Empty means: distance and nothing else,
    #: which is the cheapest a leg can be and is itself the reason it was taken.
    terms: tuple[CostTerm, ...] = ()
    #: What the search priced this against and rejected.
    avoided: tuple[str, ...] = ()
    #: One sentence joining the two. Composed from the terms, never authored.
    chose_because: str = Field(min_length=1, max_length=500)


class Route(BaseModel):
    """A traversal of the graph, drawable and explainable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    waypoints: tuple[Waypoint, ...] = Field(min_length=1)
    legs: tuple[RouteLeg, ...] = ()
    total_cost: float = Field(ge=0.0)
    total_distance_m: float = Field(ge=0.0)
    expanded_nodes: int = Field(ge=0)


class EntryPathPlan(RecordedDocument):
    """The whole answer: a route in, a route out, or a stated refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    #: ``A*`` always; named on the artifact so a reader knows what produced it.
    algorithm: str = "A*"
    heuristic: str = "euclidean-3d"
    #: Zero-based storey the route was computed to.
    target_level: int = Field(default=0, ge=0)

    refused: bool = False
    refusal_reason: str = Field(default="", max_length=400)
    refusal_refs: tuple[str, ...] = ()

    entry: Route | None = None
    #: A second way out, on a different face. ``None`` when the graph offers
    #: none, which is itself a finding and is stated in ``egress_note``.
    egress: Route | None = None
    egress_note: str = Field(default="", max_length=400)

    #: Legs the cost model refused to build, and why.
    barriers: tuple[Barrier, ...] = ()
    #: Faces that had no current thermal coverage when this was computed.
    unscanned_faces: tuple[str, ...] = ()
    #: The graph it was solved over. Kept so the arithmetic can be re-run.
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)

    # On the wire, because a renderer drawing the route on a massing model
    # needs to know which wall it goes through and should not have to
    # rediscover that by scanning the waypoints for a node kind.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def entry_face(self) -> str:
        """The face the route makes entry through, or an empty string."""
        if self.entry is None:
            return ""
        for waypoint in self.entry.waypoints:
            if waypoint.kind == "door":
                return waypoint.face
        return ""


# ------------------------------------------------------------ graph geometry


def _centroid(footprint: Sequence[Point2D]) -> Point2D:
    """Area-weighted polygon centroid, falling back to the vertex mean.

    The shoelace centre, and nothing more. It sits inside a convex parcel and
    can sit well outside a concave one, so it is not on its own a place to put a
    node -- :func:`_core_point` is what the graph asks for a stairwell, and it
    exists because this function's answer is a centre of area rather than a
    point in a building.

    On a degenerate ring -- collinear points, zero area -- the shoelace divides
    by zero, so the vertex mean stands in rather than the function raising on a
    shape the Geometry Watcher may legitimately have filed.
    """
    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    count = len(footprint)
    for index in range(count):
        x0, y0 = footprint[index]
        x1, y1 = footprint[(index + 1) % count]
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(twice_area) < 1e-9:
        return (
            sum(p[0] for p in footprint) / count,
            sum(p[1] for p in footprint) / count,
        )
    return cx / (3.0 * twice_area), cy / (3.0 * twice_area)


def _contains(footprint: Sequence[Point2D], point: Point2D) -> bool:
    """Is this point inside the ring? Even-odd ray casting, half-open in y.

    The half-open comparison ``(y0 > y) != (y1 > y)`` is what keeps a ray that
    grazes a vertex from counting that vertex twice, which is the only way this
    test goes wrong on an axis-aligned parcel -- and parcels are mostly
    axis-aligned.
    """
    x, y = point
    inside = False
    count = len(footprint)
    for index in range(count):
        x0, y0 = footprint[index]
        x1, y1 = footprint[(index + 1) % count]
        if (y0 > y) != (y1 > y):
            crossing = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if crossing > x:
                inside = not inside
    return inside


def _core_point(footprint: Sequence[Point2D]) -> Point2D:
    """Where the stairwell core stands: the centroid, pushed inside if it is not.

    The shoelace centroid sits inside a convex parcel, and the docstring above
    used to be allowed to stop there. It is not true of a concave one: an L, a
    U, a courtyard block -- all shapes the live parcel feed actually returns --
    put their area-weighted centre in open air, and the core node inherited it.
    That is a node in the courtyard, an interior leg measured to a point that is
    not in the building, and a set of WGS-84 coordinates handed to a renderer at
    seven decimal places, all under a docstring promising every node is
    "somewhere a person could actually be".

    So the centroid is tested against the ring it came from, and where it falls
    outside, it is moved the shortest way that keeps it honest: along its own
    scanline to the middle of the widest span of building that line crosses. The
    result is still inferred -- nobody surveyed this stair either -- but it is at
    least inside the structure, which is the weaker claim the rest of this module
    is entitled to make.
    """
    centre = _centroid(footprint)
    if _contains(footprint, centre):
        return centre

    # Every span of building the centroid's own scanline crosses, in x.
    y = centre[1]
    crossings: list[float] = []
    count = len(footprint)
    for index in range(count):
        x0, y0 = footprint[index]
        x1, y1 = footprint[(index + 1) % count]
        if (y0 > y) != (y1 > y):
            crossings.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    crossings.sort()

    widest: tuple[float, float] | None = None
    for index in range(0, len(crossings) - 1, 2):
        left, right = crossings[index], crossings[index + 1]
        if widest is None or (right - left) > (widest[1] - widest[0]):
            widest = (left, right)
    if widest is None:  # pragma: no cover - a ring with no interior span
        return centre
    return ((widest[0] + widest[1]) / 2.0, y)


class _Wall(BaseModel):
    """One labelled face, bound to the footprint edge it is actually on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: FaceLabel
    midpoint: Point2D
    #: Unit outward normal, in the footprint's own frame.
    normal: Point2D
    bearing_deg: float
    length_m: float


def _walls(spec: GeometrySpec) -> tuple[_Wall, ...]:
    """Bind each fireground face to a real edge of the measured footprint.

    :func:`~firstdue.domain.geometry.face_geometries` gives four labels and four
    compass bearings; it does not say which side of the polygon each one is,
    because nothing downstream of it needed to know. A route does: a door has to
    be somewhere, and "somewhere" has to be a place on the outline the slow loop
    measured rather than a point this module invented.

    So each face is matched to the footprint edge whose outward normal is
    nearest its bearing, inside the same tolerance
    :func:`~firstdue.domain.geometry.resolve_face` uses to attribute a camera --
    one threshold for "this bearing is that wall", not two. Where several edges
    qualify the longest wins, because that is the span of wall a crew can
    actually work; ties break on edge order so the graph is identical on every
    run. A face that matches nothing is left out, and the plan says which.
    """
    faces = face_geometries(spec.footprint)
    footprint = spec.footprint
    count = len(footprint)

    walls: list[_Wall] = []
    for face in faces:
        best: tuple[float, int] | None = None
        for index in range(count):
            start = footprint[index]
            end = footprint[(index + 1) % count]
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            if length <= 0.0:
                continue
            offset = angular_distance_deg(edge_normal_bearing(start, end), face.bearing_deg)
            if offset > FACE_BEARING_TOLERANCE_DEG:
                continue
            candidate = (-length, index)
            if best is None or candidate < best:
                best = candidate
        if best is None:
            continue
        index = best[1]
        start = footprint[index]
        end = footprint[(index + 1) % count]
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        # The outward normal as a unit vector in the footprint frame, from the
        # same rotation `edge_normal_bearing` describes: a counter-clockwise
        # ring's outward normal is the edge vector turned -90 degrees.
        normal = ((end[1] - start[1]) / length, -(end[0] - start[0]) / length)
        walls.append(
            _Wall(
                label=face.label,
                midpoint=((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0),
                normal=normal,
                bearing_deg=face.bearing_deg,
                length_m=round(length, 3),
            )
        )
    return tuple(walls)


def _stair_axis(walls: Sequence[_Wall]) -> Point2D:
    """The direction a switchback runs, as a unit vector.

    Along the longest measured wall, because that is the axis a building of this
    outline has room to run a stair down and the only axis the footprint gives
    any evidence for. It is an assumption about orientation, not a measurement,
    and it moves no node out of the shaft -- it only says which way the landings
    step, so a flight reads as a flight instead of a plumb line.

    Ties break on the order :func:`_walls` built them in, which is face order, so
    the graph is identical on every run.
    """
    if not walls:  # pragma: no cover - build_graph returns early without walls
        return (1.0, 0.0)
    longest = max(walls, key=lambda wall: wall.length_m)
    # The wall's own direction is its outward normal turned a quarter turn.
    return (-longest.normal[1], longest.normal[0])


def _exit_distance(footprint: Sequence[Point2D], origin: Point2D, axis: Point2D) -> float:
    """How far a ray from ``origin`` along ``axis`` travels before it leaves the ring."""
    best = math.inf
    count = len(footprint)
    for index in range(count):
        x0, y0 = footprint[index]
        x1, y1 = footprint[(index + 1) % count]
        ex, ey = x1 - x0, y1 - y0
        denominator = axis[0] * ey - axis[1] * ex
        if abs(denominator) < 1e-12:
            continue
        dx, dy = x0 - origin[0], y0 - origin[1]
        along = (dx * ey - dy * ex) / denominator
        across = (dx * axis[1] - dy * axis[0]) / denominator
        if along > 1e-9 and -1e-9 <= across <= 1.0 + 1e-9:
            best = min(best, along)
    return best


def _stair_run(footprint: Sequence[Point2D], core: Point2D, axis: Point2D) -> tuple[Point2D, float]:
    """Which way off the shaft a landing may step, and how far it may go.

    The long axis says the orientation; it does not say there is room. A stair
    at the assumed pitch wants about 3.3 m of run for a 3.5 m storey, and an
    outbuilding six metres on its long side does not have that to give from its
    own centre -- so the unclamped landing walked out through the wall and was
    published as WGS-84 to seven decimals, which is the identical defect
    :func:`_core_point` was written to stop, committed again one function later.

    So the ray is cast both ways along the axis, the roomier side wins, and the
    run is capped short of the wall it would otherwise cross. The *length* of the
    flight is not capped with it: see :func:`build_graph`.
    """
    forward = _exit_distance(footprint, core, axis)
    backward = _exit_distance(footprint, core, (-axis[0], -axis[1]))
    if backward > forward:
        axis, forward = (-axis[0], -axis[1]), backward
    if not math.isfinite(forward):  # pragma: no cover - core outside its own ring
        return axis, 0.0
    return axis, max(0.0, forward - STAIR_WALL_CLEARANCE_M)


# ---------------------------------------------------------------- cost model


def _thermal_terms(
    label: FaceLabel,
    report: FaceCoverage | None,
    voids: Sequence[VoidObservation],
    *,
    from_id: str,
    to_id: str,
) -> tuple[tuple[CostTerm, ...], Barrier | None]:
    """Everything the sensors say, or do not say, about one wall.

    Returns the terms and -- separately -- the barrier, because a barrier is not
    an expensive term. The caller builds no edge at all when one comes back.
    """
    terms: list[CostTerm] = []
    face_ref = str(label)

    if report is None or not report.scanned or report.peak_c is None:
        reason = "no current thermal frame" if report is None else report.render[:200]
        terms.append(
            CostTerm(
                term_id="thermal.unscanned",
                weight=W_UNKNOWN_FACE,
                detail=(
                    f"{face_ref} has no current coverage ({reason}). UNSCANNED is "
                    "unknown, not safe, and is priced above a wall measured warm."
                ),
                refs=(face_ref,),
            )
        )
        return tuple(terms), None

    peak = report.peak_c
    if peak >= THERMAL_BARRIER_C:
        return (), Barrier(
            from_id=from_id,
            to_id=to_id,
            reason=(
                f"{face_ref} measured {peak:.0f} C peak surface temperature, at or above "
                f"the {THERMAL_BARRIER_C:.0f} C barrier; no leg was built through it"
            ),
            refs=(face_ref,),
        )

    if peak > THERMAL_BASELINE_C:
        span = THERMAL_BARRIER_C - THERMAL_BASELINE_C
        weight = W_THERMAL_MAX * (peak - THERMAL_BASELINE_C) / span
        terms.append(
            CostTerm(
                term_id="thermal.measured",
                weight=round(weight, 4),
                detail=(
                    f"{face_ref} measured {peak:.0f} C peak surface temperature, "
                    f"{peak - THERMAL_BASELINE_C:.0f} C above the {THERMAL_BASELINE_C:.0f} C "
                    f"baseline and below the {THERMAL_BARRIER_C:.0f} C barrier"
                ),
                refs=(face_ref,),
            )
        )

    gap = 1.0 - report.coverage
    if gap > 0.0:
        terms.append(
            CostTerm(
                term_id="thermal.partial-coverage",
                weight=round(W_COVERAGE_GAP * gap, 4),
                detail=(
                    f"{face_ref} was flown at {report.coverage * 100:.0f}% of the face; "
                    f"the remaining {gap * 100:.0f}% is unmeasured"
                ),
                refs=(face_ref,),
            )
        )

    counted = [v for v in voids if v.face is label][:MAX_VOIDS_COUNTED]
    if counted:
        terms.append(
            CostTerm(
                term_id="thermal.void",
                weight=round(W_VOID * len(counted), 4),
                detail=(
                    f"{len(counted)} measured temperature difference(s) above the "
                    f"{counted[0].threshold_c:.0f} C threshold on {face_ref}; an observation "
                    "about the surface, not a finding about what is behind it"
                ),
                refs=(face_ref, *(f"region-{v.region_index}" for v in counted)),
            )
        )

    return tuple(terms), None


def _obstruction_terms(spec: GeometrySpec, wall: _Wall) -> tuple[CostTerm, ...]:
    """Roof obstructions attributed to the wall they sit over.

    An :class:`~firstdue.domain.geometry.Obstruction` names a roof segment, and
    a segment names an azimuth. Attributing one to a face is therefore the same
    arithmetic that attributes a camera to a face, against the same tolerance --
    and an obstruction on a segment that faces no wall is deliberately not
    attributed to a wall at all rather than to the nearest one.
    """
    matched: list[str] = []
    for obstruction in spec.obstructions:
        if obstruction.segment_index >= len(spec.roof_segments):  # pragma: no cover - validated
            continue
        segment = spec.roof_segments[obstruction.segment_index]
        if angular_distance_deg(segment.azimuth_deg, wall.bearing_deg) > FACE_BEARING_TOLERANCE_DEG:
            continue
        matched.append(str(obstruction.type))
    if not matched:
        return ()
    counted = sorted(matched)[:MAX_OBSTRUCTIONS_COUNTED]
    return (
        CostTerm(
            term_id="obstruction.on-face",
            weight=round(W_OBSTRUCTION * len(counted), 4),
            detail=(
                f"{', '.join(counted)} on the roof segment above {wall.label}; a crew "
                "cannot cut through it and, for a solar array or an EV charger, cannot "
                "de-energise it from the panel"
            ),
            refs=(str(wall.label), *counted),
        ),
    )


def _collapse_zone_term(spec: GeometrySpec, standoff_m: float) -> tuple[CostTerm, ...]:
    """The crossing itself, priced by how much of it is inside the zone.

    The approach ring stands at the collapse-zone radius wherever that radius
    clears :data:`MIN_STANDOFF_M`, so on most buildings the whole leg is inside
    the zone and this term is uniform across the four faces -- it does not
    choose a face, it prices going in at all against staying on the perimeter.
    """
    radius = spec.collapse_zone_radius_m
    if radius <= 0.0 or standoff_m <= 0.0:
        return ()
    fraction = min(1.0, radius / standoff_m)
    return (
        CostTerm(
            term_id="collapse.zone-crossing",
            weight=round(W_COLLAPSE_ZONE * fraction, 4),
            detail=(
                f"{fraction * 100:.0f}% of this leg lies inside the {radius:g} m collapse "
                f"zone -- the 1.5x standard applied to a measured height of "
                f"{spec.total_height_m:g} m. A geometric standard, not a prediction"
            ),
            refs=(spec.address_id,),
        ),
    )


def _structural_terms(
    spec: GeometrySpec,
    *,
    facts: Mapping[CanonicalKey, StructuralFact],
    conflicts: Sequence[Conflict],
    level_index: int | None,
) -> tuple[CostTerm, ...]:
    """What the record says about the structure the crew is standing in.

    Three sources, each cited: a confirmed lightweight truss, a storey whose
    mass the records dispute, and open disagreements on the attributes that
    change what an entry looks like. All of them are properties of being inside
    rather than of one wall, so they land on interior and vertical legs.
    """
    terms: list[CostTerm] = []

    truss = facts.get(Keys.LIGHTWEIGHT_TRUSS)
    # `is_known` first, then the value. A truss attribute that was checked and
    # found absent is a BooleanValue of False and buys no penalty; one nobody
    # checked is UnknownValue, whose `unwrap` raises rather than defaulting --
    # which is the module-wide rule that absence never reads as "no".
    if truss is not None and truss.value.is_known and truss.value.unwrap() is True:
        terms.append(
            CostTerm(
                term_id="structure.lightweight-truss",
                weight=W_LIGHTWEIGHT_TRUSS,
                detail=(
                    "the profile carries a confirmed lightweight truss for this address; "
                    "every interior and vertical leg is priced against it"
                ),
                refs=(Keys.LIGHTWEIGHT_TRUSS, truss.fact_id),
            )
        )

    if level_index is not None and level_index < len(spec.levels):
        level = spec.levels[level_index]
        if level.status is AssertionStatus.DISPUTED:
            terms.append(
                CostTerm(
                    term_id="structure.disputed-level",
                    weight=W_DISPUTED_LEVEL,
                    detail=(
                        f"storey {level_index + 1} is DISPUTED: the records disagree that it "
                        "is there, so its floor, its stairs and its height are unconfirmed"
                    ),
                    refs=tuple(r for r in (f"level-{level_index}", level.fact_id) if r),
                )
            )

    open_conflicts = [
        c
        for c in conflicts
        if c.status is ConflictStatus.OPEN and c.canonical_key in LOAD_BEARING_KEYS
    ]
    counted = sorted(open_conflicts, key=lambda c: (-c.severity, c.conflict_id))[
        :MAX_CONFLICTS_COUNTED
    ]
    if counted:
        terms.append(
            CostTerm(
                term_id="structure.open-conflict",
                weight=round(W_OPEN_CONFLICT * len(counted), 4),
                detail=(
                    f"{len(counted)} open disagreement(s) on load-bearing attributes; "
                    "the sources have not been reconciled and nobody has settled them on scene"
                ),
                refs=tuple(ref for c in counted for ref in (c.conflict_id, c.canonical_key))[:12],
            )
        )

    return tuple(terms)


# ------------------------------------------------------------ graph assembly


def build_graph(
    spec: GeometrySpec,
    *,
    coverage: Sequence[FaceCoverage] = (),
    voids: Sequence[VoidObservation] = (),
    facts: Mapping[CanonicalKey, StructuralFact] | None = None,
    conflicts: Sequence[Conflict] = (),
) -> NavGraph:
    """The navigable graph for one building, priced from one incident's data.

    Six kinds of node, and every one of them is somewhere a person could
    actually be: apparatus staging, an approach point off each wall, the door on
    each wall, the first room inside each wall, the core -- the stairwell shaft
    -- once per storey, and the landing each flight turns on between them. The
    perimeter ring joins the approaches, so walking round the building to a
    cooler wall is a route the search can actually find rather than a shape it
    has to guess.

    Interior movement runs through the core rather than wall to wall. That is
    the honest limit of what this system knows: the slow loop measured an
    outline and a storey count, not a floor plan, so a leg between two rooms
    would be a corridor nobody surveyed. The stairwell is the one interior
    feature the storey count implies.
    """
    by_face = {report.face: report for report in coverage}
    resolved_facts = facts or {}
    walls = _walls(spec)
    if not walls or not spec.levels:
        return NavGraph()

    centroid = _core_point(spec.footprint)
    stair_axis, stair_room = _stair_run(spec.footprint, centroid, _stair_axis(walls))
    standoff = max(MIN_STANDOFF_M, spec.collapse_zone_radius_m)

    nodes: list[NavNode] = []
    edges: list[NavEdge] = []
    barriers: list[Barrier] = []

    # Storey elevations: the floor of storey n is the sum of the heights below
    # it, which is what the levels already carry. Nothing here assumes a
    # ceiling height the geometry did not derive.
    elevations: list[float] = []
    running = 0.0
    for level in spec.levels:
        elevations.append(running)
        running += level.height_m

    core_ids: list[str] = []
    for index, elevation in enumerate(elevations):
        node_id = f"core:L{index}"
        core_ids.append(node_id)
        nodes.append(
            NavNode(
                node_id=node_id,
                x_m=centroid[0],
                y_m=centroid[1],
                z_m=elevation,
                kind="core",
                level=index,
            )
        )

    def add_pair(from_id: str, to_id: str, length: float, terms: Sequence[CostTerm]) -> None:
        """Both directions. A crew walks back out the way the arithmetic said in."""
        if length <= 0.0:  # pragma: no cover - coincident nodes are not built
            return
        edges.append(NavEdge(from_id=from_id, to_id=to_id, length_m=length, terms=tuple(terms)))
        edges.append(NavEdge(from_id=to_id, to_id=from_id, length_m=length, terms=tuple(terms)))

    node_by_id: dict[str, NavNode] = {n.node_id: n for n in nodes}

    def register(node: NavNode) -> NavNode:
        nodes.append(node)
        node_by_id[node.node_id] = node
        return node

    approach_by_face: dict[FaceLabel, NavNode] = {}
    for wall in walls:
        face_ref = str(wall.label)
        approach = register(
            NavNode(
                node_id=f"approach:{face_ref}",
                x_m=wall.midpoint[0] + wall.normal[0] * standoff,
                y_m=wall.midpoint[1] + wall.normal[1] * standoff,
                z_m=0.0,
                kind="approach",
                face=face_ref,
            )
        )
        approach_by_face[wall.label] = approach

        door_id = f"door:{face_ref}"
        thermal_terms, barrier = _thermal_terms(
            wall.label,
            by_face.get(wall.label),
            voids,
            from_id=approach.node_id,
            to_id=door_id,
        )
        if barrier is not None:
            # No door node either. A door nobody can reach is a place on a map
            # that reads as an option, and the barrier below is the whole point.
            barriers.append(barrier)
            continue

        door = register(
            NavNode(
                node_id=door_id,
                x_m=wall.midpoint[0],
                y_m=wall.midpoint[1],
                z_m=0.0,
                kind="door",
                face=face_ref,
            )
        )
        add_pair(
            approach.node_id,
            door.node_id,
            standoff,
            (
                *thermal_terms,
                *_obstruction_terms(spec, wall),
                *_collapse_zone_term(spec, standoff),
            ),
        )

        # The first room inside that wall, on the ground storey only: a door is
        # on the ground and nothing here knows where an upper-storey opening is.
        setback = min(
            INTERIOR_SETBACK_M,
            max(
                0.5,
                math.hypot(centroid[0] - wall.midpoint[0], centroid[1] - wall.midpoint[1]) / 2.0,
            ),
        )
        interior = register(
            NavNode(
                node_id=f"interior:{face_ref}:L0",
                x_m=wall.midpoint[0] - wall.normal[0] * setback,
                y_m=wall.midpoint[1] - wall.normal[1] * setback,
                z_m=0.0,
                kind="interior",
                face=face_ref,
                level=0,
            )
        )
        add_pair(door.node_id, interior.node_id, setback, ())
        add_pair(
            interior.node_id,
            core_ids[0],
            math.hypot(centroid[0] - interior.x_m, centroid[1] - interior.y_m),
            _structural_terms(spec, facts=resolved_facts, conflicts=conflicts, level_index=0),
        )

    # The perimeter ring, in fireground order, between the approaches that
    # exist. Outside the collapse zone by construction, so a ring move costs its
    # own distance and nothing else -- which is what makes a detour to a cooler
    # wall a thing the search can price.
    present = [label for label in GROUND_FACES if label in approach_by_face]
    for position, label in enumerate(present):
        other = present[(position + 1) % len(present)]
        if other == label:
            continue
        first, second = approach_by_face[label], approach_by_face[other]
        add_pair(
            first.node_id,
            second.node_id,
            math.hypot(first.x_m - second.x_m, first.y_m - second.y_m),
            (),
        )

    # Staging, on the address side. Alpha is the longest wall by the convention
    # `face_geometries` states and applies; where a department knows the street
    # is elsewhere, the massing model shows the labelling and an officer can see
    # it is wrong. Placed beyond the approach ring, outside the collapse zone.
    anchor = approach_by_face.get(FaceLabel.ALPHA) or approach_by_face[present[0]]
    anchor_wall = next(w for w in walls if str(w.label) == anchor.face)
    staging = register(
        NavNode(
            node_id="staging",
            x_m=anchor.x_m + anchor_wall.normal[0] * APPARATUS_SETBACK_M,
            y_m=anchor.y_m + anchor_wall.normal[1] * APPARATUS_SETBACK_M,
            z_m=0.0,
            kind="staging",
        )
    )
    for approach in approach_by_face.values():
        add_pair(
            staging.node_id,
            approach.node_id,
            math.hypot(staging.x_m - approach.x_m, staging.y_m - approach.y_m),
            (),
        )

    # The stairwell: two flights and a landing per storey, not a plumb line.
    #
    # The rise of each storey is measured -- it is the level height the geometry
    # already derived. What was wrong was treating that rise as the distance a
    # crew travels to climb it, which is only true of a lift shaft. A stair at
    # the steepest pitch the code permits covers `rise / sin(32.47 deg)`, about
    # 1.86 times the rise, and a switchback adds a landing on top of that. The
    # old single leg therefore understated the walk to a third floor by more
    # than half -- and understating distance is the direction that gets a crew
    # committed to a route they cannot finish on the air they have.
    #
    # The landing node carries the horizontal half of that: it stands off the
    # shaft along the building's long axis at half the storey's height, so each
    # flight is a real diagonal and the route reads as a stair. It moves nothing
    # about where the shaft is, which is still the inference it always was.
    pitch = math.radians(STAIR_PITCH_DEG)
    for index in range(len(core_ids) - 1):
        rise = spec.levels[index].height_m
        half_rise = rise / 2.0
        # The run of one flight, plus half the landing it turns on -- capped at
        # the room the footprint actually has on this axis.
        run = min(half_rise / math.tan(pitch) + STAIR_LANDING_M / 2.0, stair_room)
        landing = register(
            NavNode(
                node_id=f"stair:L{index}:mid",
                x_m=centroid[0] + stair_axis[0] * run,
                y_m=centroid[1] + stair_axis[1] * run,
                # A landing is between two storeys, so it is on neither. `level`
                # is documented as the storey a renderer draws a waypoint on;
                # claiming the one below would float this node above that floor's
                # plan. Staging and the approach ring say None for the same
                # reason, and mean the same thing by it.
                z_m=elevations[index] + half_rise,
                kind="stair",
                level=None,
            )
        )
        # Length is floored at the flight the assumed pitch implies, even where
        # the run was capped. A building too narrow to hold a code switchback
        # still has to be climbed, and the crew still walks the slope; what the
        # cap buys is a landing inside the building, not a shorter journey.
        flight = max(math.hypot(run, half_rise), half_rise / math.sin(pitch))
        terms = _structural_terms(
            spec, facts=resolved_facts, conflicts=conflicts, level_index=index + 1
        )
        add_pair(core_ids[index], landing.node_id, flight, terms)
        add_pair(landing.node_id, core_ids[index + 1], flight, terms)

    return NavGraph(nodes=tuple(nodes), edges=tuple(edges), barriers=tuple(barriers))


# ------------------------------------------------------------------ the plan


def _explain(leg: PathLeg) -> str:
    """One sentence per leg, composed from its own terms. Never authored.

    The wording is a template and the numbers are the search's; there is no path
    by which a model writes this, because a sentence explaining why a crew was
    routed through a wall has to be checkable against the arithmetic that routed
    them.
    """
    if not leg.terms:
        return (
            f"{leg.distance_m:.1f} m at cost {leg.cost:.1f}: distance only -- nothing "
            "measured on this leg raised its price"
        )
    named = ", ".join(f"{term.term_id} +{term.weight:.2f}" for term in leg.terms)
    return f"{leg.distance_m:.1f} m at cost {leg.cost:.1f} " f"({leg.multiplier:.2f}x for {named})"


def _route(solution: PathSolution, graph: NavGraph, origin: GeoOrigin | None) -> Route:
    by_id = graph.by_id
    waypoints: list[Waypoint] = []
    for node_id in solution.node_ids:
        node = by_id[node_id]
        longitude: float | None = None
        latitude: float | None = None
        if origin is not None:
            longitude, latitude = origin.to_wgs84(node.x_m, node.y_m)
        waypoints.append(
            Waypoint(
                node_id=node.node_id,
                kind=node.kind,
                face=node.face,
                level=node.level,
                x_m=round(node.x_m, 3),
                y_m=round(node.y_m, 3),
                z_m=round(node.z_m, 3),
                longitude=round(longitude, 7) if longitude is not None else None,
                latitude=round(latitude, 7) if latitude is not None else None,
            )
        )
    return Route(
        waypoints=tuple(waypoints),
        legs=tuple(
            RouteLeg(
                from_id=leg.from_id,
                to_id=leg.to_id,
                distance_m=leg.distance_m,
                cost=leg.cost,
                multiplier=leg.multiplier,
                terms=leg.terms,
                avoided=leg.avoided,
                chose_because=_explain(leg),
            )
            for leg in solution.legs
        ),
        total_cost=solution.total_cost,
        total_distance_m=solution.total_distance_m,
        expanded_nodes=solution.expanded,
    )


def compute_entry_path(
    *,
    incident_id: str,
    spec: GeometrySpec | None,
    coverage: Sequence[FaceCoverage] = (),
    voids: Sequence[VoidObservation] = (),
    facts: Mapping[CanonicalKey, StructuralFact] | None = None,
    conflicts: Sequence[Conflict] = (),
    origin: GeoOrigin | None = None,
    target_level: int = 0,
) -> EntryPathPlan:
    """Solve one entry and one egress, or refuse and say why.

    ``target_level`` is zero-based and defaults to the ground storey, which is
    the only storey a route can reach without assuming a stairwell somebody
    surveyed. A caller that knows the floor of origin -- the intake binds one,
    marked as reported -- passes it and gets a route to that storey's core.

    The egress is a *second* search, from the target to the best approach point
    on a face the entry did not use. That is the question a crew actually asks,
    and it is not answered by reversing the entry: the cheapest way in and the
    cheapest second way out are different searches over the same graph, and on a
    building with one usable face there is no second answer -- which this says
    rather than papering over.
    """
    address_id = spec.address_id if spec is not None else ""
    if spec is None:
        return EntryPathPlan(
            incident_id=incident_id,
            address_id=address_id or incident_id,
            refused=True,
            refusal_reason=(
                "no pre-incident geometry for this address, so there is no footprint to "
                "route over; the slow loop has to measure the building before a path means "
                "anything"
            ),
            refusal_refs=(incident_id,),
        )
    if not spec.levels:
        return EntryPathPlan(
            incident_id=incident_id,
            address_id=address_id,
            refused=True,
            refusal_reason=(
                "the massing model carries no storeys, so there is no interior to route into"
            ),
            refusal_refs=(address_id,),
        )
    if target_level >= len(spec.levels):
        return EntryPathPlan(
            incident_id=incident_id,
            address_id=address_id,
            target_level=target_level,
            refused=True,
            refusal_reason=(
                f"storey {target_level + 1} was asked for and the massing model has "
                f"{len(spec.levels)}; nothing here invents a floor the geometry does not have"
            ),
            refusal_refs=(address_id,),
        )

    graph = build_graph(spec, coverage=coverage, voids=voids, facts=facts, conflicts=conflicts)
    unscanned = tuple(str(r.face) for r in coverage if not r.scanned)

    if not graph.nodes:
        return EntryPathPlan(
            incident_id=incident_id,
            address_id=address_id,
            target_level=target_level,
            refused=True,
            refusal_reason=(
                "no face of the measured footprint resolves to a wall a crew could "
                "approach, so no navigable graph could be built"
            ),
            refusal_refs=(address_id,),
            unscanned_faces=unscanned,
        )

    goal_id = f"core:L{target_level}"
    outcome = astar(graph, start_id="staging", goal_id=goal_id)
    if isinstance(outcome, PathRefusal):
        return EntryPathPlan(
            incident_id=incident_id,
            address_id=address_id,
            target_level=target_level,
            refused=True,
            refusal_reason=outcome.reason,
            refusal_refs=outcome.refs,
            barriers=graph.barriers,
            unscanned_faces=unscanned,
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
        )

    entry = _route(outcome, graph, origin)
    entry_face = next((w.face for w in entry.waypoints if w.kind == "door"), "")

    # The second way out. Every approach on a face the entry did not use, priced
    # by the same arithmetic, cheapest first and ties broken on node id.
    #
    # A candidate counts only if the route to it goes through *that face's own
    # door*. Without the check, three barred walls still produce an "egress":
    # out through the one usable opening and round the perimeter ring to another
    # approach point. That is a real walk and it is not a second way out, and
    # printing it as one on a building with a single usable opening is precisely
    # the wrong thing to hand a crew.
    egress: Route | None = None
    note = ""
    nodes_by_id = graph.by_id
    best: PathSolution | None = None
    for node in sorted(graph.nodes, key=lambda n: n.node_id):
        if node.kind != "approach" or node.face == entry_face:
            continue
        door_id = f"door:{node.face}"
        if door_id not in nodes_by_id:
            continue
        result = astar(graph, start_id=goal_id, goal_id=node.node_id)
        if not isinstance(result, PathSolution) or door_id not in result.node_ids:
            continue
        if best is None or result.total_cost < best.total_cost:
            best = result
    if best is not None:
        egress = _route(best, graph, origin)
    else:
        note = (
            f"no second way out: every face other than {entry_face or 'the entry face'} "
            "is either absent from the measured footprint or behind a barrier, so the only "
            "way out of this graph is the way in"
        )

    return EntryPathPlan(
        incident_id=incident_id,
        address_id=address_id,
        target_level=target_level,
        entry=entry,
        egress=egress,
        egress_note=note,
        barriers=graph.barriers,
        unscanned_faces=unscanned,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
    )


__all__ = [
    "LOAD_BEARING_KEYS",
    "THERMAL_BARRIER_C",
    "THERMAL_BASELINE_C",
    "EntryPathPlan",
    "GeoOrigin",
    "Route",
    "RouteLeg",
    "Waypoint",
    "build_graph",
    "compute_entry_path",
]
