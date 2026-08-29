'use client';

/**
 * The structure, built as a building rather than as a block.
 *
 * This replaces the massing renderer wholesale. That one extruded the footprint
 * to a height and stopped, which was defensible -- a prism is exactly what a
 * storey count and a parcel outline justify -- and it read as slop, because a
 * featureless box does not look like a thing anyone has ever stood outside of.
 * An officer cannot check a model against a building if the model has no
 * openings to check.
 *
 * So this generates the *architecture* the spec implies: storeys separated by
 * floor bands, a window grid on every elevation, a doorway on the address side,
 * a parapet or a pitched roof depending on what the roof segments say, and the
 * roof obstructions standing where the records put them.
 *
 * **Nothing here invents a fact.** Every dimension traces to the spec: the
 * outline is the measured footprint, the storey heights are the levels the slow
 * loop filed, the roof pitch is the roof segment, the obstructions are the ones
 * on record. What is *invented* is only what any elevation drawing invents --
 * where windows sit on a wall nobody surveyed window by window. That is drawn
 * as regular fenestration and it is called out in the caption as indicative, so
 * nobody reads a window count off a picture and puts a crew through it.
 *
 * A DISPUTED storey stays visually distinct: it is the whole reason this system
 * exists, and it must survive the prettier rendering.
 *
 * **Three.js r128**, imported under the `three-r128` alias. The photorealistic
 * tiles beside this need three >= 0.167, so the two versions coexist rather
 * than one being downgraded into the other's way. They never share an object;
 * each owns its own renderer, scene and canvas.
 */

import { useEffect, useRef, useState } from 'react';

import type * as ThreeNS from 'three-r128';

import type { FaceView, GeometryView, RouteView } from '@/lib/api/types';

export type ViewAngle = 'ALPHA' | 'BRAVO' | 'CHARLIE' | 'DELTA' | 'ISO';

/**
 * A computed route, drawn over the massing model.
 *
 * **Only ever passed once a package exists.** A route on screen from the moment
 * an incident opens says nothing: the point of this drawing is that it is the
 * *outcome* of the interceptor deciding the record was good enough to compute
 * one. Before that the structure view is what it always was, and the caller
 * gates on the package rather than on the incident.
 *
 * Drawn in footprint-local metres, which is the frame the waypoints already
 * carry and the same frame the measured footprint is in -- so the two land on
 * each other with no transform either side invented. The waypoints' WGS-84
 * fields are deliberately unused here: they are null whenever the city could
 * not place the parcel, and a renderer that filled in an origin would be
 * putting a crew's route at coordinates nobody surveyed.
 */
export interface RouteOverlay {
  entry: RouteView | null;
  egress: RouteView | null;
  /** The leg under the cursor or under selection, drawn brighter than the rest. */
  highlight: { route: 'entry' | 'egress'; leg: number } | null;
  /**
   * Which computed route this is -- the package id, in practice.
   *
   * The progressive draw restarts when this changes and **never** when the
   * highlight does. Without it, hovering the leg list would replay the walk
   * from the staging point on every pointer move, because a highlight change
   * hands this component a new overlay object exactly the way a new package
   * does. The identity of the route is a fact the caller has; asking the
   * renderer to infer it from deep-equal waypoints would be guessing.
   */
  drawKey: string;
}

/** Entry is `live`, egress is `confirmed` -- and both carry a label as well. */
const ROUTE_COLORS = { entry: 0x38bdf8, egress: 0x4ade80, highlight: 0xe8edf4 } as const;

// ------------------------------------------------------- the route's draw --

/**
 * How long the whole route may take to draw, entry and egress together.
 *
 * **Charged against the two minutes the rest of the system is racing.** This
 * animation lands at the culmination of an incident: the fleet has composed a
 * package and a human is about to be asked to sign it. A draw that took ten
 * seconds would be spending the officer's decision time on a transition, and
 * an officer who has to wait for a picture learns to tap through it.
 *
 * 1.6 s is about the length of one held glance -- long enough to follow a line
 * from the kerb through a door and up a stair and understand it as a walk,
 * short enough that nobody reaches for the mouse before it lands.
 */
export const ROUTE_DRAW_BUDGET_MS = 1600;

/**
 * The pace one leg would take if the route were short enough to afford it.
 *
 * 220 ms is where a stroke still reads as *travel*. Much below that and
 * consecutive legs blur into a single wipe, which says "a line appeared"
 * rather than "a crew goes this way"; much above and a five-leg route alone
 * eats the budget.
 *
 * The budget wins over the pace. A four-leg route takes 880 ms at the full
 * pace; a twenty-leg route compresses to 80 ms a leg and still finishes at
 * 1.6 s. That is the whole point of having both numbers: **the officer's wait
 * is fixed and the leg count moves the pace**, never the other way round. A
 * route computed through a deep building must not cost more of a commander's
 * attention than a route into a shopfront.
 */
export const ROUTE_LEG_MS = 220;

/**
 * The beat between the entry route finishing and the egress starting.
 *
 * The second way out is a separate answer to a separate question, and drawing
 * it continuously off the end of the entry route would read as one long walk
 * that doubles back. The pause is what makes the green a second statement
 * rather than more of the blue.
 */
export const ROUTE_EGRESS_GAP_MS = 160;

/** The tail of a leg over which the waypoint it reaches comes up. */
const MARKER_LAND_FRACTION = 0.34;

export interface RouteDrawSchedule {
  /** Legs in the entry route: one fewer than its waypoints, never negative. */
  entryLegs: number;
  egressLegs: number;
  /** What one leg takes here. Below `ROUTE_LEG_MS` on a long route. */
  legMs: number;
  /** The beat before the egress. Zero unless both routes are present. */
  gapMs: number;
  /** When the sequence is over. **Zero means there is nothing to draw** --
      an empty overlay, a refusal, or a reader who asked for no motion. */
  totalMs: number;
}

/** Legs in a route: the walk between consecutive waypoints, and no more. */
function legCount(drawn: RouteView | null): number {
  if (!drawn || drawn.waypoints.length < 2) return 0;
  return drawn.waypoints.length - 1;
}

/**
 * The timetable for drawing one overlay, derived and never authored.
 *
 * Pure, and exported, because two clocks read it: the frame loop below, which
 * decides how much of each leg is on screen, and the console, which decides
 * when the approval card may interrupt. They agree because they compute the
 * same number from the same overlay rather than because one tells the other.
 *
 * The order is the order the waypoints arrived in -- staging, approach, door,
 * interior, core, and up through the levels -- because that is the order the
 * search returned them in and it is the order a crew walks them. Nothing here
 * sorts, and nothing here interpolates a leg the plan did not contain.
 */
export function routeDrawSchedule(
  overlay: RouteOverlay | null,
  options: { reducedMotion?: boolean } = {},
): RouteDrawSchedule {
  const entryLegs = legCount(overlay?.entry ?? null);
  const egressLegs = legCount(overlay?.egress ?? null);
  const legs = entryLegs + egressLegs;
  // Reduced motion is not a faster animation, it is no animation: the whole
  // route is already there and the card may be raised on the same tick.
  if (legs === 0 || options.reducedMotion) {
    return { entryLegs, egressLegs, legMs: 0, gapMs: 0, totalMs: 0 };
  }
  const gapMs = entryLegs > 0 && egressLegs > 0 ? ROUTE_EGRESS_GAP_MS : 0;
  const legMs = Math.min(ROUTE_LEG_MS, (ROUTE_DRAW_BUDGET_MS - gapMs) / legs);
  // Clamped as well as derived: `legs * (budget / legs)` lands a floating-point
  // hair over the budget, and "at most 1.6 s" has to be true of the number the
  // console gates on, not merely of the arithmetic that produced it.
  const totalMs = Math.min(ROUTE_DRAW_BUDGET_MS, legs * legMs + gapMs);
  return { entryLegs, egressLegs, legMs, gapMs, totalMs };
}

export interface RouteDrawState {
  /** `legs` is fractional: 2.4 is two whole legs and a fifth of a third. */
  entry: { begun: boolean; legs: number };
  egress: { begun: boolean; legs: number };
  /** True once the egress -- or the entry, when there is no egress -- is whole. */
  complete: boolean;
}

