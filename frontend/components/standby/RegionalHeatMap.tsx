/**
 * Regional fire activity in three dimensions: what is burning around us.
 *
 * **The question this answers is "how far from us, and how big".** VIIRS pixels
 * are roughly 375 m across and the instrument is built for wildfire, so a
 * room-and-contents fire on Hayes Street never reaches the detection threshold.
 * The normal reading is an empty city inside a busy region, and that is not an
 * alarm and not a fault -- it is what drives mutual-aid demand, pulls strike
 * teams out of the city, puts smoke over the district and moves the red-flag
 * posture. So the map is regional on purpose, the city's own count travels
 * beside it, and the resolution note ships with every answer.
 *
 * **The ground is a mesh, not a plate.** A flat picture answers "where"; it does
 * not answer "which side of the ridge", and at a five-degree box that is most of
 * what terrain is for. Ridgelines are what wind follows, what a fire runs up,
 * and what a crew has to drive around. `TerrainLayer` builds the mesh from two
 * tiled grids -- public terrarium elevation and licensed satellite imagery --
 * both proxied through this system, so the browser talks to one origin and the
 * Maps key never leaves the server.
 *
 * **Vertical exaggeration is real and is declared.** Northern California's
 * relief is about half a percent of the region's width; drawn true to scale it
 * is a flat sheet. The mesh is exaggerated so the shape reads, the factor is a
 * constant, and the key prints it -- an unlabelled exaggeration is a lie about
 * how steep the country is.
 *
 * **What is drawn over it.**
 *
 * 1. A continuous heat field over the detections, weighted by fire radiative
 *    power. Continuous rather than binned columns because that is what thermal
 *    energy over a landscape looks like, and because a column at this scale
 *    obscures the ground it is standing on.
 * 2. The district, and range rings at 25, 50 and 100 km. This is the "how far"
 *    half of the question, and a ring an officer can read a distance off beats
 *    any amount of prose about it.
 * 3. The strongest clusters, ranked and numbered, each openable for what the
 *    instrument actually reported there.
 *
 * Everything above the mesh draws with `depthTest: false`. A heat field buried
 * inside a hillside is not a subtler rendering of the same fact, it is a fire
 * you cannot see.
 *
 * **A detection is not a fire.** VIIRS reports a pixel that ran hotter than its
 * neighbours during one satellite pass, and the panel says "detections"
 * throughout for that reason. Everything in the hotspot card is read off the
 * feed -- radiative power, brightness temperature, the confidence flag, the pass
 * time -- and nothing in it is modelled.
 *
 * **The district marker is not a detection.** It is a hollow ring in the live
 * blue, never the fire ramp, and it is labelled. The panel this replaces
 * refused to draw a city marker at all, on the grounds that a marker in an
 * empty city makes the map look alive when it is not -- that objection is about
 * inventing *activity*, and it stands. Drawing where the department is, in a
 * colour the data never uses, answers "how far from us" without asserting
 * anything about fire.
 *
 * **Nothing is inferred.** Every count, every box and every reading is read off
 * the response. Where the response carries none, the panel says so and draws
 * nothing.
 */

'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { FireActivity, FireBBox, FireDetection } from '@/components/standby/FireActivityMap';
import { gatewayPath } from '@/lib/api/client';
import type { RegionBasemapView } from '@/lib/api/types';

// ------------------------------------------------------------------ constants

/**
 * The console's thermal ramp, shared with the structure model's heat map.
 *
 * One hue, monotone lightness, validated against the `surface` token with the
 * dataviz palette checker: a sequential ramp is the right family for a
 * magnitude, and a rainbow would turn "how much energy" into a hue comparison
 * nobody can rank.
 *
 * It is the same ramp the building's face temperatures use, which is a real
 * risk worth naming: those are degrees and these are megawatts. They never
 * appear on screen together -- the structure model is the incident view, this
 * is standby -- and each carries its own key with its own units. Inventing a
 * second hue for the second quantity would have cost more than it bought.
 */
const THERMAL_RAMP: ReadonlyArray<readonly [number, number, number]> = [
  [138, 68, 16],
  [178, 90, 16],
  [217, 116, 16],
  [245, 160, 42],
  [255, 206, 104],
];

/**
 * The same ramp, as the heat field reads it.
 *
 * `HeatmapLayer` maps density across this range and fades the low end out
 * against `threshold`, so the darkest step is where the field begins rather
 * than a floor colour painted over quiet ground. Deliberately not extended with
 * a cool end: VIIRS does not report "cold", it reports nothing, and a blue
 * periphery would be data where there is none.
 */
const HEAT_RANGE: ReadonlyArray<readonly [number, number, number]> = THERMAL_RAMP;

/** The `live` token. Used for the district and its rings, and never for data. */
const LIVE_BLUE: readonly [number, number, number] = [56, 189, 248];

/**
 * How far the mesh is stretched vertically.
 *
 * Northern California runs from sea level to about 2,700 m inside this box,
 * across roughly 550 km of ground -- half a percent of the width. Drawn true to
 * scale the terrain is a flat sheet, and the whole reason for a mesh is gone.
 *
 * Eight is the smallest factor at which the Coast Ranges, the Central Valley and
 * the Sierra front read as three different things at a glance. **The key prints
 * it**, because an unlabelled exaggeration is a claim about how steep the
 * country is, and this one is eight times too steep.
 */
const VERTICAL_EXAGGERATION = 8;

/**
 * Terrarium's encoding, times the exaggeration.
 *
 * A terrarium pixel is `(r * 256 + g + b / 256) - 32768` metres. Scaling all
 * four terms together stretches height uniformly, which is what an exaggeration
 * has to be: scaling the scalers and forgetting the offset would raise sea level
 * by 32 km.
 */
const TERRARIUM_DECODER = {
  rScaler: 256 * VERTICAL_EXAGGERATION,
  gScaler: 1 * VERTICAL_EXAGGERATION,
  bScaler: (1 / 256) * VERTICAL_EXAGGERATION,
  offset: -32768 * VERTICAL_EXAGGERATION,
};

/**
 * Zooms the mesh is built at.
 *
 * **The floor is a cliff, not a preference.** `TileLayer` does not clamp *down*
 * against its `minZoom` the way it clamps up against its ceiling: below the
 * floor `getTileIndices` returns an empty set, so one notch under it the mesh
 * does not coarsen, it vanishes, and the panel is the black rectangle it was
 * before anything loaded. This used to be 5 against an opening camera near 6.5,
 * which put the cliff about two wheel notches from where the map opens -- close
 * enough that zooming out at all was how you found it. Three is a continental
 * view, the proxy serves from zero, and the camera is floored at the same
 * number below so it can never reach ground the mesh does not cover.
 *
 * The ceiling is the proxy's and is deeper than this camera goes -- past it the
 * squares are a street map, which is a different product and somebody else's
 * quota. It needs no matching camera limit: against the ceiling `TileLayer`
 * clamps and keeps drawing, so zooming in past it costs resolution rather than
 * the ground.
 */
