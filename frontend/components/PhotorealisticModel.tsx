'use client';

/**
 * The building as it actually is: Google's Photorealistic 3D Tiles.
 *
 * The massing model beside this is *derived* -- a parcel outline extruded to a
 * storey count the fleet worked out from filings and a roof measurement. That
 * is the right thing to draw when the question is "what do the records say",
 * and it is the wrong thing to draw when the question is "what does this
 * building look like". It answered the second question badly enough to read as
 * a placeholder, because a prism over a real footprint is still a prism.
 *
 * So both are on screen and they are not the same claim. This view is
 * photogrammetry -- it shows the structure, its neighbours, and the street,
 * captured at some point in the past. It knows nothing about storeys nobody
 * permitted, and it cannot be asked. The derived model carries the record;
 * this carries the world.
 *
 * **The key reaches the browser, and that is a deliberate exception.** Every
 * other credential in this console stays server-side: the gateway proxy holds
 * the backend token and it never crosses to the client. Photorealistic tiles
 * cannot work that way -- the renderer streams hundreds of tiles directly from
 * `tile.googleapis.com` as the camera moves, and proxying that would put this
 * server in the path of every one. So a *separate, public* Maps key is used,
 * and it must be restricted by HTTP referrer in the Cloud console. An
 * unrestricted key in a browser is billable by anyone who finds it.
 *
 * Absent that key the component renders nothing and says why, rather than
 * showing an empty canvas that reads as a failure.
 */

import { useEffect, useRef, useState } from 'react';

import type * as ThreeNS from 'three';

/** How far back the camera sits from the structure, in metres.
 *
 * Was 220m, which framed a city block and read as blurry mush: photogrammetry
 * has a finite resolution, and at that range the screen space one building
 * occupies is smaller than the tiles it would take to draw it sharply. Close
 * enough now that the requested structure fills the frame and its neighbours
 * give it context. */
const CAMERA_DISTANCE_M = 52;

/** And how high. A shallow oblique rather than a top-down: roof shape and the
 *  street-facing elevation in one view, which is what an arriving officer is
 *  trying to reconcile against the record. */
const CAMERA_HEIGHT_M = 26;

/**
 * Screen-space error target, in pixels: the renderer subdivides until a tile's
 * error is under this, so **lower means sharper and more tiles**.
 *
 * This is the setting that made the difference. `GoogleCloudAuthPlugin`'s
 * `useRecommendedSettings` defaults to true and pins this at 20 -- tuned for
 * flying a whole city, and far too coarse for looking at one building. Hence
 * the plugin's recommendations are declined below and this is set directly.
 */
const ERROR_TARGET_PX = 4;

/** How long tile loading may settle before the view is called ready anyway. A
 *  view that never announces itself is indistinguishable from a broken one. */
const SETTLE_TIMEOUT_MS = 20_000;

/**
 * Why there is no photorealistic view.
 *
 * Split, because these were one state once and the panel printed the key
 * explanation for both -- so a console whose backend had not been restarted
 * reported a missing key that was sitting right there in the bundle. A wrong
 * reason costs more than no reason: it sends someone to fix the wrong thing.
 */
type Status = 'loading' | 'ready' | 'no-key' | 'no-coordinates' | 'failed';

/**
 * What the console knows about this structure's geometry.
 *
 * Passed in rather than inferred from a null coordinate, because "the fetch has
 * not come back yet" and "the fetch came back without a position" are different
 * facts and only the caller holds the difference. Inferring it produced a panel
 * that reported a backend defect while a perfectly good request was still in
 * flight.
 */
export type GeometryState = 'idle' | 'loading' | 'ready' | 'unavailable';