/**
 * Where the draw has got to at `elapsedMs`, as legs rather than as pixels.
 *
 * Separated from the renderer so the schedule can be checked without a GPU,
 * and so the console can gate the approval card on the same arithmetic the
 * drawing obeys instead of on a duration copied into a second place.
 */
export function routeDrawState(
  schedule: RouteDrawSchedule,
  elapsedMs: number,
): RouteDrawState {
  const { entryLegs, egressLegs, legMs, gapMs, totalMs } = schedule;
  // Whole once the clock is past the total, stated rather than left to fall
  // out of the division: the total is clamped to the budget above, so on a
  // long route the last leg would otherwise finish a floating-point hair short
  // of its own waypoint and stay there.
  if (totalMs <= 0 || legMs <= 0 || elapsedMs >= totalMs) {
    return {
      entry: { begun: true, legs: entryLegs },
      egress: { begun: true, legs: egressLegs },
      complete: true,
    };
  }
  const elapsed = Math.max(0, elapsedMs);
  const entryEnds = entryLegs * legMs;
  const egressBegins = entryEnds + gapMs;
  return {
    entry: { begun: true, legs: Math.min(entryLegs, elapsed / legMs) },
    egress: {
      begun: elapsed >= egressBegins,
      legs: Math.min(egressLegs, Math.max(0, (elapsed - egressBegins) / legMs)),
    },
    complete: elapsed >= totalMs,
  };
}

/**
 * One leg as a mesh that can be *extended*, rather than as finished geometry.
 *
 * A cylinder's local +Y is its own length, so growing it means scaling Y and
 * sliding the mesh half the new length along its own direction -- otherwise it
 * grows from its middle in both directions at once, which is a leg appearing,
 * not a crew walking. The start and the unit direction are kept for exactly
 * that sum; they are the waypoints the API returned, not a resampled curve.
 */
interface DrawnLeg {
  mesh: ThreeNS.Mesh;
  /** The leg's place in the *plan*, which is not its place in this array: a
      degenerate leg draws no mesh and the schedule still spends a beat on it. */
  index: number;
  from: ThreeNS.Vector3;
  direction: ThreeNS.Vector3;
  length: number;
}

interface DrawnMarker {
  mesh: ThreeNS.Mesh;
  /** How many legs of this route must be walked before it lands. 0 is the start. */
  at: number;
}

interface DrawnRoute {
  kind: 'entry' | 'egress';
  legs: DrawnLeg[];
  markers: DrawnMarker[];
}

/**
 * The four ground faces, in the order the backend labels them.
 *
 * These name *framings*, not a cage. The camera orbits freely; a named view is
 * the shortest way back to a wall an officer can call over the radio, and
 * double-click returns to whichever one was last chosen.
 */
const GROUND_FACES = ['ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'] as const;

/**
 * Which wall the backend calls ALPHA, and therefore where each camera stands.
 *
 * Mirrors `firstdue.domain.geometry.face_geometries` exactly: **Alpha is the
 * longest wall**, ties break on the lower bearing so a square footprint labels
 * identically on every run, and Bravo, Charlie and Delta follow clockwise.
 *
 * This has to be mirrored rather than approximated. Fixed compass cameras put
 * the ALPHA view on whichever wall happened to face +Z, which on a deep narrow
 * lot -- the ordinary San Francisco parcel -- is the *short* wall, not Alpha.
 * An officer told they are looking at Alpha and shown Bravo is worse served
 * than one shown no label at all. The same index drives where the door goes
 * and which wall a thermal frame paints onto.
 *
 * Bearings are compass degrees clockwise from north, and the footprint is ENU
 * (x east, y north), so the outward normal of an edge is the edge vector
 * rotated -90 degrees.
 */
function faceBearings(footprint: [number, number][]): Record<string, number> {
  let alphaBearing = 0;
  let best: { length: number; bearing: number } | null = null;
  for (let i = 0; i < footprint.length; i += 1) {
    const a = footprint[i]!;
    const b = footprint[(i + 1) % footprint.length]!;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const length = Math.hypot(dx, dy);
    if (length <= 0) continue;
    const bearing = ((Math.atan2(dy, -dx) * 180) / Math.PI + 360) % 360;
    // Longest wins; a tie goes to the lower bearing, as the backend does.
    if (!best || length > best.length || (length === best.length && bearing < best.bearing)) {
      best = { length, bearing };
    }
  }
  if (best) alphaBearing = best.bearing;
  const out: Record<string, number> = {};
  GROUND_FACES.forEach((label, offset) => {
    out[label] = (alphaBearing + 90 * offset) % 360;
  });
  return out;
}

/** How far the camera stands off, as a multiple of the structure's size. */
const CAMERA_STANDOFF = 2.1;

const COLORS = {
  wall: 0x8b94a0,
  wallDisputed: 0xfbbf24,
  band: 0x6b7480,
  glass: 0x121821,
  frame: 0x39424f,
  door: 0x2a3341,
  roof: 0x5c6572,
  parapet: 0x707a86,
  solar: 0xf87171,
  obstruction: 0x38bdf8,
  collapse: 0xfbbf24,
  edge: 0x0d1117,
};

/**
 * The thermal ramp: **one hue, monotonic lightness**, ambient to hot.
 *
 * Single-hue rather than the classic ironbow, because a multi-hue ramp makes
 * magnitude a hue comparison and readers cannot order hues reliably. Lightness
 * carries the magnitude and it is monotonic across all five steps.
 *
 * The coolest step keeps real chroma on purpose: a desaturated dark step would
 * read as this system's `unknown` grey, and "cool wall" must never be
 * confusable with "no data". Validated against the #12161c surface.
 *
 * Colour is never the only encoding -- every cell is labelled with its
 * temperature, and the two darkest steps fall below 3:1 against the surface,
 * which makes those labels required rather than decorative.
 */
const THERMAL_RAMP = [0x8a4410, 0xb25a10, 0xd97410, 0xf5a02a, 0xffce68] as const;

/** Domain of the ramp, Celsius. Stated so the legend can say what it means. */
const THERMAL_MIN_C = 20;
const THERMAL_MAX_C = 400;

export function thermalStep(celsius: number): number {
  const span = THERMAL_MAX_C - THERMAL_MIN_C;
  const t = (celsius - THERMAL_MIN_C) / span;
  return Math.min(THERMAL_RAMP.length - 1, Math.max(0, Math.round(t * (THERMAL_RAMP.length - 1))));
}

export function thermalColor(celsius: number): number {
  // `thermalStep` is clamped into range, so the lookup always hits.
  return THERMAL_RAMP[thermalStep(celsius)] ?? THERMAL_RAMP[0];
}

/** How long one storey takes to rise. */
const LEVEL_RISE_MS = 260;
/** A beat between the mass finishing and the thermal arriving. */
const THERMAL_DELAY_MS = 180;
/** How long the heat map takes to settle on. */
const THERMAL_FADE_MS = 420;
/** The overlay's resting opacity.
 *
 * Lowered from 0.82 once the walls had windows on them: at that strength the
 * heat map covered the architecture and the model went back to reading as a
 * coloured block. The overlay has to sit *on* a building that is still
 * visible, and the legend carries the exact numbers regardless. */