const TERRAIN_MIN_ZOOM = 3;
const TERRAIN_MAX_ZOOM = 11;

/**
 * How far the heat field spreads from a detection, pixels.
 *
 * Screen-space, because that is how `HeatmapLayer` works: the field is a
 * gaussian over the projected points, so a bin does not change size as the
 * camera moves the way a ground-radius one would. Wide enough that two
 * detections a few kilometres apart read as one area of activity, which is what
 * they are.
 */
const HEAT_RADIUS_PX = 62;

/**
 * How many clusters get a number.
 *
 * Enough to rank what matters, few enough that the labels do not collide over a
 * busy week. Everything else is still in the field and still in the totals --
 * the numbering is a reading order, not a filter.
 */
const HOTSPOT_COUNT = 6;

/**
 * How close two detections have to be to count as one hotspot, kilometres.
 *
 * A VIIRS pass lays detections down in a line along the scan, so a single fire
 * arrives as a scatter rather than a point. 25 km gathers one fire's worth of
 * pixels without merging two valleys.
 */
const HOTSPOT_RADIUS_KM = 25;

/** Range rings, kilometres. Mutual aid, then the drive that costs a shift. */
const RING_KM: readonly number[] = [25, 50, 100];

/**
 * Zoom added after fitting, to pay for the tilt.
 *
 * `fitBounds` solves for a top-down camera; tilting one back widens the ground
 * it sees and shrinks the subject into the middle of the frame. Empirical
 * rather than derived -- the factor depends on the frame's aspect as well as
 * the pitch -- and checked against a rendered frame rather than reasoned about.
 */
const PITCH_ZOOM_COMPENSATION = 0.78;

/**
 * How far the camera is tilted back, degrees.
 *
 * Steep enough that the relief reads as relief. Past about 55 the far edge of
 * the region compresses into a band and the ridges stack.
 */
const CAMERA_PITCH = 50;

/** Degrees of latitude per kilometre. Constant enough at any latitude. */
const KM_PER_DEG_LAT = 110.574;

// ------------------------------------------------------------------ geometry

function ringPolygon(
  centre: readonly [number, number],
  km: number,
  steps = 128,
): [number, number][] {
  const [longitude, latitude] = centre;
  const degLat = km / KM_PER_DEG_LAT;
  // Longitude degrees shrink towards the poles, so a circle on the ground is an
  // ellipse in degrees. Using one radius for both axes draws a ring that is
  // right north-south and 20% wrong east-west at this latitude, which an
  // officer would read a distance off.
  const degLon = km / (111.32 * Math.cos((latitude * Math.PI) / 180));
  return Array.from({ length: steps + 1 }, (_, index) => {
    const angle = (index / steps) * Math.PI * 2;
    return [longitude + degLon * Math.cos(angle), latitude + degLat * Math.sin(angle)] as [
      number,
      number,
    ];
  });
}

function centreOf(box: FireBBox): [number, number] {
  return [(box.west + box.east) / 2, (box.south + box.north) / 2];
}

/**
 * A region's identity, by its corners rather than by its object.
 *
 * The fire-activity poll re-reads the whole payload every few minutes and hands
 * back a freshly parsed bounding box each time -- same four numbers, new object.
 * Anything that treats that as "the region changed" re-frames the camera, and
 * re-framing the camera throws away wherever the officer had just panned to.
 * Comparing the corners is the difference between a refetch and a move.
 */
export function regionKey(box: FireBBox): string {
  return `${box.west},${box.south},${box.east},${box.north}`;
}

/**
 * Whether the mesh has any tiles under a camera at this zoom.
 *
 * `TileLayer` rounds the camera's zoom to a tile row -- its tiles are 512 px,
 * which is the viewport's own scale -- and answers with nothing at all below its
 * floor. So this is the exact predicate for "is there ground under the heat
 * field", and it is what the camera's own floor exists to keep true.
 */
export function terrainCoversZoom(zoom: number): boolean {
  return Math.round(zoom) >= TERRAIN_MIN_ZOOM;
}

/**
 * The ground the mesh is allowed to ask for, as `TileLayer` wants it.
 *
 * The proxy serves one region and refuses every square outside it before it
 * contacts a provider -- that refusal is what stops the endpoint being an open
 * relay onto the department's metered quota. Cheap to answer, and not cheap to
 * *ask*: a camera tilted back 50 degrees sees ground well past the region on
 * three sides, and every doomed square is a real trip through the gateway
 * occupying one of the six connections the browser will open to this origin.
 *
 * The *region*, not the basemap's box. The basemap covers more ground than the
 * region because an integer zoom always does, and the extra is exactly the
 * ground the proxy will not serve.
 */
export function terrainExtent(box: FireBBox): [number, number, number, number] {
  return [box.west, box.south, box.east, box.north];
}

