'use client';

/**
 * The structure, rendered from GeometrySpec and nothing else.
 *
 * The renderer holds no knowledge about buildings. Every decision it makes --
 * how many levels, which are disputed, where the solar array is, how wide the
 * collapse zone is -- comes out of the spec the backend computed. That is why
 * the static SVG fallback can show the same disagreement: **the conflict is in
 * the data, not in the renderer.**
 *
 * Three ways this degrades, all of them deliberate:
 *
 * - **No WebGL** (locked-down tablet, software rendering disabled) -- the SVG
 *   the backend already produced is shown, with a line saying why.
 * - **Reduced motion** -- no idle rotation, no transitions. The view is
 *   orthographic and fixed either way; motion was never load-bearing.
 * - **No geometry at all** -- an explicit "no geometry on record" panel, never
 *   an empty box that reads as a featureless building.
 */

import { useEffect, useRef, useState } from 'react';

import type { FaceView, GeometryView } from '@/lib/api/types';

export type ViewAngle = 'ALPHA' | 'BRAVO' | 'CHARLIE' | 'DELTA' | 'ISO';

/** Fixed orthographic camera positions. No free orbit: a fireground view is
 * a view an officer can name over the radio. */
const VIEWS: Record<ViewAngle, [number, number, number]> = {
  ALPHA: [0, 8, 40],
  BRAVO: [40, 8, 0],
  CHARLIE: [0, 8, -40],
  DELTA: [-40, 8, 0],
  ISO: [28, 24, 28],
};

const COLORS = {
  confirmed: 0x4ade80,
  disputed: 0xfbbf24,
  unknown: 0x7c8b9a,
  roof: 0x38bdf8,
  solar: 0xf87171,
  collapse: 0xfbbf24,
  ground: 0x0a0c0f,
};

/**
 * The thermal ramp: **one hue, monotonic lightness**, ambient to hot.
 *
 * Single-hue rather than the classic black-red-yellow-white ironbow, because a
 * multi-hue ramp makes magnitude a hue comparison and readers cannot order hues
 * reliably. Lightness carries the magnitude here and it is monotonic across all
 * five steps.
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
  const t = Math.max(0, Math.min(1, (celsius - THERMAL_MIN_C) / span));
  return Math.min(
    THERMAL_RAMP.length - 1,
    Math.floor(t * THERMAL_RAMP.length),
  );
}

export function thermalColor(celsius: number): number {
  // `thermalStep` is clamped into range, so the lookup always hits; the
  // fallback exists only to satisfy noUncheckedIndexedAccess.
  return THERMAL_RAMP[thermalStep(celsius)] ?? THERMAL_RAMP[0];
}

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function webglAvailable(): boolean {
  if (typeof document === 'undefined') return false;
  try {
    const canvas = document.createElement('canvas');
    return Boolean(
      canvas.getContext('webgl2') ?? canvas.getContext('webgl') ?? false,
    );
  } catch {
    return false;
  }
}

/** The face an officer is looking at, as a label they would say aloud. */
export function faceLabelFor(view: ViewAngle): string {
  return view === 'ISO' ? 'Isometric' : `${view} face`;
}