const THERMAL_OPACITY = 0.6;

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value));
/** Decelerating, so a storey lands rather than snapping to height. */
const easeOut = (t: number): number => 1 - (1 - t) ** 3;

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function webglAvailable(): boolean {
  if (typeof document === 'undefined') return false;
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

/** The face an officer is looking at, as a label they would say aloud. */
export function faceLabelFor(view: ViewAngle): string {
  return view === 'ISO' ? 'Isometric' : `${view} face`;
}

// --------------------------------------------------------------- generation --

/** Window opening, metres. Domestic proportions: taller than wide. */
const WINDOW_W = 1.15;
const WINDOW_H = 1.5;
/** Height of a window sill above its own floor. */
const SILL_H = 0.95;
/** Target spacing between window centres along a wall. */
const WINDOW_PITCH = 3.1;
/** A wall shorter than this gets no windows: it is a party wall or a return. */
const MIN_WINDOWED_WALL = 2.6;
/** How far openings sit proud of the wall, to beat z-fighting. */
const PROUD = 0.04;

const DOOR_W = 1.2;
const DOOR_H = 2.25;
/** Parapet above the roof deck on a flat roof. */
const PARAPET_H = 0.7;

interface Edge {
  /** Start and end of the wall, in centred metres. */
  ax: number;
  az: number;
  bx: number;
  bz: number;
  length: number;
  /** Outward normal, unit. */
  nx: number;
  nz: number;
  /** Rotation about Y that makes a +Z-facing plane lie on this wall. */
  angle: number;
}

/**
 * The footprint as centred wall segments.
 *
 * Centring matters: the camera positions above are absolute, so an uncentred
 * parcel would sit off frame for any address whose coordinates happen to be
 * large. Winding is normalised so the outward normal is genuinely outward
 * rather than depending on which way the source polygon happened to run.
 */
function edgesOf(footprint: [number, number][]): { edges: Edge[]; cx: number; cz: number } {
  const cx = footprint.reduce((sum, p) => sum + p[0], 0) / footprint.length;
  const cz = footprint.reduce((sum, p) => sum + p[1], 0) / footprint.length;

  // Shoelace: a negative area means clockwise, and the normal has to flip.
  let area = 0;
  for (let i = 0; i < footprint.length; i += 1) {
    const p = footprint[i]!;
    const q = footprint[(i + 1) % footprint.length]!;
    area += p[0] * q[1] - q[0] * p[1];
  }
  const winding = area >= 0 ? 1 : -1;

  const edges: Edge[] = [];
  for (let i = 0; i < footprint.length; i += 1) {
    const p = footprint[i]!;
    const q = footprint[(i + 1) % footprint.length]!;
    const ax = p[0] - cx;
    const az = p[1] - cz;
    const bx = q[0] - cx;
    const bz = q[1] - cz;
    const dx = bx - ax;
    const dz = bz - az;
    const length = Math.hypot(dx, dz);
    if (length < 0.05) continue;
    // Outward normal of an edge running (dx,dz) is (dz,-dx) for CCW winding.
    const nx = (dz / length) * winding;
    const nz = (-dx / length) * winding;
    edges.push({ ax, az, bx, bz, length, nx, nz, angle: Math.atan2(nx, nz) });
  }
  return { edges, cx, cz };
}

/** Where the openings go along one wall, as offsets from the wall's midpoint. */
function openingOffsets(length: number, pitch: number, itemWidth: number): number[] {
  const usable = length - 1.2; // leave a quoin at each end
  if (usable < itemWidth) return [];
  const count = Math.max(1, Math.floor(usable / pitch));
  const step = usable / count;
  const offsets: number[] = [];
  for (let i = 0; i < count; i += 1) {
    offsets.push(-usable / 2 + step * (i + 0.5));
  }
  return offsets;
}


/**
 * The roof the segments actually describe, as real sloping planes.
 *
 * The first attempt drew a smaller extrusion sitting on the deck, which
 * rendered as a grey plinth -- a box on a box, which is exactly the reading
 * this rewrite existed to get rid of. A roof has to slope.
 *
 * What the data gives is a pitch and one azimuth per plane, so the *count* is
 * what decides the form: two planes is a gable, four is a hip, and the pitch
 * fixes the rise from the span it climbs. Nothing is guessed except which axis
 * the ridge runs along, and that follows the footprint -- a ridge across the
 * short axis of a deep lot is not a roof anyone has built.
 *
 * Built as raw triangles with `DoubleSide`, so a winding mistake shows as a
 * shading artefact rather than as a hole in the roof.
 */
function roofGeometry(
  THREE: typeof ThreeNS,
  bounds: { minX: number; maxX: number; minZ: number; maxZ: number },
  pitchDeg: number,
  segments: number,
): { geometry: ThreeNS.BufferGeometry; rise: number } {
  // Eaves overhang. Small, but it is the difference between a roof and a lid.
  const EAVES = 0.35;
  const minX = bounds.minX - EAVES;
  const maxX = bounds.maxX + EAVES;
  const minZ = bounds.minZ - EAVES;
  const maxZ = bounds.maxZ + EAVES;

  const width = maxX - minX;
  const depth = maxZ - minZ;
  const alongZ = depth >= width;
  const span = alongZ ? width : depth;
  const rise = Math.min(5, (span / 2) * Math.tan((pitchDeg * Math.PI) / 180));

  const cx = (minX + maxX) / 2;
  const cz = (minZ + maxZ) / 2;
  // A hip pulls the ridge in from both ends by half the span it climbs.
  const hip = segments >= 4;
  const inset = hip ? span / 2 : 0;

  const tri: number[] = [];
  const push = (
    a: [number, number, number],
    b: [number, number, number],
    c: [number, number, number],
  ) => tri.push(...a, ...b, ...c);
  const quad = (
    a: [number, number, number],
    b: [number, number, number],
    c: [number, number, number],
    d: [number, number, number],
  ) => {
    push(a, b, c);
    push(a, c, d);
  };

  if (alongZ) {
    const r0: [number, number, number] = [cx, rise, minZ + inset];
    const r1: [number, number, number] = [cx, rise, maxZ - inset];
    const a: [number, number, number] = [minX, 0, minZ];
    const b: [number, number, number] = [maxX, 0, minZ];
    const c: [number, number, number] = [maxX, 0, maxZ];
    const d: [number, number, number] = [minX, 0, maxZ];
    quad(a, d, r1, r0); // the -X slope
    quad(c, b, r0, r1); // the +X slope
    if (hip) {
      push(a, r0, b);
      push(c, r1, d);
    } else {
      // Gable ends: vertical triangles closing the roof against the wall.
      push(a, r0, b);
      push(c, r1, d);
    }
  } else {
    const r0: [number, number, number] = [minX + inset, rise, cz];
    const r1: [number, number, number] = [maxX - inset, rise, cz];
    const a: [number, number, number] = [minX, 0, minZ];
    const b: [number, number, number] = [maxX, 0, minZ];
    const c: [number, number, number] = [maxX, 0, maxZ];
    const d: [number, number, number] = [minX, 0, maxZ];
    quad(a, b, r1, r0); // the -Z slope
    quad(c, d, r0, r1); // the +Z slope
    push(a, r0, d);
    push(b, c, r1);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(tri, 3));
  geometry.computeVertexNormals();
  return { geometry, rise };
}


/**
 * Orbit, zoom and pan, written here rather than pulled in.
 *
 * r128 ships `OrbitControls`, and it imports from the bare specifier `three`
 * -- which in this project resolves to the *modern* copy that the tile
 * renderer needs. Using it would run one controller across two three
 * instances, mixing modern `Vector3`/`Spherical` maths into r128 objects. That
 * mostly works, which is the problem: it fails subtly and later. Vendoring the
 * file to repoint its import would be a thousand lines of somebody else's code
 * to maintain for a few hundred lines of behaviour.
 *
 * So this is the behaviour, directly: drag to orbit, wheel or pinch to zoom,
 * shift-drag or two-finger drag to pan, double-click to return to the framing
 * the named view chose. The polar angle is clamped above the horizon because
 * the tileset of a building has no underside -- letting the camera go beneath
 * it shows the inside of the walls and reads as a rendering fault.
 *
 * Damped: the camera eases toward where the pointer put it, so a named view
 * arrives as a move rather than a cut, and an officer keeps their bearings.
 */
interface Orbit {
  /** Azimuth, radians. 0 looks from +Z. */
  theta: number;
  /** Polar angle from +Y, radians. Clamped off both poles. */
  phi: number;
  /** Orthographic zoom. Larger is closer. */
  zoom: number;
  /** What the camera looks at, in metres. */
  target: { x: number; y: number; z: number };
}

/** Never quite overhead: at the pole `lookAt` has no stable up vector and the
 *  model rolls. This also keeps a plan view readable as a building. */
const MIN_PHI = 0.28;
/** Just above the horizon: below it the camera is under the building. */
const MAX_PHI = Math.PI / 2 - 0.02;
const MIN_ZOOM = 0.35;
const MAX_ZOOM = 6;
/** How fast the camera catches up with the pointer. 1 would be instant. */
const DAMPING = 0.18;

/** A compass bearing as an orbit azimuth. */
function azimuthForBearing(bearingDeg: number): number {
  return Math.PI - (bearingDeg * Math.PI) / 180;
}

// ------------------------------------------------------------------ render --

export function StructureModel({
  geometry,
  view = 'ISO',
  forceFallback = false,
  route = null,
}: {
  geometry: GeometryView | null;
  view?: ViewAngle;
  forceFallback?: boolean;
  route?: RouteOverlay | null;
}) {
  const mount = useRef<HTMLDivElement | null>(null);
  const [fallback, setFallback] = useState(false);
  /** Redraws the route group without tearing the scene down. Set once the
      scene exists, for the same reason `aim` below is: a highlight changing
      under the cursor must not replay the storey-by-storey build-up. */
  const applyRoute = useRef<((overlay: RouteOverlay | null) => void) | null>(null);
  /** Where the camera is heading. Written by the pointer and by the view
      buttons; read by the frame loop, which eases the camera toward it. */
  const wanted = useRef<Orbit | null>(null);
  /** The framing the current named view chose, for double-click to return to. */
  const home = useRef<Orbit | null>(null);
  /** Set once the scene exists, so a view change can move the camera without
      tearing the scene down and replaying the storey-by-storey build-up. */
  const aim = useRef<((view: ViewAngle, animate: boolean) => void) | null>(null);

  // A named view moves the camera; it no longer rebuilds the model. Separate
  // from the build effect below, which deliberately does not depend on `view`.
  useEffect(() => {
    aim.current?.(view, true);
  }, [view]);

  // The route arriving is an event, not a camera move: it means a package was
  // composed. It still does not rebuild the scene, because a hover over the
  // leg list would then rebuild it forty times a second.
  useEffect(() => {
    applyRoute.current?.(route);
  }, [route]);

  useEffect(() => {
    if (!geometry || forceFallback) return;
    if (!webglAvailable()) {
      setFallback(true);
      return;
    }

    let disposed = false;
    let cleanup: (() => void) | undefined;
    let raf: number | null = null;

    import('three-r128')
      .then((THREE: typeof ThreeNS) => {
        if (disposed || !mount.current) return;
        const node = mount.current;
        const width = node.clientWidth || 480;
        const height = node.clientHeight || 300;

        const scene = new THREE.Scene();
        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        renderer.setSize(width, height);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        // Colour management is left at r128's default on purpose: the encoding
        // properties are exactly the surface this version and the modern one
        // disagree about, and the shared type declaration cannot describe both.
        // Materials below are authored for the default, so nothing is lost.
        node.appendChild(renderer.domElement);

        const spec = geometry.spec;
        // The centroid the footprint was shifted by, kept: route waypoints are
        // in the *unshifted* footprint frame, and drawing them without the same
        // offset would put the route beside the building rather than through it.
        const { edges, cx: footprintCx, cz: footprintCz } = edgesOf(spec.footprint);
        const bearings = faceBearings(spec.footprint);

        /** The wall a labelled face refers to, by nearest outward normal. */
        const edgeForFace = (label: string): Edge | undefined => {
          const want = bearings[label];
          if (want === undefined) return undefined;
          let closest: { edge: Edge; off: number } | null = null;
          for (const edge of edges) {
            // `edge.angle` is atan2(nx, nz) in scene space; convert back to a
            // compass bearing so the two conventions can be compared.
            const bearing = ((Math.atan2(edge.nx, -edge.nz) * 180) / Math.PI + 360) % 360;
            const off = Math.abs(((bearing - want + 180) % 360) - 180);
            if (!closest || off < closest.off) closest = { edge, off };
          }
          return closest?.edge;
        };
        const totalHeight = spec.levels.reduce((sum, l) => sum + l.height_m, 0);
        const span = Math.max(
          ...edges.map((e) => Math.max(Math.abs(e.ax), Math.abs(e.bx), Math.abs(e.az), Math.abs(e.bz))),
          totalHeight,
        );

        // Orthographic: an elevation is a measured drawing, and perspective
        // would make two storeys of the same height read as different.
        const frustum = span * 2.6;
        const aspect = width / height;
        const camera = new THREE.OrthographicCamera(
          (-frustum * aspect) / 2,
          (frustum * aspect) / 2,
          frustum / 2,
          -frustum / 2,
          0.1,
          2000,
        );
        // Orbit state. A named view sets it; the pointer changes it; the
        // frame loop eases the camera toward it.
        const target = { x: 0, y: totalHeight * 0.42, z: 0 };
        const radius = span * CAMERA_STANDOFF;

        const framingFor = (angle: ViewAngle): Orbit =>
          angle === 'ISO'
            ? { theta: Math.PI * 0.75, phi: 0.95, zoom: 1, target }
            : {
                theta: azimuthForBearing(bearings[angle] ?? 0),
                // Slightly off level, so an elevation still reads as a solid.
                phi: MAX_PHI - 0.16,
                zoom: 1,
                target,
              };

        const current: Orbit = { ...framingFor(view) };
        wanted.current = { ...current };
        home.current = { ...current };

        aim.current = (angle: ViewAngle, animate: boolean) => {
          const next = framingFor(angle);
          home.current = { ...next };
          wanted.current = next;
          if (!animate) {
            current.theta = next.theta;
            current.phi = next.phi;
            current.zoom = next.zoom;
          }
        };

        const applyCamera = () => {
          const sinPhi = Math.sin(current.phi);
          camera.position.set(
            current.target.x + radius * sinPhi * Math.sin(current.theta),
            current.target.y + radius * Math.cos(current.phi),
            current.target.z + radius * sinPhi * Math.cos(current.theta),
          );
          camera.zoom = current.zoom;
          camera.updateProjectionMatrix();
          camera.lookAt(current.target.x, current.target.y, current.target.z);
        };
        applyCamera();

        scene.add(new THREE.AmbientLight(0xffffff, 0.72));
        const key = new THREE.DirectionalLight(0xffffff, 0.85);
        key.position.set(30, 46, 24);
        scene.add(key);
        const fill = new THREE.DirectionalLight(0x9fb4cc, 0.35);
        fill.position.set(-28, 16, -20);
        scene.add(fill);

        const building = new THREE.Group();
        scene.add(building);

        // ---- the computed route, when there is one -------------------------
        //
        // Its own group, added to the scene rather than to `building`, because
        // the building animates storey by storey on first draw and a route
        // scaled up out of the floor would read as the route being built.
        //
        // Drawn with `depthTest: false` so a leg inside the structure stays
        // visible through the mass. That is an overlay convention, not a claim
        // about seeing through walls, and the caption says so -- a route hidden
        // by the very building it goes into would be a drawing of nothing.
        const routeGroup = new THREE.Group();
        routeGroup.renderOrder = 999;
        scene.add(routeGroup);
        const routeRadius = Math.max(0.12, span * 0.014);

        // Read once, here, rather than at every use: this is the same
        // preference the storey build-up below obeys, and one reading of it
        // keeps the two from disagreeing halfway down a single frame.
        const reduced = prefersReducedMotion();

        /** One leg, as a solid the eye can follow. Lines are 1px on most GPUs. */
        const buildLeg = (
          index: number,
          from: ThreeNS.Vector3,
          to: ThreeNS.Vector3,
          material: ThreeNS.Material,
        ): DrawnLeg | null => {
          const direction = new THREE.Vector3().subVectors(to, from);
          const length = direction.length();
          // A degenerate leg -- two nodes at one point -- is skipped rather than
          // drawn as a zero-height cylinder, which renders as a disc.
          if (length < 1e-3) return null;
          const mesh = new THREE.Mesh(
            new THREE.CylinderGeometry(routeRadius, routeRadius, length, 8, 1, true),
            material,
          );
          mesh.quaternion.setFromUnitVectors(
            new THREE.Vector3(0, 1, 0),
            direction.clone().normalize(),
          );
          mesh.renderOrder = 999;
          // Placed by `growLeg` on the first painted frame, never here: a leg
          // that existed at full length for one frame before the schedule got
          // to it would flash the whole route and then take it away again.
          mesh.visible = false;
          routeGroup.add(mesh);
          return {
            mesh,
            index,
            from: from.clone(),
            direction: direction.clone().normalize(),
            length,
          };
        };

        /** Extend one leg to `extent` of its own length, from its own start. */
        const growLeg = (leg: DrawnLeg, extent: number) => {
          const shown = clamp01(extent);
          leg.mesh.visible = shown > 0;
          // A cylinder is centred on its origin, so a scaled one has to slide
          // forward by half of what it grew to keep its tail on the waypoint
          // it left. Without this the leg opens outward from its midpoint.
          leg.mesh.scale.y = Math.max(1e-3, shown);
          leg.mesh.position
            .copy(leg.from)
            .addScaledVector(leg.direction, (leg.length * shown) / 2);
        };

        /** Bring a waypoint up as the leg that reaches it arrives. */
        const landMarker = (marker: DrawnMarker, extent: number) => {
          const eased = easeOut(clamp01(extent));
          marker.mesh.visible = eased > 0.02;
          marker.mesh.scale.setScalar(Math.max(1e-3, eased));
        };

        const routeMaterial = (color: number) =>
          new THREE.MeshBasicMaterial({
            color,
            depthTest: false,
            transparent: true,
            opacity: 0.95,
          });

        /** The meshes the schedule below moves, rebuilt whenever a route lands. */
        let drawnRoutes: DrawnRoute[] = [];
        let routeSchedule = routeDrawSchedule(null);
        /** Which route is being drawn, and when its walk started. */
        let routeKey = '';
        let routeStartedAt = performance.now();

        /**
         * The route as the schedule says it stands at `elapsed`.
         *
         * Every frame, because the route has to grow while the camera is still
         * orbiting under the officer's finger -- a draw that only advanced on
         * a React render would stutter against the pointer.
         */
        const paintRoute = (elapsed: number) => {
          if (drawnRoutes.length === 0) return;
          const state = routeDrawState(routeSchedule, elapsed);
          for (const drawn of drawnRoutes) {
            const phase = drawn.kind === 'entry' ? state.entry : state.egress;
            const legs = phase.begun ? phase.legs : 0;
            // Keyed on the leg's place in the plan, never on its place in this
            // array: a degenerate leg is skipped as a mesh and still spends its
            // beat, so indexing by position would slide every later leg forward
            // and land the waypoints ahead of the walk.
            drawn.legs.forEach((leg) => growLeg(leg, legs - leg.index));
            drawn.markers.forEach((marker) => {
              // The first waypoint is where the crew already is, so it is there
              // as soon as its route begins. Every other one comes up over the
              // last third of the leg that reaches it, which is what makes the
              // sphere read as being arrived at rather than as being announced.
              const extent =
                marker.at === 0
                  ? 1
                  : (legs - marker.at + 1 - (1 - MARKER_LAND_FRACTION)) / MARKER_LAND_FRACTION;
              landMarker(marker, phase.begun ? extent : 0);
            });
          }
        };

        applyRoute.current = (overlay: RouteOverlay | null) => {
          // Dispose before clearing: the scene-wide traverse in `cleanup` only
          // ever sees the *last* route, so a replaced one leaks without this.
          routeGroup.children.forEach((child) => {
            const mesh = child as ThreeNS.Mesh;
            mesh.geometry?.dispose();
            const material = mesh.material as ThreeNS.Material | undefined;
            material?.dispose();
          });
          routeGroup.clear();
          drawnRoutes = [];
          if (!overlay) {
            routeSchedule = routeDrawSchedule(null);
            routeKey = '';
            return;
          }

          routeSchedule = routeDrawSchedule(overlay, { reducedMotion: reduced });
          // A *different* route restarts the walk; the same route redrawn --
          // which is what a hover over the leg list is -- keeps its clock, so
          // the highlight changes under a draw already in progress instead of
          // sending the crew back to the kerb.
          if (overlay.drawKey !== routeKey) {
            routeKey = overlay.drawKey;
            routeStartedAt = performance.now();
          }

          for (const kind of ['entry', 'egress'] as const) {
            const drawn = overlay[kind];
            if (!drawn || drawn.waypoints.length === 0) continue;
            const points = drawn.waypoints.map(
              (waypoint) =>
                new THREE.Vector3(
                  waypoint.x_m - footprintCx,
                  waypoint.z_m,
                  waypoint.y_m - footprintCz,
                ),
            );
            const plain = routeMaterial(ROUTE_COLORS[kind]);
            const lit = routeMaterial(ROUTE_COLORS.highlight);
            const highlighted =
              overlay.highlight?.route === kind ? overlay.highlight.leg : -1;
            const legs: DrawnLeg[] = [];
            for (let index = 0; index + 1 < points.length; index += 1) {
              const leg = buildLeg(
                index,
                points[index]!,
                points[index + 1]!,
                index === highlighted ? lit : plain,
              );
              // A degenerate leg contributes no mesh, and the schedule spends
              // its beat regardless: the pace is derived from what the plan
              // contains, not from what happened to be drawable. The leg
              // carries its own plan index so the omission costs no ordering.
              if (leg) legs.push(leg);
            }
            // A node the route passes through, marked. The door is larger
            // because "which wall do we go in" is the first question asked of
            // this drawing, and it should be findable without reading a label.
            const markers: DrawnMarker[] = drawn.waypoints.map((waypoint, index) => {
              const mesh = new THREE.Mesh(
                new THREE.SphereGeometry(
                  routeRadius * (waypoint.kind === 'door' ? 3.1 : 1.9),
                  10,
                  8,
                ),
                plain,
              );
              mesh.position.copy(points[index]!);
              mesh.renderOrder = 999;
              mesh.visible = false;
              routeGroup.add(mesh);
              return { mesh, at: index };
            });
            drawnRoutes.push({ kind, legs, markers });
          }
          // Painted once here as well as in the frame loop, so a route that
          // arrives between frames is never on screen at full length first.
          paintRoute(reduced ? Number.MAX_SAFE_INTEGER : performance.now() - routeStartedAt);
        };
        applyRoute.current(route);

        // ---- the footprint, as a reusable shape -------------------------
        const shape = new THREE.Shape();
        edges.forEach((edge, index) => {
          if (index === 0) shape.moveTo(edge.ax, edge.az);
          shape.lineTo(edge.bx, edge.bz);
        });
        shape.closePath();

        const storeys: { group: ThreeNS.Group; base: number; height: number }[] = [];
        const thermalMaterials: ThreeNS.Material[] = [];

        // The address side, resolved through the same labelling the backend
        // uses rather than worked out again here -- two rules for one fact is
        // how the door ends up on a different wall from the Alpha reading.
        const alphaEdge = edgeForFace('ALPHA');

        let base = 0;
        spec.levels.forEach((level, storeyIndex) => {
          const group = new THREE.Group();
          const disputed = level.status === 'DISPUTED';

          // ---- the storey's mass --------------------------------------
          const solid = new THREE.ExtrudeGeometry(shape, {
            depth: level.height_m,
            bevelEnabled: false,
          });
          // Extrude builds along +Z; stand it up so depth becomes height.
          solid.rotateX(-Math.PI / 2);
          const wall = new THREE.Mesh(
            solid,
            new THREE.MeshStandardMaterial({
              color: disputed ? COLORS.wallDisputed : COLORS.wall,
              roughness: 0.85,
              metalness: 0.02,
              transparent: disputed,
              opacity: disputed ? 0.55 : 1,
            }),
          );
          group.add(wall);

          // A disputed storey keeps its outline drawn, so the disagreement is
          // legible as a shape and not only as a tint.
          if (disputed) {
            group.add(
              new THREE.LineSegments(
                new THREE.EdgesGeometry(solid),
                new THREE.LineBasicMaterial({ color: COLORS.wallDisputed }),
              ),
            );
          }

          // ---- the floor band ------------------------------------------
          const bandShape = new THREE.ExtrudeGeometry(shape, { depth: 0.22, bevelEnabled: false });
          bandShape.rotateX(-Math.PI / 2);
          const band = new THREE.Mesh(
            bandShape,
            new THREE.MeshStandardMaterial({ color: COLORS.band, roughness: 0.7 }),
          );
          band.scale.set(1.035, 1, 1.035);
          band.position.y = level.height_m - 0.22;
          group.add(band);

          // ---- openings -------------------------------------------------
          const ground = storeyIndex === 0;
          edges.forEach((edge) => {
            if (edge.length < MIN_WINDOWED_WALL) return;
            const midX = (edge.ax + edge.bx) / 2;
            const midZ = (edge.az + edge.bz) / 2;
            const ux = (edge.bx - edge.ax) / edge.length;
            const uz = (edge.bz - edge.az) / edge.length;

            const place = (
              mesh: ThreeNS.Mesh,
              offset: number,
              y: number,
              proud: number,
            ) => {
              mesh.position.set(
                midX + ux * offset + edge.nx * proud,
                y,
                midZ + uz * offset + edge.nz * proud,
              );
              mesh.rotation.y = edge.angle;
              group.add(mesh);
            };

            // Ground floor of the address side gets a doorway; the rest of the
            // ground floor gets taller shopfront glazing, which is what these
            // streets actually look like.
            const isAddressSide = ground && alphaEdge !== undefined && edge === alphaEdge;
            const winH = ground ? Math.min(2.1, level.height_m - 0.9) : WINDOW_H;
            const sill = ground ? 0.55 : SILL_H;
            const offsets = openingOffsets(edge.length, WINDOW_PITCH, WINDOW_W);

            offsets.forEach((offset, n) => {
              const middle = Math.floor(offsets.length / 2);
              if (isAddressSide && n === middle) {
                const door = new THREE.Mesh(
                  new THREE.PlaneGeometry(DOOR_W, DOOR_H),
                  new THREE.MeshStandardMaterial({
                    color: COLORS.door,
                    roughness: 0.6,
                    side: THREE.DoubleSide,
                  }),
                );
                place(door, offset, DOOR_H / 2, PROUD);
                const surround = new THREE.Mesh(
                  new THREE.PlaneGeometry(DOOR_W + 0.28, DOOR_H + 0.18),
                  new THREE.MeshStandardMaterial({
                    color: COLORS.frame,
                    roughness: 0.7,
                    side: THREE.DoubleSide,
                  }),
                );
                place(surround, offset, (DOOR_H + 0.18) / 2, PROUD * 0.5);
                return;
              }
              const glass = new THREE.Mesh(
                new THREE.PlaneGeometry(WINDOW_W, winH),
                new THREE.MeshStandardMaterial({
                  color: COLORS.glass,
                  roughness: 0.25,
                  metalness: 0.55,
                  side: THREE.DoubleSide,
                }),
              );
              place(glass, offset, sill + winH / 2, PROUD);
              const frame = new THREE.Mesh(
                new THREE.PlaneGeometry(WINDOW_W + 0.18, winH + 0.18),
                new THREE.MeshStandardMaterial({
                  color: COLORS.frame,
                  roughness: 0.8,
                  side: THREE.DoubleSide,
                }),
              );
              place(frame, offset, sill + winH / 2, PROUD * 0.5);
            });
          });

          group.position.y = base;
          building.add(group);
          storeys.push({ group, base, height: level.height_m });
          base += level.height_m;
        });

        // ---- roof ---------------------------------------------------------
        const roofGroup = new THREE.Group();
        roofGroup.position.y = totalHeight;
        const pitched = spec.roof_segments.length > 0 && spec.roof_segments[0]!.pitch_deg > 5;
        const bounds = {
          minX: Math.min(...edges.map((e) => e.ax)),
          maxX: Math.max(...edges.map((e) => e.ax)),
          minZ: Math.min(...edges.map((e) => e.az)),
          maxZ: Math.max(...edges.map((e) => e.az)),
        };
        let roofRise = 0;

        const deck = new THREE.ExtrudeGeometry(shape, { depth: 0.25, bevelEnabled: false });
        deck.rotateX(-Math.PI / 2);
        roofGroup.add(
          new THREE.Mesh(deck, new THREE.MeshStandardMaterial({ color: COLORS.roof, roughness: 0.9 })),
        );

        if (pitched) {
          const built = roofGeometry(
            THREE,
            bounds,
            spec.roof_segments[0]!.pitch_deg,
            spec.roof_segments.length,
          );
          roofRise = built.rise;
          const roof = new THREE.Mesh(
            built.geometry,
            new THREE.MeshStandardMaterial({
              color: COLORS.roof,
              roughness: 0.92,
              side: THREE.DoubleSide,
              flatShading: true,
            }),
          );
          roof.position.y = 0.25;
          roofGroup.add(roof);
          // The eaves line, picked out so the roof reads as a separate element
          // from the wall it sits on rather than as more of the same grey.
          roofGroup.add(
            new THREE.LineSegments(
              new THREE.EdgesGeometry(built.geometry, 25),
              new THREE.LineBasicMaterial({ color: COLORS.parapet }),
            ).translateY(0.25),
          );
        } else {
          // Parapet: a low wall round the deck, which is what a flat roof has
          // and what makes a flat-roofed model stop reading as a cut-off box.
          //
          // Built as a rim, not as two nested solids: an outer box with a
          // smaller box inside puts a full-height block in the middle of the
          // roof. The rim stands alone and the surface is laid inside it.
          const parapet = new THREE.ExtrudeGeometry(shape, { depth: PARAPET_H, bevelEnabled: false });
          parapet.rotateX(-Math.PI / 2);
          const rim = new THREE.Mesh(
            parapet,
            new THREE.MeshStandardMaterial({ color: COLORS.parapet, roughness: 0.85 }),
          );
          rim.scale.set(1.02, 1, 1.02);
          rim.position.y = 0.24;
          roofGroup.add(rim);

          const surface = new THREE.ExtrudeGeometry(shape, { depth: 0.08, bevelEnabled: false });
          surface.rotateX(-Math.PI / 2);
          const surfaceMesh = new THREE.Mesh(
            surface,
            new THREE.MeshStandardMaterial({ color: COLORS.roof, roughness: 0.95 }),
          );
          surfaceMesh.scale.set(0.955, 1, 0.955);
          surfaceMesh.position.y = 0.24 + PARAPET_H - 0.22;
          roofGroup.add(surfaceMesh);
        }

        // ---- roof obstructions, where the records put them ----------------
        spec.obstructions.forEach((obstruction, index) => {
          const solar = /solar|pv|array/i.test(obstruction.type);
          const size = solar ? [3.4, 0.18, 2.2] : [1.5, 1.1, 1.5];
          const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(size[0]!, size[1]!, size[2]!),
            new THREE.MeshStandardMaterial({
              color: solar ? COLORS.solar : COLORS.obstruction,
              roughness: 0.5,
              transparent: obstruction.status === 'DISPUTED',
              opacity: obstruction.status === 'DISPUTED' ? 0.6 : 1,
            }),
          );
          // Spread them across the roof rather than stacking at the centre.
          const theta = (index / Math.max(1, spec.obstructions.length)) * Math.PI * 2;
          if (pitched) {
            // On a slope, lying along it: a panel floating above a pitched roof
            // is the giveaway that the roof is decoration.
            const side = index % 2 === 0 ? 1 : -1;
            const t = 0.42;
            mesh.position.set(
              side * (bounds.maxX - bounds.minX) * 0.5 * t,
              0.3 + roofRise * (1 - t) + size[1]! / 2,
              Math.sin(theta) * (bounds.maxZ - bounds.minZ) * 0.22,
            );
            mesh.rotation.z = side * -Math.atan2(roofRise, (bounds.maxX - bounds.minX) / 2);
          } else {
            const ring = span * 0.34;
            mesh.position.set(
              Math.cos(theta) * ring,
              0.24 + PARAPET_H - 0.14 + size[1]! / 2,
              Math.sin(theta) * ring,
            );
          }
          roofGroup.add(mesh);
        });
        building.add(roofGroup);

        // ---- the heat map -------------------------------------------------
        //
        // Cells are in face-local (u, v): u across the wall, v up it. Drawn as
        // quads a little proud of the wall so the temperature reads as being
        // *on* the surface, which is exactly what a thermal camera measures.
        spec.faces.forEach((face) => {
          const cells = face.thermal_cells ?? [];
          if (cells.length === 0) return;
          const edge = edgeForFace(face.label);
          if (!edge) return;
          const ux = (edge.bx - edge.ax) / edge.length;
          const uz = (edge.bz - edge.az) / edge.length;
          const midX = (edge.ax + edge.bx) / 2;
          const midZ = (edge.az + edge.bz) / 2;

          cells.forEach((cell) => {
            const w = Math.max(0.2, (cell.u_to - cell.u_from) * edge.length);
            const h = Math.max(0.2, (cell.v_to - cell.v_from) * totalHeight);
            const material = new THREE.MeshBasicMaterial({
              color: thermalColor(cell.temperature_c),
              transparent: true,
              opacity: 0,
              side: THREE.DoubleSide,
              depthWrite: false,
            });
            thermalMaterials.push(material);
            const quad = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
            const offset = (cell.u_from + cell.u_to) / 2 - 0.5;
            quad.position.set(
              midX + ux * offset * edge.length + edge.nx * (PROUD * 3),
              ((cell.v_from + cell.v_to) / 2) * totalHeight,
              midZ + uz * offset * edge.length + edge.nz * (PROUD * 3),
            );
            quad.rotation.y = edge.angle;
            building.add(quad);
          });
        });

        // ---- the collapse zone, on the ground ------------------------------
        const zone = new THREE.Mesh(
          new THREE.RingGeometry(
            spec.collapse_zone_radius_m - 0.35,
            spec.collapse_zone_radius_m,
            72,
          ),
          new THREE.MeshBasicMaterial({
            color: COLORS.collapse,
            transparent: true,
            opacity: 0.5,
            side: THREE.DoubleSide,
          }),
        );
        zone.rotation.x = -Math.PI / 2;
        zone.position.y = 0.02;
        scene.add(zone);


        // ---- pointer: orbit, pan, zoom -------------------------------------
        const canvas = renderer.domElement;
        canvas.style.touchAction = 'none';
        canvas.style.cursor = 'grab';

        const pointers = new Map<number, { x: number; y: number }>();
        let panning = false;
        let pinch = 0;

        const clampPhi = (value: number) => Math.min(MAX_PHI, Math.max(MIN_PHI, value));
        const clampZoom = (value: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));

        const onPointerDown = (event: PointerEvent) => {
          canvas.setPointerCapture(event.pointerId);
          pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
          // Shift-drag, middle button or right button pans; anything else orbits.
          panning = event.shiftKey || event.button === 1 || event.button === 2;
          canvas.style.cursor = panning ? 'move' : 'grabbing';
        };

        const onPointerMove = (event: PointerEvent) => {
          const previous = pointers.get(event.pointerId);
          if (!previous) return;
          const dx = event.clientX - previous.x;
          const dy = event.clientY - previous.y;
          pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
          const next = wanted.current;
          if (!next) return;

          if (pointers.size >= 2) {
            // Two fingers: pinch to zoom, drag to pan. Tablets are the device
            // this console is actually used on, so touch is not an afterthought.
            const [a, b] = Array.from(pointers.values());
            if (a && b) {
              const distance = Math.hypot(a.x - b.x, a.y - b.y);
              if (pinch > 0) next.zoom = clampZoom(next.zoom * (distance / pinch));
              pinch = distance;
            }
            return;
          }

          if (panning) {
            // Pan across the camera's own plane, scaled by zoom so the model
            // keeps pace with the pointer at every magnification.
            //
            // The basis is derived rather than approximated. With the camera at
            // spherical (theta, phi) looking at the target, the world-space
            // right vector is (cos t, 0, -sin t) and the camera's up vector is
            // (-cos p sin t, sin p, -cos p cos t). An eyeballed version drifts
            // the model sideways as soon as the view is tipped.
            const scale = (span * 2) / (canvas.clientHeight * next.zoom);
            const sinT = Math.sin(next.theta);
            const cosT = Math.cos(next.theta);
            const sinP = Math.sin(next.phi);
            const cosP = Math.cos(next.phi);
            const right = { x: cosT, y: 0, z: -sinT };
            const up = { x: -cosP * sinT, y: sinP, z: -cosP * cosT };
            next.target.x += (-dx * right.x + dy * up.x) * scale;
            next.target.y += dy * up.y * scale;
            next.target.z += (-dx * right.z + dy * up.z) * scale;
            return;
          }

          next.theta -= (dx / canvas.clientWidth) * Math.PI * 2;
          // Minus, to match every other 3D viewer: dragging *down* tips the
          // camera up and shows the roof. The other way round felt inverted
          // against `OrbitControls`, which is the muscle memory anyone opening
          // this already has.
          next.phi = clampPhi(next.phi - (dy / canvas.clientHeight) * Math.PI);
        };

        const endPointer = (event: PointerEvent) => {
          pointers.delete(event.pointerId);
          if (pointers.size < 2) pinch = 0;
          if (pointers.size === 0) {
            panning = false;
            canvas.style.cursor = 'grab';
          }
        };

        const onWheel = (event: WheelEvent) => {
          event.preventDefault();
          const next = wanted.current;
          if (!next) return;
          next.zoom = clampZoom(next.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12));
        };

        // Back to the framing the named view chose, without hunting for it.
        const onDoubleClick = () => {
          if (home.current) {
            wanted.current = {
              theta: home.current.theta,
              phi: home.current.phi,
              zoom: home.current.zoom,
              target: { ...home.current.target },
            };
          }
        };

        // The right button pans, so its menu would fire on every pan.
        const onContextMenu = (event: Event) => event.preventDefault();

        canvas.addEventListener('pointerdown', onPointerDown);
        canvas.addEventListener('pointermove', onPointerMove);
        canvas.addEventListener('pointerup', endPointer);
        canvas.addEventListener('pointercancel', endPointer);
        canvas.addEventListener('wheel', onWheel, { passive: false });
        canvas.addEventListener('dblclick', onDoubleClick);
        canvas.addEventListener('contextmenu', onContextMenu);

        // ---- the build-up --------------------------------------------------
        // `reduced` is read once, up with the route group, and covers both this
        // and the route's walk.
        const startedAt = performance.now();
        const massDone = storeys.length * LEVEL_RISE_MS;

        const draw = () => {
          const elapsed = reduced ? Number.MAX_SAFE_INTEGER : performance.now() - startedAt;

          storeys.forEach((storey, index) => {
            const t = clamp01((elapsed - index * LEVEL_RISE_MS) / LEVEL_RISE_MS);
            const eased = easeOut(t);
            storey.group.visible = t > 0;
            storey.group.scale.y = Math.max(0.001, eased);
            storey.group.position.y = storey.base;
          });
          const built = clamp01((elapsed - (massDone - LEVEL_RISE_MS)) / LEVEL_RISE_MS);
          roofGroup.visible = built > 0.6;

          const fade = clamp01((elapsed - massDone - THERMAL_DELAY_MS) / THERMAL_FADE_MS);
          thermalMaterials.forEach((material) => {
            (material as ThreeNS.MeshBasicMaterial).opacity = fade * THERMAL_OPACITY;
          });

          // The route walks on its own clock, started when the route arrived
          // rather than when the scene was built: a package composed four
          // minutes into an incident must draw from its own first frame, not
          // arrive already finished because the storeys went up long ago.
          paintRoute(reduced ? Number.MAX_SAFE_INTEGER : performance.now() - routeStartedAt);

          // Ease toward where the pointer (or a named view) put the camera.
          const next = wanted.current;
          if (next) {
            // Take the shorter way round, so crossing north does not spin the
            // model most of a turn to arrive somewhere adjacent.
            let delta = next.theta - current.theta;
            while (delta > Math.PI) delta -= Math.PI * 2;
            while (delta < -Math.PI) delta += Math.PI * 2;
            current.theta += delta * DAMPING;
            current.phi += (next.phi - current.phi) * DAMPING;
            current.zoom += (next.zoom - current.zoom) * DAMPING;
            current.target.x += (next.target.x - current.target.x) * DAMPING;
            current.target.y += (next.target.y - current.target.y) * DAMPING;
            current.target.z += (next.target.z - current.target.z) * DAMPING;
          }
          applyCamera();

          renderer.render(scene, camera);
          raf = requestAnimationFrame(draw);
        };
        raf = requestAnimationFrame(draw);

        const onResize = () => {
          const w = node.clientWidth || width;
          const h = node.clientHeight || height;
          const a = w / h;
          camera.left = (-frustum * a) / 2;
          camera.right = (frustum * a) / 2;
          camera.updateProjectionMatrix();
          renderer.setSize(w, h);
        };
        window.addEventListener('resize', onResize);

        cleanup = () => {
          aim.current = null;
          applyRoute.current = null;
          canvas.removeEventListener('pointerdown', onPointerDown);
          canvas.removeEventListener('pointermove', onPointerMove);
          canvas.removeEventListener('pointerup', endPointer);
          canvas.removeEventListener('pointercancel', endPointer);
          canvas.removeEventListener('wheel', onWheel);
          canvas.removeEventListener('dblclick', onDoubleClick);
          canvas.removeEventListener('contextmenu', onContextMenu);
          window.removeEventListener('resize', onResize);
          if (raf !== null) cancelAnimationFrame(raf);
          scene.traverse((object: ThreeNS.Object3D) => {
            const mesh = object as ThreeNS.Mesh;
            if (mesh.geometry) mesh.geometry.dispose();
            const material = mesh.material as ThreeNS.Material | ThreeNS.Material[] | undefined;
            if (Array.isArray(material)) material.forEach((m) => m.dispose());
            else if (material) material.dispose();
          });
          renderer.dispose();
          if (renderer.domElement.parentElement === node) {
            node.removeChild(renderer.domElement);
          }
        };
      })
      .catch(() => {
        if (!disposed) setFallback(true);
      });

    return () => {
      disposed = true;
      if (raf !== null) cancelAnimationFrame(raf);
      cleanup?.();
    };
    // `view` is deliberately absent: it moves the camera through `aim`, and
    // rebuilding the scene for it would replay the build-up on every click.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [geometry, forceFallback]);

  if (!geometry) {
    return (
      <div
        className="border border-dashed border-line p-6 text-muted"
        role="img"
        aria-label="No geometry on record for this structure"
      >
        <p className="text-ink">No geometry on record</p>
        <p className="mt-1 text-micro leading-5">
          Nothing has measured this structure yet. This is an absence of
          measurement, not a measurement of an unremarkable building.
        </p>
      </div>
    );
  }

  const showFallback = forceFallback || fallback;

  return (
    <figure className="m-0">
      {showFallback ? (
        <div
          className="border border-line bg-surface p-2 [&_svg]:h-auto [&_svg]:w-full [&_svg_rect]:stroke-ink [&_svg_text]:fill-muted"
          role="img"
          aria-label={describeGeometry(geometry, view)}
          data-testid="geometry-svg"
          dangerouslySetInnerHTML={{ __html: geometry.svg }}
        />
      ) : (
        <div
          ref={mount}
          data-testid="geometry-canvas"
          role="img"
          aria-label={describeGeometry(geometry, view)}
          className="h-[360px] w-full border border-line bg-ground"
        />
      )}
      <ThermalLegend faces={geometry.spec.faces} />
      <figcaption className="mt-2 space-y-1 text-micro text-muted">
        {!showFallback && (
          <span className="block text-ink">
            Drag to orbit · scroll to zoom · shift-drag to pan · double-click to
            reset. The named buttons above jump to a wall.
          </span>
        )}
        <span className="block">{describeGeometry(geometry, view)}</span>
        {/* The route in words, because the drawing is not the record. Under the
            static elevation it is the *only* account of it: the fallback is a
            pre-rendered SVG of the structure and nothing can be added to it, so
            saying nothing there would leave a route the console has and does
            not mention. Both branches also state the overlay convention -- legs
            are drawn through the mass, which is a drawing choice and not a
            claim that anything sees through a wall. */}
        {route && (route.entry || route.egress) && (
          <span className="block" data-testid="route-caption">
            {showFallback
              ? 'The computed route is not on the static elevation: it is drawn on the interactive model only. Every leg of it, with what it was weighed against, is listed in the entry package.'
              : `Route drawn over the mass: entry in blue${
                  route.egress ? ', second way out in green' : ''
                }. Legs are drawn through the structure so an interior leg stays visible; that is an overlay, not a sightline.`}
          </span>
        )}
        {showFallback && (
          <span className="block text-disputed">
            {forceFallback
              ? 'Static elevation. It marks the same disputed mass as the interactive view.'
              : 'WebGL is unavailable on this device. Showing the static elevation, which marks the same disputed mass.'}
          </span>
        )}
      </figcaption>
    </figure>
  );
}