/** Great-circle distance in kilometres. Used for the "nearest detection" line. */
export function distanceKm(
  from: readonly [number, number],
  to: readonly [number, number],
): number {
  const [lon1, lat1] = from;
  const [lon2, lat2] = to;
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

/**
 * The closest detection to the district, and how far.
 *
 * `null` when there are no detections or no district to measure from -- never
 * a zero, which would read as "a fire at the station".
 */
export function nearestDetection(
  detections: readonly FireDetection[],
  centre: readonly [number, number] | null,
): { detection: FireDetection; km: number } | null {
  if (!centre || detections.length === 0) return null;
  let best: { detection: FireDetection; km: number } | null = null;
  for (const detection of detections) {
    const km = distanceKm(centre, [detection.longitude, detection.latitude]);
    if (best === null || km < best.km) best = { detection, km };
  }
  return best;
}

/**
 * One cluster of detections, ranked, with only what the feed actually said.
 *
 * Every field here is read or summed from the payload. There is no risk score,
 * no spread model and no "concern level": those would be a forecast, and this
 * system does not make one from a five-day detection table.
 */
export interface Hotspot {
  /** Rank by summed radiative power, 1 is the strongest. Not an id. */
  rank: number;
  longitude: number;
  latitude: number;
  detections: FireDetection[];
  /** Megawatts, summed. The closest thing the feed has to "how big". */
  totalFrp: number;
  /** The single hottest pixel's radiative power. */
  peakFrp: number;
  /** Hottest brightness temperature in the cluster, kelvin, or null. */
  peakBrightnessK: number | null;
  /** The most recent satellite pass that saw any of it. */
  lastSeen: string | null;
  /** How many pixels carried each confidence flag. */
  confidence: { high: number; nominal: number; low: number; unknown: number };
  /** Passes by daylight and by night. */
  daynight: { day: number; night: number; unknown: number };
  /** Kilometres from the district, or null with no district to measure from. */
  km: number | null;
}

function confidenceBucket(raw: string | number | null): keyof Hotspot['confidence'] {
  if (typeof raw === 'number') {
    // MODIS reports 0-100. The bands are the product's own.
    if (raw >= 80) return 'high';
    if (raw >= 30) return 'nominal';
    return 'low';
  }
  const value = (raw ?? '').toString().trim().toLowerCase();
  if (value === 'h' || value === 'high') return 'high';
  if (value === 'n' || value === 'nominal') return 'nominal';
  if (value === 'l' || value === 'low') return 'low';
  return 'unknown';
}

/**
 * Gather detections into ranked hotspots.
 *
 * Greedy and single-pass: strongest pixel first, each subsequent detection
 * joining the nearest existing cluster within `HOTSPOT_RADIUS_KM` or starting
 * its own. Not k-means, deliberately -- k-means needs a k nobody can justify and
 * moves cluster centres between renders, so a hotspot numbered 3 could become 4
 * because a pixel arrived on the far side of the region.
 *
 * The centre is **radiative-power weighted**, so a cluster's marker sits on the
 * energy rather than on the middle of the scatter's bounding box.
 */
export function hotspotsFrom(
  detections: readonly FireDetection[],
  centre: readonly [number, number] | null,
  { limit = HOTSPOT_COUNT, radiusKm = HOTSPOT_RADIUS_KM } = {},
): Hotspot[] {
  const ordered = [...detections].sort((a, b) => (b.frp ?? 0) - (a.frp ?? 0));
  const groups: FireDetection[][] = [];
  const seeds: [number, number][] = [];

  for (const detection of ordered) {
    const here: [number, number] = [detection.longitude, detection.latitude];
    let best = -1;
    let bestKm = Infinity;
    for (let index = 0; index < seeds.length; index += 1) {
      const seed = seeds[index];
      if (!seed) continue;
      const km = distanceKm(seed, here);
      if (km < bestKm) {
        bestKm = km;
        best = index;
      }
    }
    if (best >= 0 && bestKm <= radiusKm) {
      groups[best]?.push(detection);
    } else {
      groups.push([detection]);
      seeds.push(here);
    }
  }

  const summarised = groups.map((group) => {
    const weights = group.map((d) => d.frp ?? 0);
    const total = weights.reduce((sum, frp) => sum + frp, 0);
    // Falls back to an unweighted mean when nothing in the cluster reported a
    // power, rather than dividing by zero and putting the marker at the origin.
    const denominator = total > 0 ? total : group.length;
    const longitude =
      group.reduce((sum, d, i) => sum + d.longitude * (total > 0 ? (weights[i] ?? 0) : 1), 0) /
      denominator;
    const latitude =
      group.reduce((sum, d, i) => sum + d.latitude * (total > 0 ? (weights[i] ?? 0) : 1), 0) /
      denominator;

    const brightnesses = group
      .map((d) => d.brightness_k)
      .filter((k): k is number => typeof k === 'number');
    const times = group
      .map((d) => d.acquired_at)
      .filter((t): t is string => typeof t === 'string' && t !== '');

    const confidence = { high: 0, nominal: 0, low: 0, unknown: 0 };
    const daynight = { day: 0, night: 0, unknown: 0 };
    for (const d of group) {
      confidence[confidenceBucket(d.confidence)] += 1;
      daynight[d.daynight] += 1;
    }

    return {
      rank: 0,
      longitude,
      latitude,
      detections: group,
      totalFrp: total,
      peakFrp: weights.length ? Math.max(...weights) : 0,
      peakBrightnessK: brightnesses.length ? Math.max(...brightnesses) : null,
      lastSeen: times.length ? times.sort().at(-1) ?? null : null,
      confidence,
      daynight,
      km: centre ? distanceKm(centre, [longitude, latitude]) : null,
    } satisfies Hotspot;
  });

  return summarised
    .sort((a, b) => b.totalFrp - a.totalFrp)
    .slice(0, limit)
    .map((hotspot, index) => ({ ...hotspot, rank: index + 1 }));
}

/** Total fire radiative power across every detection, megawatts. */
export function totalFrp(detections: readonly FireDetection[]): number | null {
  const known = detections.map((d) => d.frp).filter((f): f is number => typeof f === 'number');
  if (known.length === 0) return null;
  return known.reduce((sum, frp) => sum + frp, 0);
}

// ------------------------------------------------------------------- WebGL

/**
 * Whether this browser can draw the map at all.
 *
 * Checked before deck.gl is mounted rather than left to throw inside it. Two
 * real cases: a station tablet old enough to lack WebGL2, and jsdom, where the
 * test suite renders this component and must get the panel's chrome rather than
 * an exception. Either way the honest answer is the counts and a sentence
 * saying the map could not be drawn -- not a blank rectangle.
 *
 * **Asked once, and the context it costs is handed straight back.**
 *
 * Answering this question means acquiring a real WebGL2 context, and a browser
 * caps how many may be live at once -- Chrome at about sixteen. This ran from
 * the component body, so every render took another one and left it for the
 * collector. Standby re-renders on a seven-second poll, so a console left open
 * drifted past the cap, and at the cap Chrome does not refuse the new context:
 * it kills the OLDEST live one. The oldest one is deck.gl's, the one actually
 * drawing the region. The canvas turns white and stays white, because deck.gl
 * does not rebuild after a lost context -- and a white rectangle where the
 * region should be reads as "nothing is burning", which is the one thing this
 * panel must never say by accident.
 *
 * The browser's capability does not change between renders, so it is asked
 * once and remembered. The probe context is released explicitly rather than
 * left to garbage collection, because the cap counts live contexts and not
 * live canvases.
 */
let webgl2Support: boolean | null = null;

/** Test seam: forget the cached answer. Never called by the app. */
export function __resetWebGL2Probe(): void {
  webgl2Support = null;
}

export function hasWebGL2(): boolean {
  if (webgl2Support !== null) return webgl2Support;
  if (typeof document === 'undefined') return false;
  try {
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('webgl2');
    // Give it back at once. Without this the probe holds a slot against the
    // browser's cap for as long as the canvas takes to be collected.
    (
      context as WebGL2RenderingContext | null
    )?.getExtension('WEBGL_lose_context')?.loseContext();
    webgl2Support = context !== null;
    return webgl2Support;
  } catch {
    webgl2Support = false;
    return false;
  }
}

// --------------------------------------------------------------------- props

export interface RegionalHeatMapProps {
  activity: FireActivity | null;
  /** A failed *request*, as distinct from an answered one carrying a refusal. */
  error?: string | null;
  basemap: RegionBasemapView | null;
  /** Forced off for tests and for a browser with no WebGL2. */
  webgl?: boolean;
}

/** The slice of deck.gl's picking info this panel reads. */
interface HexPickingInfo {
  picked?: boolean;
  x?: number;
  y?: number;
  object?: { points?: unknown; elevationValue?: unknown } | null;
}

interface HoveredBin {
  count: number;
  frp: number;
  x: number;
  y: number;
}

// ---------------------------------------------------------------- the panel

export function RegionalHeatMap({
  activity,
  error = null,
  basemap,
  webgl,
}: RegionalHeatMapProps) {
  const [deck, setDeck] = useState<DeckModules | null>(null);
  /**
   * Why the renderer is not on screen, when it is not.
   *
   * A panel that says "Drawing the region…" forever is the failure this
   * codebase refuses everywhere else: it reads as "working" and it is not.
   * The import can genuinely fail -- a chunk that 404s behind a stale service
   * worker, a browser that rejects the module -- and when it does, this says
   * so and the counts and key below still stand.
   */
  const [loadFailed, setLoadFailed] = useState<string | null>(null);
  const [hovered, setHovered] = useState<HoveredBin | null>(null);
  /** Which hotspot's card is open, by rank. Null is none, which is the default. */
  const [selected, setSelected] = useState<number | null>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);

  /**
   * A **callback** ref, not `useRef` plus a mount effect.
   *
   * The frame only exists in this component's success branch: before the first
   * fire-activity answer arrives the panel renders a refusal instead, and that
   * branch has no frame in it. A `useEffect(..., [])` therefore ran once, on a
   * render where the node did not exist, found `null`, and never ran again --
   * so the map sat on "Drawing the region…" permanently while every other part
   * of the panel worked.
   *
   * `PhotorealisticModel` was bitten by the same shape and solved it by keeping
   * its mount node in the tree in every state. A callback ref is the other
   * solution and the better one here, because it also survives the frame being
   * unmounted and remounted when the panel switches between refusal and data.
   */
  const frameRef = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  const drawable = webgl ?? hasWebGL2();

  // deck.gl is imported only where it can run. A static import would pull
  // luma.gl into the server bundle and into jsdom, where constructing a device
  // throws before any of this component's own error handling can report it.
  useEffect(() => {
    if (!drawable) return;
    let live = true;
    loadDeck().then(
      (modules) => {
        if (live) setDeck(modules);
      },
      (cause: unknown) => {
        if (!live) return;
        // Named, not swallowed. Without this the panel waits for a promise
        // that already rejected.
        setLoadFailed(cause instanceof Error ? cause.message : 'the renderer could not be loaded');
      },
    );
    return () => {
      live = false;
    };
  }, [drawable]);

  // Memoised because `?? []` mints a new array every render, which would make
  // every downstream memo and every deck.gl layer rebuild on each frame.
  const detections = useMemo(() => activity?.detections ?? [], [activity]);
  // Memoised for the same reason, and it matters more: an unmemoised centre is a
  // new array on every render, so hovering a pin or opening a hotspot card --
  // neither of which is new data -- invalidated the hotspot clustering and every
  // deck.gl layer underneath it.
  const districtCentre = useMemo(
    () => (activity?.cityBBox ? centreOf(activity.cityBBox) : null),
    [activity],
  );
  const nearest = useMemo(
    () => nearestDetection(detections, districtCentre),
    [detections, districtCentre],
  );
  const summedFrp = useMemo(() => totalFrp(detections), [detections]);
  const hotspots = useMemo(
    () => hotspotsFrom(detections, districtCentre),
    [detections, districtCentre],
  );
  const openHotspot = hotspots.find((h) => h.rank === selected) ?? null;

  /**
   * Where the mesh's tiles come from, or null when there is no mesh.
   *
   * Inferred from the basemap rather than probed. Both are chosen by the same
   * `IMAGERY_PROVIDER` switch in the container -- `_build_tiles` and
   * `_build_imagery` read it identically -- so a basemap that answered means
   * tiles will, and one that refused means they will not. Probing a tile to
   * find out would spend a metered request to learn something the answer in
   * hand already implies.
   */
  const tileBase =
    basemap?.available === true
      ? gatewayPath('/api/v1/terrain')
      : null;

  const heading = (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h2 id="regional-heat-heading" className="text-label uppercase tracking-widest text-muted">
        Regional heat map
      </h2>
      {activity?.source && (
        <span className="font-mono text-micro text-muted">{activity.source}</span>
      )}
    </div>
  );

  if (error) {
    return (
      <Shell heading={heading}>
        <p className="border border-dashed border-alarm px-3 py-2 text-micro text-alarm">
          Fire-activity request failed: {error}
        </p>
      </Shell>
    );
  }

  if (!activity || !activity.available) {
    return (
      <Shell heading={heading}>
        <p className="border border-dashed border-line px-3 py-2 text-micro text-muted">
          Fire activity UNAVAILABLE —{' '}
          {activity?.unavailable_reason ?? 'the backend reported none. Nothing is inferred here.'}
        </p>
      </Shell>
    );
  }

  /**
   * What the camera frames -- the *region*, not the basemap.
   *
   * The basemap covers more ground than the region (integer zoom), so framing
   * on it would leave a margin of ocean and Nevada on every side and shrink the
   * subject. Framing on the region instead lets the ground plane bleed off the
   * edges, which is what a map is supposed to do.
   */
  const bounds = activity.bbox ?? basemap?.bounds ?? null;

  return (
    <Shell heading={heading}>
      {/* The counts live in the Regional fire activity card beside this one,
          and are deliberately not repeated here: two panels printing the same
          number is two places for it to drift. What this line carries is what
          only the map can say -- how far the nearest anomaly is from the
          district, which is the whole reason the rings are drawn. */}
      <p className="mb-2 text-micro text-muted" data-testid="regional-heat-lede">
        {nearest ? (
          <>
            Nearest detection{' '}
            <span className="font-mono text-base text-ink">{nearest.km.toFixed(0)} km</span> from{' '}
            {activity.cityLabel}
          </>
        ) : detections.length > 0 ? (
          <>
            {detections.length} {detections.length === 1 ? 'detection' : 'detections'} across{' '}
            {activity.regionLabel} — no city box reported, so no distance is claimed
          </>
        ) : (
          <>No detections in {activity.regionLabel} over the reported window</>
        )}
      </p>

      {/* **The frame takes what is left, and the key is what is left over
          from.** It used to hold a 440 px floor at `lg`, which is taller than
          the space the standby column can spare once a structure profile is
          open below it -- so the panel's own content ran past the bottom of the
          card, the card clips (`overflow-hidden`, and the column scrolls
          outside it, so the overflow was unreachable), and the half that went
          over the edge was the key. A map with no key is a picture of colours
          nobody can read, so the map is the thing that gives way: the floor
          here is only the point below which the frame is not worth drawing at
          all, and it applies on the stacked layout, which has no ceiling to
          overflow. */}
      <div
        ref={frameRef}
        className="relative min-h-[220px] flex-1 overflow-hidden rounded-md border border-line bg-ground"
        data-testid="regional-heat-canvas"
      >
        {drawable && deck && bounds && size ? (
          <DeckScene
            deck={deck}
            bounds={bounds}
            size={size}
            basemap={basemap}
            detections={detections}
            districtCentre={districtCentre}
            hotspots={hotspots}
            selected={selected}
            tileBase={tileBase}
            onSelect={setSelected}
            onHover={setHovered}
          />
        ) : (
          <p className="absolute inset-0 flex items-center justify-center px-6 text-center text-micro text-muted">
            {!drawable
              ? 'This display cannot draw the map: no WebGL2. The counts above and the key below still apply.'
              : loadFailed
                ? `The map renderer could not be loaded: ${loadFailed}. The counts above and the key below still apply.`
                : !bounds
                  ? 'No bounding box reported, so there is nothing to project the detections onto.'
                  : 'Drawing the region…'}
          </p>
        )}

        {openHotspot && (
          <HotspotCard
            hotspot={openHotspot}
            cityLabel={activity.cityLabel}
            onClose={() => setSelected(null)}
          />
        )}

        {hovered && !openHotspot && (
          <div
            className="pointer-events-none absolute z-10 rounded border border-line bg-surface/95 px-2 py-1 font-mono text-micro text-ink shadow-lg"
            style={{ left: hovered.x + 12, top: hovered.y + 12 }}
            role="status"
          >
            {hovered.count} {hovered.count === 1 ? 'detection' : 'detections'} ·{' '}
            {hovered.frp.toFixed(1)} MW
          </div>
        )}
      </div>

      <MapKey
        activity={activity}
        detections={detections}
        summedFrp={summedFrp}
        basemap={basemap}
      />
    </Shell>
  );
}