export function PhotorealisticModel({
  latitude,
  longitude,
  label,
  geometryState,
}: {
  latitude: number | null;
  longitude: number | null;
  label: string;
  geometryState: GeometryState;
}) {
  const mount = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<Status>('loading');
  const [attribution, setAttribution] = useState<string>('');
  const [failure, setFailure] = useState<string>('');

  useEffect(() => {
    const key = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY?.trim();
    if (!key) {
      setStatus('no-key');
      return;
    }
    if (typeof latitude !== 'number' || typeof longitude !== 'number') {
      // Not `=== null`: a backend that predates the coordinate field sends no
      // field at all, which arrives as undefined and slipped through a null
      // check into a NaN camera frame.
      setStatus(geometryState === 'ready' ? 'no-coordinates' : 'loading');
      return;
    }

    let disposed = false;
    let cleanup: (() => void) | undefined;
    let raf: number | null = null;

    Promise.all([
      import('three'),
      import('3d-tiles-renderer'),
      import('3d-tiles-renderer/plugins'),
      import('three/examples/jsm/controls/OrbitControls.js'),
    ])
      .then(([THREE, tiles, plugins, { OrbitControls }]) => {
        if (disposed || !mount.current) return;
        const node = mount.current;
        const width = node.clientWidth || 480;
        const height = node.clientHeight || 320;

        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(50, width / height, 1, 8_000_000);
        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        renderer.setSize(width, height);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        node.appendChild(renderer.domElement);
        renderer.domElement.setAttribute('role', 'img');
        renderer.domElement.setAttribute('aria-label', `Photorealistic 3D view of ${label}`);

        const TilesRenderer = tiles.TilesRenderer as unknown as new (
          url?: string,
        ) => Record<string, unknown>;
        const GoogleCloudAuthPlugin = plugins.GoogleCloudAuthPlugin as unknown as new (
          options: Record<string, unknown>,
        ) => unknown;

        const controller = new TilesRenderer() as {
          registerPlugin: (plugin: unknown) => void;
          setResolutionFromRenderer: (camera: unknown, renderer: unknown) => boolean;
          setCamera: (camera: unknown) => void;
          group: ThreeNS.Object3D;
          update: () => void;
          dispose: () => void;
          errorTarget: number;
          /** Live tile counts. `loaded` is what distinguishes "finished" from
              "has not started": a fresh renderer reports nothing in flight,
              which read as settled on the first frame and had the screenshot
              wait for nothing. */
          stats?: { downloading: number; parsing: number; loaded: number };
          ellipsoid: {
            getObjectFrame: (
              lat: number,
              lon: number,
              height: number,
              azimuth: number,
              elevation: number,
              roll: number,
              target: ThreeNS.Matrix4,
            ) => ThreeNS.Matrix4;
          };
          getAttributions?: (target: { value: string }[]) => void;
          addEventListener?: (name: string, handler: () => void) => void;
        };

        controller.registerPlugin(
          new GoogleCloudAuthPlugin({
            apiToken: key,
            autoRefreshToken: true,
            // Declined deliberately: the recommended settings pin errorTarget
            // at 20, which is right for flying a city and is what made one
            // building read as mush.
            useRecommendedSettings: false,
          }),
        );
        controller.errorTarget = ERROR_TARGET_PX;
        // Order matters, and silently: `setResolutionFromRenderer` returns
        // false for a camera that has not been registered yet, and it is the
        // resolution that makes screen-space error computable. Called the other
        // way round the tileset never refines past its root -- which is a
        // low-resolution globe, so the panel showed a blurry blue sphere and
        // credited it to bathymetry surveys. Register the camera first.
        controller.setCamera(camera);
        if (!controller.setResolutionFromRenderer(camera, renderer)) {
          // Never seen once the order above is right, and worth failing loudly
          // rather than rendering an unrefined globe that looks like a bug in
          // the imagery instead of a bug here.
          throw new Error('the tile renderer refused the camera resolution');
        }
        scene.add(controller.group);

        // Put the requested building at the origin.
        //
        // The tileset is earth-centred: every vertex is in ECEF metres, so
        // without this the structure sits six thousand kilometres from the
        // camera. `getObjectFrame` builds the east-north-up basis at a
        // coordinate; inverting it brings that point to the origin with local
        // up as +Y, which is what makes the camera framing below plain metres.
        const frameMatrix = new THREE.Matrix4();
        controller.ellipsoid.getObjectFrame(
          (latitude * Math.PI) / 180,
          (longitude * Math.PI) / 180,
          0,
          0,
          0,
          0,
          frameMatrix,
        );
        frameMatrix.invert();
        // Decomposed into position/quaternion/scale rather than left as a
        // matrix: the tiles group overrides `updateMatrixWorld`, so a matrix
        // written straight in is a matrix something else owns. The frame is a
        // rigid transform, so the decomposition is exact.
        controller.group.matrix.copy(frameMatrix);
        controller.group.matrix.decompose(
          controller.group.position,
          controller.group.quaternion,
          controller.group.scale,
        );

        camera.position.set(0, CAMERA_HEIGHT_M, CAMERA_DISTANCE_M);
        camera.lookAt(0, 0, 0);

        // Drag to orbit, scroll to zoom. A commander checking which side has
        // the fire escape needs to get round the back, and a fixed camera on a
        // photograph of a building is a postcard.
        const orbit = new OrbitControls(camera, renderer.domElement);
        orbit.target.set(0, 0, 0);
        orbit.enableDamping = true;
        orbit.maxDistance = 600;
        orbit.minDistance = 25;
        // Stop the camera going under the ground, where the tileset is hollow.
        orbit.maxPolarAngle = Math.PI / 2.05;
        orbit.update();

        scene.add(new THREE.AmbientLight(0xffffff, 1.6));
        const sun = new THREE.DirectionalLight(0xffffff, 1.2);
        sun.position.set(1, 2, 3);
        scene.add(sun);

        let announced = false;
        const startedAt = performance.now();
        const frame = () => {
          orbit.update();
          camera.updateMatrixWorld();
          controller.update();
          renderer.render(scene, camera);

          // Ready means "the picture has stopped improving", not "a frame was
          // drawn". Announcing on the first frame marked an empty canvas ready
          // and gave a screenshot nothing to wait for.
          const stats = controller.stats;
          const settled =
            stats !== undefined &&
            stats.loaded > 0 &&
            stats.downloading === 0 &&
            stats.parsing === 0;
          if (!announced && (settled || performance.now() - startedAt > SETTLE_TIMEOUT_MS)) {
            announced = true;
            setStatus('ready');
            node.setAttribute('data-tiles', settled ? 'settled' : 'timeout');
          }

          // Attribution is required and is *not* static: it names whoever
          // captured the tiles currently on screen, so it changes as the camera
          // moves. Read every frame, published when it changes.
          const target: { value: string }[] = [];
          controller.getAttributions?.(target);
          const credit = target.map((entry) => entry.value).join(' · ');
          if (credit) setAttribution((current) => (current === credit ? current : credit));
          raf = requestAnimationFrame(frame);
        };
        raf = requestAnimationFrame(frame);

        const onResize = () => {
          const w = node.clientWidth || width;
          const h = node.clientHeight || height;
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
          renderer.setSize(w, h);
          // Screen-space error is computed against this; a resized panel that
          // kept the old resolution would stop refining at the new size.
          controller.setResolutionFromRenderer(camera, renderer);
        };
        window.addEventListener('resize', onResize);

        cleanup = () => {
          orbit.dispose();
          window.removeEventListener('resize', onResize);
          if (raf !== null) cancelAnimationFrame(raf);
          controller.dispose();
          renderer.dispose();
          if (renderer.domElement.parentElement === node) {
            node.removeChild(renderer.domElement);
          }
        };
      })
      .catch((error: unknown) => {
        if (disposed) return;
        // Loudly: a tile failure is a configuration problem nine times in ten
        // -- a key with the wrong API enabled, or a referrer restriction that
        // does not include this origin -- and none of that is guessable from
        // a grey box.
        console.error('[firstdue] photorealistic tiles failed', error);
        setFailure(error instanceof Error ? error.message : String(error));
        setStatus('failed');
      });

    return () => {
      disposed = true;
      if (raf !== null) cancelAnimationFrame(raf);
      cleanup?.();
    };
  }, [latitude, longitude, label, geometryState]);

  /**
   * The line under the canvas, or null when there is nothing to say.
   *
   * Every branch below still renders the mount node. That is the whole point:
   * returning a bare paragraph for the error states meant `mount.current` was
   * null on the render that followed, so when coordinates finally arrived the
   * effect re-ran, found no node to draw into, and returned without setting a
   * status. The message then stayed on screen for ever, describing a failure
   * that had already resolved. A component that reports its own state must not
   * unmount the thing that clears it.
   */
  const note = (): { text: string; tone: string } | null => {
    switch (status) {
      case 'no-key':
        return {
          text: 'Photorealistic view UNCONFIGURED — NEXT_PUBLIC_GOOGLE_MAPS_API_KEY is not set on the console process, so no tile provider was contacted.',
          tone: 'text-muted',
        };
      case 'no-coordinates':
        return {
          text: 'Photorealistic view UNAVAILABLE — the backend returned this structure without coordinates, so there is no place to point a camera.',
          tone: 'text-muted',
        };
      case 'failed':
        return {
          text: `Photorealistic view UNAVAILABLE — the tile service could not be reached${failure ? `: ${failure}` : ''}.`,
          tone: 'text-alarm',
        };
      case 'loading':
        return geometryState === 'unavailable'
          ? {
              text: 'Photorealistic view UNAVAILABLE — no geometry for this structure reached the console.',
              tone: 'text-muted',
            }
          : { text: 'Locating the structure…', tone: 'text-muted' };
      default:
        return null;
    }
  };

  const message = note();
  const drawing = status === 'ready';

  return (
    <div className="relative">
      {/* Taller than the massing model beside it, on purpose: screen space is
          what decides how many tiles get drawn, so a bigger canvas is a
          sharper building rather than merely a larger one. */}
      <div
        ref={mount}
        className={`h-[26rem] w-full bg-ground ${drawing ? '' : 'border border-dashed border-line'}`}
      />
      {message && <p className={`mt-2 text-body ${message.tone}`}>{message.text}</p>}
      {/* Google requires the data attribution to be visible over the imagery. */}
      {drawing && <p className="mt-1 text-micro text-muted">{attribution || 'Google'}</p>}
    </div>
  );
}
