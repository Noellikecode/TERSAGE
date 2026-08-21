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

import type { GeometryView } from '@/lib/api/types';

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
  if (unscanned.length > 0) {
    parts.push(`Faces with no thermal coverage: ${unscanned.join(', ')} -- UNSCANNED, not cool.`);
  }
  return parts.join(' ');
}