/**
 * What the instrument actually reported at one hotspot.
 *
 * Every row is read or summed from the detection table. There is deliberately
 * no risk score, no spread projection and no "concern level": a five-day
 * detection table does not support one, and a number with a label like that
 * would be acted on as though it did.
 *
 * **Brightness is a temperature, not an anomaly.** VIIRS says how hot the pixel
 * radiated; it ships no background to subtract, so "+8 °C above normal" would be
 * inventing the normal. It is printed as what it is, in °C, with the kelvin the
 * feed sent it in.
 *
 * Fire weather is deliberately absent. It exists -- NASA POWER reanalysis, in
 * the card beside this panel -- but it is regional and days old, and putting it
 * inside a per-hotspot card would read as conditions measured *there, now*.
 */
function HotspotCard({
  hotspot,
  cityLabel,
  onClose,
}: {
  hotspot: Hotspot;
  cityLabel: string;
  onClose: () => void;
}) {
  const celsius =
    hotspot.peakBrightnessK === null ? null : hotspot.peakBrightnessK - 273.15;
  const dominant =
    hotspot.confidence.high >= hotspot.confidence.nominal &&
    hotspot.confidence.high >= hotspot.confidence.low
      ? 'high'
      : hotspot.confidence.nominal >= hotspot.confidence.low
        ? 'nominal'
        : 'low';

  return (
    <div
      className="absolute right-3 top-3 z-20 w-64 rounded-md border border-line bg-surface/95 p-3 shadow-lg"
      role="dialog"
      aria-label={`Hotspot ${hotspot.rank}`}
      data-testid="hotspot-card"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-micro uppercase tracking-widest text-ink">
          Hotspot {hotspot.rank}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="text-micro text-muted underline underline-offset-2 hover:text-ink"
        >
          close
        </button>
      </div>

      <dl className="mt-2 space-y-1">
        <Row label="Radiative power" value={`${hotspot.totalFrp.toFixed(1)} MW`} accent />
        <Row label="Hottest pixel" value={`${hotspot.peakFrp.toFixed(1)} MW`} />
        <Row
          label="Brightness"
          value={
            celsius === null
              ? 'not reported'
              : `${celsius.toFixed(0)} °C · ${hotspot.peakBrightnessK?.toFixed(0)} K`
          }
        />
        <Row
          label="Detections"
          value={`${hotspot.detections.length} · ${dominant} confidence`}
        />
        <Row
          label="Passes"
          value={`${hotspot.daynight.day} day · ${hotspot.daynight.night} night`}
        />
        <Row
          label={`From ${cityLabel}`}
          value={hotspot.km === null ? 'not measurable' : `${hotspot.km.toFixed(0)} km`}
        />
        <Row
          label="Last seen"
          value={
            hotspot.lastSeen
              ? new Date(hotspot.lastSeen).toISOString().replace('T', ' ').slice(0, 16) + 'Z'
              : 'not reported'
          }
        />
      </dl>

      <p className="mt-2 text-micro leading-4 text-muted">
        A satellite pass, not a fire report. Nothing here is modelled.
      </p>
    </div>
  );
}