export function GeometryCanvas({
  geometry,
  view = 'ISO',
  forceFallback = false,
}: {
  geometry: GeometryView | null;
  view?: ViewAngle;
  /** Set by tests and by the WebGL-disabled path. */
  forceFallback?: boolean;
}) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const [fallback, setFallback] = useState<'none' | 'no-webgl' | 'error'>('none');

  useEffect(() => {
    if (forceFallback || !geometry) return;
    if (!webglAvailable()) {
      setFallback('no-webgl');
      return;
    }

    const mount = mountRef.current;
    if (!mount) return;

    let disposed = false;
    let cleanup: (() => void) | undefined;

    // Imported dynamically so a tablet without WebGL never downloads or parses
    // the renderer at all.
    import('three')
      .then((THREE) => {
        if (disposed) return;
        const width = mount.clientWidth || 480;
        const height = mount.clientHeight || 360;

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(COLORS.ground);

        const aspect = width / height;
        const frustum = 26;
        const camera = new THREE.OrthographicCamera(
          (-frustum * aspect) / 2,
          (frustum * aspect) / 2,
          frustum / 2,
          -frustum / 2,
          0.1,
          400,
        );
        const [x, y, z] = VIEWS[view];
        camera.position.set(x, y, z);
        camera.lookAt(0, 6, 0);

        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setSize(width, height);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        mount.appendChild(renderer.domElement);
        renderer.domElement.setAttribute('role', 'img');
        renderer.domElement.setAttribute(
          'aria-label',
          describeGeometry(geometry, view),
        );

        scene.add(new THREE.AmbientLight(0xffffff, 0.75));
        const key = new THREE.DirectionalLight(0xffffff, 0.6);
        key.position.set(20, 30, 20);
        scene.add(key);

        const footprint = geometry.spec.footprint;
        const widthX = Math.max(...footprint.map((p) => p[0])) || 11.5;
        const depthZ = Math.max(...footprint.map((p) => p[1])) || 22;

        // Levels, bottom up. A disputed level is drawn differently *because the
        // spec says it is disputed* -- the renderer never decides that.
        let base = 0;
        geometry.spec.levels.forEach((level) => {
          const disputed = level.status === 'DISPUTED';
          const geo = new THREE.BoxGeometry(widthX, level.height_m, depthZ);
          const material = new THREE.MeshStandardMaterial({
            color: disputed ? COLORS.disputed : COLORS.confirmed,
            // Disputed mass is translucent and wireframed as well as coloured,
            // so it reads as disputed without relying on colour.
            transparent: disputed,
            opacity: disputed ? 0.35 : 1,
            wireframe: false,
          });
          const mesh = new THREE.Mesh(geo, material);
          mesh.position.set(0, base + level.height_m / 2, 0);
          scene.add(mesh);

          if (disputed) {
            const edges = new THREE.LineSegments(
              new THREE.EdgesGeometry(geo),
              new THREE.LineDashedMaterial({
                color: COLORS.disputed,
                dashSize: 0.6,
                gapSize: 0.4,
              }),
            );
            edges.computeLineDistances();
            edges.position.copy(mesh.position);
            scene.add(edges);
          }
          base += level.height_m;
        });

        // The heat map, registered onto the faces the mass was extruded from.
        //
        // Each cell names a rectangle of one wall in face coordinates: u across
        // the width, v UP from the ground. The quad is placed on that wall at
        // exactly those bounds, so the overlay lands on the building rather
        // than floating near it. Total height is `base`, which the level loop
        // above just finished accumulating.
        //
        // A face with no cells gets no quads at all. UNSCANNED is drawn as
        // nothing, never as a cool wall.
        const totalHeight = base;
        const FACE_PLACEMENT: Record<
          string,
          { axis: 'x' | 'z'; sign: 1 | -1 }
        > = {
          ALPHA: { axis: 'z', sign: 1 },
          BRAVO: { axis: 'x', sign: 1 },
          CHARLIE: { axis: 'z', sign: -1 },
          DELTA: { axis: 'x', sign: -1 },
        };

        geometry.spec.faces.forEach((face) => {
          const placement = FACE_PLACEMENT[face.label];
          if (!placement || !face.thermal_cells?.length) return;
          const spanAcross = placement.axis === 'z' ? widthX : depthZ;

          face.thermal_cells.forEach((cell) => {
            const cellWidth = (cell.u_to - cell.u_from) * spanAcross;
            const cellHeight = (cell.v_to - cell.v_from) * totalHeight;
            if (cellWidth <= 0 || cellHeight <= 0) return;

            const quad = new THREE.Mesh(
              new THREE.PlaneGeometry(cellWidth, cellHeight),
              new THREE.MeshBasicMaterial({
                color: thermalColor(cell.temperature_c),
                side: THREE.DoubleSide,
                transparent: true,
                // Translucent so the mass beneath still reads as confirmed or
                // disputed. The overlay adds information; it does not replace
                // what the slow loop established.
                opacity: 0.82,
              }),
            );

            // Centre of the cell, in face coordinates, mapped to world space.
            const across =
              ((cell.u_from + cell.u_to) / 2 - 0.5) * spanAcross * placement.sign;
            const up = ((cell.v_from + cell.v_to) / 2) * totalHeight;
            // A hair proud of the wall so it does not z-fight with the mass.
            const out = (placement.axis === 'z' ? depthZ : widthX) / 2 + 0.03;

            if (placement.axis === 'z') {
              quad.position.set(across, up, out * placement.sign);
              if (placement.sign === -1) quad.rotation.y = Math.PI;
            } else {
              quad.position.set(out * placement.sign, up, across);
              quad.rotation.y = (Math.PI / 2) * placement.sign;
            }
            scene.add(quad);
          });
        });

        // Roof segments, pitched as the Solar API measured them.
        geometry.spec.roof_segments.forEach((segment, index) => {
          const plane = new THREE.Mesh(
            new THREE.PlaneGeometry(widthX, depthZ / 2),
            new THREE.MeshStandardMaterial({
              color: COLORS.roof,
              side: THREE.DoubleSide,
              transparent: true,
              opacity: 0.55,
            }),
          );
          plane.rotation.x = -Math.PI / 2 + (segment.pitch_deg * Math.PI) / 180;
          plane.position.set(0, base + 0.4, index === 0 ? depthZ / 4 : -depthZ / 4);
          scene.add(plane);
        });

        // Obstructions a crew cannot cut through.
        geometry.spec.obstructions.forEach((obstruction) => {
          const box = new THREE.Mesh(
            new THREE.BoxGeometry(widthX * 0.5, 0.3, depthZ * 0.25),
            new THREE.MeshStandardMaterial({ color: COLORS.solar }),
          );
          box.position.set(
            0,
            base + 1.1,
            obstruction.segment_index === 0 ? depthZ / 4 : -depthZ / 4,
          );
          scene.add(box);
        });

        // The collapse zone: the 1.5x-height convention, drawn as a ring.
        const ring = new THREE.Mesh(
          new THREE.RingGeometry(
            geometry.spec.collapse_zone_radius_m - 0.25,
            geometry.spec.collapse_zone_radius_m,
            72,
          ),
          new THREE.MeshBasicMaterial({
            color: COLORS.collapse,
            side: THREE.DoubleSide,
            transparent: true,
            opacity: 0.5,
          }),
        );
        ring.rotation.x = -Math.PI / 2;
        ring.position.y = 0.02;
        scene.add(ring);

        renderer.render(scene, camera);

        cleanup = () => {
          renderer.dispose();
          if (renderer.domElement.parentElement === mount) {
            mount.removeChild(renderer.domElement);
          }
        };
      })
      .catch(() => {
        if (!disposed) setFallback('error');
      });

    return () => {
      disposed = true;
      cleanup?.();
    };
  }, [geometry, view, forceFallback]);

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

  const useSvg = forceFallback || fallback !== 'none';

  return (
    <figure className="m-0">
      {useSvg ? (
        <div
          className="border border-line bg-surface p-2 [&_svg]:h-auto [&_svg]:w-full [&_svg_rect]:stroke-ink [&_svg_text]:fill-muted"
          role="img"
          aria-label={describeGeometry(geometry, view)}
          data-testid="geometry-svg"
          dangerouslySetInnerHTML={{ __html: geometry.svg }}
        />
      ) : (
        <div
          ref={mountRef}
          data-testid="geometry-canvas"
          className="h-[360px] w-full border border-line bg-ground"
        />
      )}
      <ThermalLegend faces={geometry.spec.faces} />
      <figcaption className="mt-2 space-y-1 text-micro text-muted">
        <span className="block">{describeGeometry(geometry, view)}</span>
        {useSvg && (
          <span className="block text-disputed">
            {fallback === 'no-webgl'
              ? 'WebGL is unavailable on this device. Showing the static elevation, which marks the same disputed mass.'
              : forceFallback
                ? 'Static elevation. It marks the same disputed mass as the interactive view.'
                : 'The interactive renderer failed to load. Showing the static elevation instead.'}
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
 * SVG fallback. The two darkest ramp steps sit below 3:1 against the surface,
 * which makes these labels required rather than a nicety.
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
 * the conflict.
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
      `${disputed} level${disputed === 1 ? ' is' : 's are'} DISPUTED and drawn with a dashed outline.`,
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
  return parts.join(' ');
}
