'use client';

/**
 * A photograph of the incident address, beside the massing model.
 *
 * The massing model is a *computed* structure: extruded from the geometry spec
 * the backend derived from permits and lidar. It is exact about the things the
 * record knows and silent about everything else. A photograph is the opposite --
 * it says nothing about storey counts and everything about which door faces the
 * street, where the fire escape is, and what the building actually looks like
 * when the first engine turns the corner. Neither one replaces the other, so
 * they sit side by side.
 *
 * Three states, all of them honest:
 *
 * - **No incident.** An empty panel that says a photograph appears at dispatch,
 *   rather than a grey box that reads as a building with no features.
 * - **`available: false`.** The backend answers 200 with a reason -- no key
 *   configured, no coverage at that address, provider refused. That is an
 *   answer, not an error, and the reason is what gets rendered.
 * - **The request itself failed.** A different thing entirely: the console
 *   could not ask. Said in those words, never folded into "no imagery".
 *
 * Attribution is rendered visibly whenever an image is. Street-level imagery
 * providers require the credit to appear with the photograph; an image shown
 * without it is a licence violation, so the two are emitted together and there
 * is no branch that renders one without the other.
 */

import { useEffect, useState } from 'react';

import { browserGet } from '@/lib/api/client';

/**
 * The imagery response.
 *
 * Declared here rather than in `lib/api/types.ts` because the endpoint is
 * landing alongside this panel; move it there once the backend view types are
 * regenerated.
 */
export interface BuildingImageryView {
  address_id: string;
  available: boolean;
  provider: string | null;
  content_type: string | null;
  /** A `data:` URI. The bytes come through the gateway; the browser never
   *  talks to the imagery provider directly. */
  data_url: string | null;
  attribution: string | null;
  captured_hint: string | null;
  unavailable_reason: string | null;
}

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | { kind: 'answered'; imagery: BuildingImageryView };

export function BuildingImagery({ addressId }: { addressId: string | null }) {
  const [state, setState] = useState<State>({ kind: 'idle' });

  useEffect(() => {
    if (!addressId) {
      setState({ kind: 'idle' });
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setState({ kind: 'loading' });

    void (async () => {
      // Through the console's own gateway: the backend credential stays on the
      // server, and so does the imagery provider key behind it.
      const result = await browserGet<BuildingImageryView>(
        `/api/v1/buildings/${addressId}/imagery`,
        { signal: controller.signal },
      );
      if (cancelled) return;
      setState(
        result.ok
          ? { kind: 'answered', imagery: result.data }
          : { kind: 'failed', message: result.error.message },
      );
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [addressId]);

  if (state.kind === 'idle') {
    return (
      <p className="border border-dashed border-line p-4 text-micro leading-5 text-muted">
        No incident open. A photograph of the incident address appears here at dispatch.
      </p>
    );
  }

  if (state.kind === 'loading') {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted" role="status">
        Requesting imagery for {addressId}.
      </p>
    );
  }

  if (state.kind === 'failed') {
    return (
      <div className="border border-alarm p-4 text-micro leading-5">
        <p className="text-alarm">Imagery request failed</p>
        <p className="mt-1 text-muted">
          {state.message}. This is the console failing to ask, not the provider
          reporting no coverage.
        </p>
      </div>
    );
  }

  const { imagery } = state;

  // `available` and the bytes have to agree. A true flag with no `data_url` is
  // a backend that promised a photograph it did not send, and rendering an
  // empty frame under it would read as a featureless building.
  if (!imagery.available || !imagery.data_url) {
    return (
      <div className="border border-dashed border-line p-4 text-micro leading-5 text-muted">
        <p className="text-ink">No photograph available</p>
        <p className="mt-1">
          {imagery.unavailable_reason ??
            'The imagery service returned no reason. Treat this as no coverage, not as an empty lot.'}
        </p>
        {imagery.provider && <p className="mt-2 uppercase tracking-wide">Provider {imagery.provider}</p>}
      </div>
    );
  }

  return (
    <figure className="m-0">
      {/* Provider bytes arrive as a data URI through the gateway, so there is
          no remote host for next/image to optimise and no loader to configure. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={imagery.data_url}
        alt={`Street-level photograph of ${imagery.address_id}`}
        className="h-[360px] w-full border border-line bg-surface object-cover"
      />
      <figcaption className="mt-2 space-y-1 text-micro text-muted">
        {/* Required by the imagery provider's terms. Never conditional on
            layout, never truncated away. */}
        {imagery.attribution && (
          <span className="block text-ink" data-testid="imagery-attribution">
            {imagery.attribution}
          </span>
        )}
        {imagery.captured_hint && <span className="block">Captured {imagery.captured_hint}</span>}
        <span className="block">
          A photograph is what the street looked like when it was taken, not what the
          structure is now. The massing model beside it carries the record.
        </span>
      </figcaption>
    </figure>
  );
}