function Row({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-micro text-muted">{label}</dt>
      <dd className={`font-mono text-micro ${accent ? 'text-disputed' : 'text-ink'}`}>{value}</dd>
    </div>
  );
}

function Shell({ heading, children }: { heading: React.ReactNode; children: React.ReactNode }) {
  return (
    <section
      // `flex-1 min-h-0`, so the panel is exactly as tall as the card that holds
      // it. Sized by its content instead, it grew past the card's bottom edge
      // and the card clipped the overflow -- which is how the key disappeared.
      // Filling the card is what gives the frame a real height to divide with
      // the key rather than a height it invents from its own minimums.
      aria-labelledby="regional-heat-heading"
      className="flex min-h-0 flex-1 flex-col bg-ground px-4 py-3"
      data-testid="regional-heat"
    >
      {heading}
      <div className="mt-2 flex min-h-0 flex-1 flex-col">{children}</div>
    </section>
  );
}

// ------------------------------------------------------------------- the key

/**
 * The key. Not decoration: a height and a colour with no units is a picture of
 * nothing, and this is the only place the bin radius and the window appear.
 */
function MapKey({
  activity,
  detections,
  summedFrp,
  basemap,
}: {
  activity: FireActivity;
  detections: readonly FireDetection[];
  summedFrp: number | null;
  basemap: RegionBasemapView | null;
}) {
  const peak = useMemo(() => {
    const frps = detections.map((d) => d.frp).filter((f): f is number => typeof f === 'number');
    return frps.length ? Math.max(...frps) : null;
  }, [detections]);

  return (
    <div className="mt-2 shrink-0 space-y-1.5" data-testid="regional-heat-key">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-micro uppercase tracking-widest text-muted">Fire radiative power</span>
        <span className="flex items-center gap-1" aria-hidden="true">
          {THERMAL_RAMP.map((rgb) => (
            <span
              key={rgb.join(',')}
              className="h-2.5 w-6 rounded-sm"
              style={{ background: `rgb(${rgb.join(',')})` }}
            />
          ))}
        </span>
        <span className="font-mono text-micro text-muted">
          low → high{peak !== null && <> · peak {peak.toFixed(0)} MW</>}
        </span>
      </div>

      <p className="text-micro leading-5 text-muted">
        The field is weighted by radiative power and{' '}
        <strong className="text-ink">relative to the busiest area in this window</strong> — a quiet
        week and a bad one fill the frame alike, so the absolute figures are the ones here. Numbered
        pins are the {HOTSPOT_COUNT} strongest clusters; click one for what the instrument reported.
        Rings mark {RING_KM.join(', ')} km from the district.
        {summedFrp !== null && (
          <>
            {' '}
            Region total <span className="font-mono text-ink">{summedFrp.toFixed(0)} MW</span> over{' '}
            {detections.length} {detections.length === 1 ? 'detection' : 'detections'}.
          </>
        )}
      </p>

      <p className="text-micro leading-5 text-muted">
        Terrain is drawn at{' '}
        <strong className="text-ink">×{VERTICAL_EXAGGERATION} vertical exaggeration</strong>. The
        region is 550 km across and its relief is under half a percent of that, so true scale is a
        flat sheet — the shape is real, the steepness is not.
      </p>

      {activity.resolution_note ? (
        <p className="text-micro leading-5 text-muted">{activity.resolution_note}</p>
      ) : (
        <details>
          <summary className="cursor-pointer text-micro text-muted hover:text-ink">
            Why the city is always empty
          </summary>
          <p className="mt-1 text-micro leading-5 text-muted">
            VIIRS pixels are ~375 m and built for wildfire, so a structure fire never registers
            here. An empty city inside a busy region is the instrument working, not a fault.
          </p>
        </details>
      )}

      {/* Google's Terms require attribution wherever Maps imagery shows, and a
          basemap under a data layer is still the imagery being shown. */}
      {basemap?.attribution && (
        <p className="font-mono text-micro text-muted">{basemap.attribution}</p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ deck.gl

interface DeckModules {
  DeckGL: typeof import('@deck.gl/react').default;
  BitmapLayer: typeof import('@deck.gl/layers').BitmapLayer;
  PathLayer: typeof import('@deck.gl/layers').PathLayer;
  ScatterplotLayer: typeof import('@deck.gl/layers').ScatterplotLayer;
  TextLayer: typeof import('@deck.gl/layers').TextLayer;
  HeatmapLayer: typeof import('@deck.gl/aggregation-layers').HeatmapLayer;
  TerrainLayer: typeof import('@deck.gl/geo-layers').TerrainLayer;
  WebMercatorViewport: typeof import('@deck.gl/core').WebMercatorViewport;
}

async function loadDeck(): Promise<DeckModules> {
  const [react, layers, aggregation, geo, core] = await Promise.all([
    import('@deck.gl/react'),
    import('@deck.gl/layers'),
    import('@deck.gl/aggregation-layers'),
    import('@deck.gl/geo-layers'),
    import('@deck.gl/core'),
  ]);
  return {
    DeckGL: react.default,
    BitmapLayer: layers.BitmapLayer,
    PathLayer: layers.PathLayer,
    ScatterplotLayer: layers.ScatterplotLayer,
    TextLayer: layers.TextLayer,
    HeatmapLayer: aggregation.HeatmapLayer,
    TerrainLayer: geo.TerrainLayer,
    WebMercatorViewport: core.WebMercatorViewport,
  };
}

/** Draw over the mesh rather than inside it. See the module docstring. */
const OVER_TERRAIN = { depthTest: false } as const;

/**
 * Where the camera is, as this panel owns it.
 *
 * deck.gl hands back a good deal more than this on every gesture -- the width
 * and height it measured, the pivot a rotation started from, the constraints it
 * applied. Keeping five fields and re-declaring the constraints is deliberate:
 * the transient half of that object is about one gesture and has no business
 * outliving it, and the width and height are the frame's to report, not the
 * camera's to remember.
 */
interface Camera {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

/**
 * The camera that frames a region on first sight of it.
 *
 * Computed once per region rather than per render -- see `regionKey`. Nothing
 * here depends on the frame having settled at its final size: a later resize
 * widens what the camera sees, which is what a resize should do, and re-solving
 * for the new frame would move a camera the officer had already aimed.
 */
function frameRegion(
  Viewport: DeckModules['WebMercatorViewport'],
  bounds: FireBBox,
  size: { width: number; height: number },
): Camera {
  const fitted = new Viewport({ width: size.width, height: size.height }).fitBounds(
    [
      [bounds.west, bounds.south],
      [bounds.east, bounds.north],
    ],
    { padding: 24 },
  );
  return {
    longitude: fitted.longitude,
    latitude: fitted.latitude,
    // `fitBounds` solves for a top-down camera. Tilting one back widens the
    // ground it can see, so the region it just fitted shrinks into the middle
    // of the frame with a margin all round. This is the compensation, and it is
    // a constant because the pitch is.
    zoom: fitted.zoom + PITCH_ZOOM_COMPENSATION,
    pitch: CAMERA_PITCH,
    bearing: 0,
  };
}

function DeckScene({
  deck,
  bounds,
  size,
  basemap,
  detections,
  districtCentre,
  hotspots,
  selected,
  tileBase,
  onSelect,
  onHover,
}: {
  deck: DeckModules;
  bounds: FireBBox;
  size: { width: number; height: number };
  basemap: RegionBasemapView | null;
  detections: readonly FireDetection[];
  districtCentre: readonly [number, number] | null;
  hotspots: readonly Hotspot[];
  selected: number | null;
  /** Tile template root, or null when the mesh cannot be built. */
  tileBase: string | null;
  onSelect: (rank: number | null) => void;
  onHover: (bin: HoveredBin | null) => void;
}) {
  const {
    DeckGL,
    BitmapLayer,
    PathLayer,
    ScatterplotLayer,
    TextLayer,
    HeatmapLayer,
    TerrainLayer,
    WebMercatorViewport,
  } = deck;

  /**
   * **The camera is this component's state, not deck.gl's.**
   *
   * It was `initialViewState`, which reads as "set it once" and is not: deck
   * re-reads that prop on every `setProps` and overwrites its internal camera
   * whenever the value differs, and the value was solved from `bounds` and from
   * the frame's measured size. Both of those move under a camera that is not
   * moving. The region box arrives as a new object on every fire-activity poll,
   * and the frame is `flex-1` between a lede and a key that both reflow when
   * the detection count changes -- so a poll that added a line of text resized
   * the frame by a few pixels, which re-solved the fit, which threw the camera
   * back to the opening shot. An officer who had zoomed out was zoomed back in
   * for them, and had to do it again.
   *
   * Owning it here inverts that. Deck reports where the gesture left the camera
   * and this holds it; nothing else writes it except a genuine change of
   * region, which is checked below by the corners rather than by the object.
   */
  const [camera, setCamera] = useState<Camera>(() =>
    frameRegion(WebMercatorViewport, bounds, size),
  );

  const region = regionKey(bounds);
  const [framed, setFramed] = useState(region);
  if (framed !== region) {
    // Re-framing during render rather than in an effect, so the first paint of
    // a new region is already pointed at it -- an effect would show one frame
    // of the old camera over the new ground. Only a genuine move gets here: a
    // refetch of the same region compares equal and leaves the camera alone.
    setFramed(region);
    setCamera(frameRegion(WebMercatorViewport, bounds, size));
  }

  /**
   * Whether any ground has been drawn yet.
   *
   * Latched on purpose: once a square of mesh has arrived the map is a map, and
   * a later poll that re-reads the same region must not put a "loading" line
   * back over ground the officer is already looking at.
   */
  const [groundLoaded, setGroundLoaded] = useState(false);
  const onGroundLoad = useCallback(() => setGroundLoaded(true), []);

  /**
   * The region as the tile loader wants it, and stable across polls.
   *
   * Memoised on the four *numbers* rather than on `bounds`, because the
   * fire-activity poll hands back a new box object with identical corners every
   * few minutes -- see `regionKey`. Keyed on the object, this array would be new
   * on every poll and would rebuild the mesh underneath a camera nobody moved.
   */
  const { west, south, east, north } = bounds;
  const regionExtent = useMemo(
    () => terrainExtent({ west, south, east, north }),
    [west, south, east, north],
  );

  const viewState = useMemo(
    () => ({
      ...camera,
      // The floor is the mesh's, not a taste in how far out a regional map
      // should go: below it `TileLayer` has no tiles and the ground disappears
      // entirely. Stopping the camera at the edge of the data is honest in a
      // way that letting it slide off into black is not. With no mesh -- the
      // flat-image fallback -- there is no such edge and no floor to impose.
      minZoom: tileBase ? TERRAIN_MIN_ZOOM : undefined,
    }),
    [camera, tileBase],
  );

  const layers = useMemo(() => {
    const built: unknown[] = [];

    if (tileBase) {
      built.push(
        new TerrainLayer({
          id: 'region-terrain',
          minZoom: TERRAIN_MIN_ZOOM,
          maxZoom: TERRAIN_MAX_ZOOM,
          elevationDecoder: TERRARIUM_DECODER,
          elevationData: `${tileBase}/elevation/{z}/{x}/{y}`,
          texture: `${tileBase}/imagery/{z}/{x}/{y}`,
          // Coarser than the default, on purpose: the mesh is regional scenery
          // under a data layer, not a survey, and a tighter tolerance spends
          // frame time triangulating ground nobody is measuring.
          meshMaxError: 12,
          // Flat-lit rather than shaded by a light this scene does not have.
          // A specular hillside under an orange heat field reads as more fire.
          material: false,
          /**
           * **The proxy's region, told to the tile loader instead of discovered.**
           *
           * The backend serves one region and refuses every square outside it
           * before it contacts a provider -- that refusal is what stops the
           * endpoint being an open relay onto the department's metered quota.
           * It is cheap to answer and it is not cheap to *ask*: a camera tilted
           * back 50 degrees sees a frustum whose ground footprint runs well past
           * the region on three sides, so without this the mesh requested a pile
           * of squares whose only possible answer was 404, two per square
           * because height and skin are separate grids. Each one is a real trip
           * through the gateway and each one occupies one of the six connections
           * the browser will open to this origin -- which is to say they were
           * queued ahead of the tiles the officer is actually waiting on.
           *
           * `extent` makes the tile loader skip them. It is the region box
           * rather than the basemap's, deliberately: the basemap covers more
           * ground than the region (an integer zoom always does) and the extra
           * is exactly the ground the proxy will not serve.
           */
          extent: regionExtent,
          // A square that 404s is a hole, not a failure: the heat field, the
          // rings and the key are all drawn regardless.
          onTileError: () => {},
          // The one signal that ground has arrived. `onViewportLoad` would read
          // better and is not available: `TerrainLayer` binds that prop to its
          // own z-range bookkeeping and never calls a caller's.
          onTileLoad: onGroundLoad,
        }),
      );
    } else if (basemap?.available && basemap.data_url && basemap.bounds) {
      // No mesh: fall back to the one flat image, drawn against the box it
      // actually covers. Strictly worse and strictly honest -- it is the same
      // ground, without the shape.
      built.push(
        new BitmapLayer({
          id: 'region-ground',
          image: basemap.data_url,
          bounds: [
            basemap.bounds.west,
            basemap.bounds.south,
            basemap.bounds.east,
            basemap.bounds.north,
          ],
          opacity: 1,
        }),
      );
    }

    if (detections.length > 0) {
      built.push(
        new HeatmapLayer({
          id: 'heat-field',
          data: detections as FireDetection[],
          getPosition: (d: FireDetection) => [d.longitude, d.latitude],
          // Weighted by radiative power, not by count: ten smouldering pixels
          // and one campaign fire are not the same event.
          getWeight: (d: FireDetection) => d.frp ?? 0,
          radiusPixels: HEAT_RADIUS_PX,
          intensity: 1,
          // The field fades out rather than resolving into a cool colour. VIIRS
          // does not report "cool" -- it reports nothing there -- and painting
          // the quiet ground blue would be data where there is none.
          threshold: 0.05,
          colorRange: HEAT_RANGE.map((rgb) => [...rgb]) as [number, number, number][],
          opacity: 0.75,
          parameters: OVER_TERRAIN,
        }),
      );
    }

    if (districtCentre) {
      built.push(
        new PathLayer({
          id: 'range-rings',
          data: RING_KM.map((km) => ({ km, path: ringPolygon(districtCentre, km) })),
          getPath: (d: { path: [number, number][] }) => d.path,
          getColor: [...LIVE_BLUE, 90] as [number, number, number, number],
          getWidth: 1.5,
          widthUnits: 'pixels',
          widthMinPixels: 1,
          parameters: OVER_TERRAIN,
        }),
        new TextLayer({
          id: 'range-ring-labels',
          data: RING_KM.map((km) => ({
            km,
            position: [districtCentre[0], districtCentre[1] + km / KM_PER_DEG_LAT],
          })),
          getPosition: (d: { position: [number, number] }) => d.position,
          getText: (d: { km: number }) => `${d.km} km`,
          getSize: 10,
          getColor: [...LIVE_BLUE, 170] as [number, number, number, number],
          fontFamily: 'ui-monospace, Menlo, monospace',
          getTextAnchor: 'middle',
          getAlignmentBaseline: 'bottom',
          parameters: OVER_TERRAIN,
        }),
        // The district itself: hollow, in a colour no data uses, so it can
        // never be read as a detection.
        new ScatterplotLayer({
          id: 'district-marker',
          data: [{ position: districtCentre }],
          getPosition: (d: { position: [number, number] }) => d.position,
          stroked: true,
          filled: false,
          getLineColor: [...LIVE_BLUE, 230] as [number, number, number, number],
          getRadius: 5,
          radiusUnits: 'pixels',
          lineWidthMinPixels: 2,
          parameters: OVER_TERRAIN,
        }),
      );
    }

    if (hotspots.length > 0) {
      built.push(
        new ScatterplotLayer({
          id: 'hotspot-pins',
          data: hotspots as Hotspot[],
          getPosition: (d: Hotspot) => [d.longitude, d.latitude],
          stroked: true,
          filled: true,
          getFillColor: (d: Hotspot) =>
            (d.rank === selected ? [255, 206, 104, 235] : [24, 18, 14, 205]) as [
              number,
              number,
              number,
              number,
            ],
          getLineColor: [255, 206, 104, 240] as [number, number, number, number],
          getRadius: (d: Hotspot) => (d.rank === selected ? 15 : 12),
          radiusUnits: 'pixels',
          lineWidthMinPixels: 2,
          pickable: true,
          onClick: (info: HexPickingInfo) => {
            const hotspot = info.object as Hotspot | null | undefined;
            // Clicking the open one closes it, so the card is dismissable
            // without hunting for an X.
            onSelect(hotspot && hotspot.rank !== selected ? hotspot.rank : null);
            return true;
          },
          onHover: (info: HexPickingInfo): boolean => {
            const hotspot = info.object as Hotspot | null | undefined;
            if (!info.picked || !hotspot) {
              onHover(null);
              return false;
            }
            onHover({
              count: hotspot.detections.length,
              frp: hotspot.totalFrp,
              x: info.x ?? 0,
              y: info.y ?? 0,
            });
            return false;
          },
          parameters: OVER_TERRAIN,
        }),
        new TextLayer({
          id: 'hotspot-ranks',
          data: hotspots as Hotspot[],
          getPosition: (d: Hotspot) => [d.longitude, d.latitude],
          getText: (d: Hotspot) => String(d.rank),
          getSize: 13,
          getColor: (d: Hotspot) =>
            (d.rank === selected ? [24, 18, 14, 255] : [255, 206, 104, 255]) as [
              number,
              number,
              number,
              number,
            ],
          fontFamily: 'ui-monospace, Menlo, monospace',
          fontWeight: 700,
          getTextAnchor: 'middle',
          getAlignmentBaseline: 'center',
          parameters: OVER_TERRAIN,
        }),
      );
    }

    return built;
  }, [
    BitmapLayer,
    PathLayer,
    ScatterplotLayer,
    TextLayer,
    HeatmapLayer,
    TerrainLayer,
    basemap,
    detections,
    districtCentre,
    hotspots,
    onGroundLoad,
    onHover,
    onSelect,
    regionExtent,
    selected,
    tileBase,
  ]);

  return (
    <>
      {/* **The frame says what it is doing rather than sitting black.** The
          heat field, the rings and the numbered hotspots are all up by now --
          they need no network -- but the mesh is scores of tiles and the ground
          under them is empty until the first of those lands. Without a line
          here that gap reads as "the map is broken", which is the reading this
          console refuses everywhere else. It states the wait and nothing more:
          no progress bar over a count nobody has, and no placeholder terrain. */}
      {tileBase && !groundLoaded && (
        <p
          className="pointer-events-none absolute inset-x-0 top-2 z-10 text-center font-mono text-micro text-muted"
          role="status"
          data-testid="regional-heat-terrain-status"
        >
          Loading terrain…
        </p>
      )}
      <DeckGL
        viewState={viewState}
        onViewStateChange={(params) => {
          // Deck types this as the union of every view state any view could
          // report; the only view here is a map view, whose camera is these five
          // fields. It has already been clamped against the constraints above,
          // so what lands here is a camera the mesh can be drawn under.
          const next = params.viewState as unknown as Camera;
          setCamera({
            longitude: next.longitude,
            latitude: next.latitude,
            zoom: next.zoom,
            pitch: next.pitch,
            bearing: next.bearing,
          });
        }}
        controller={{ dragRotate: true }}
        layers={layers as never}
        style={{ position: 'absolute', inset: '0px' }}
        getCursor={({ isDragging }: { isDragging: boolean }) =>
          isDragging ? 'grabbing' : 'crosshair'
        }
      />
    </>
  );
}