/**
 * The heat map read as numbers, per face, ground up.
 *
 * The overlay on the model is colour; this is the same data as text, so the
 * reading survives colourblindness, a washed-out tablet in daylight, and the
 * static fallback. The two darkest ramp steps sit below 3:1 against the
 * surface, which makes these labels required rather than a nicety.
 *
 * A face with no cells contributes no row. UNSCANNED is stated in the caption
 * as absence, never rendered here as a temperature.
 */
export function ThermalLegend({ faces }: { faces: FaceView[] }) {
  const scanned = faces.filter((f) => f.thermal_cells?.length);
  if (scanned.length === 0) return null;

  return (
    <div className="mt-2 border border-line p-2">
      <p className="text-micro uppercase tracking-wide text-muted">
        Measured surface temperature, ground up
      </p>
      <ul className="mt-1 space-y-1">
        {scanned.map((face) => (
          <li key={face.label} className="flex items-center gap-2 text-micro">
            <span className="w-16 shrink-0 text-ink">{face.label}</span>
            <span className="flex flex-wrap gap-1">
              {face.thermal_cells.map((cell, index) => (
                <span
                  key={`${face.label}-${index}`}
                  className="border border-line px-1.5 py-0.5 tabular-nums text-ink"
                  style={{
                    backgroundColor: `#${thermalColor(cell.temperature_c)
                      .toString(16)
                      .padStart(6, '0')}`,
                  }}
                >
                  {Math.round(cell.temperature_c)} C
                </span>
              ))}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-1 text-micro text-muted">
        Thermal imaging measures exterior surface temperature and cannot see
        through walls. Faces not listed are UNSCANNED, not cool.
      </p>
    </div>
  );
}

/**
 * The text a screen reader hears, and the caption everyone else reads.
 *
 * Says the same things the picture does, including the disputed storey -- a
 * view that only sighted users can extract the conflict from is not a view of
 * the conflict. It also states what the fenestration is: drawn regularly
 * because no survey counted windows, so nobody reads an opening count off it.
 */
export function describeGeometry(geometry: GeometryView, view: ViewAngle): string {
  const spec = geometry.spec;
  const disputed = spec.levels.filter((l) => l.status === 'DISPUTED').length;
  const unscanned = spec.faces
    .filter((f) => f.thermal.kind === 'UNSCANNED')
    .map((f) => f.label);

  const parts = [
    `${faceLabelFor(view)}.`,
    `${spec.levels.length} level${spec.levels.length === 1 ? '' : 's'},`,
    `${geometry.total_height_m} m measured height.`,
  ];
  if (disputed > 0) {
    parts.push(
      `${disputed} level${disputed === 1 ? ' is' : 's are'} DISPUTED and drawn translucent with its outline picked out.`,
    );
  }
  if (spec.obstructions.length > 0) {
    parts.push(`${spec.obstructions.length} roof obstruction marked.`);
  }
  parts.push(`Collapse zone ${spec.collapse_zone_radius_m} m, the 1.5x measured-height convention.`);
  for (const face of spec.faces) {
    if (!face.thermal_cells?.length) continue;
    const readings = face.thermal_cells
      .map((c) => `${Math.round(c.temperature_c)} C`)
      .join(', ');
    parts.push(
      `${face.label} measured ground up: ${readings}. Surface temperature only.`,
    );
  }
  if (unscanned.length > 0) {
    parts.push(`Faces with no thermal coverage: ${unscanned.join(', ')} -- UNSCANNED, not cool.`);
  }
  parts.push(
    'Windows and doors are drawn as regular fenestration to make the elevation legible; no survey counted openings, so their number and position are indicative.',
  );
  return parts.join(' ');
}
