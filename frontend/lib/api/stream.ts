/**
 * The brief stream.
 *
 * `EventSource` gives reconnect and `Last-Event-ID` resume for free, and the
 * backend keys frames on the brief version -- so a tablet that loses signal
 * reconnects and receives exactly the versions it missed, in order.
 *
 * Two rules this hook enforces on top of that:
 *
 * - **Never render an emission that is not in the log.** Every frame carries
 *   `persisted_at`; one without it is dropped and counted, because a brief the
 *   record does not contain is a brief nobody can account for afterwards.
 * - **Never go backwards.** Frames are applied in version order and a stale
 *   redelivery is ignored, so a reconnect cannot rewind the commander's screen.
 */

'use client';

import { useEffect, useRef, useState } from 'react';

import type { BriefEmissionView } from './types';
import { gatewayPath } from './client';

export type StreamState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'failed';

export interface BriefStream {
  emissions: BriefEmissionView[];
  latest: BriefEmissionView | null;
  state: StreamState;
  /** Frames dropped because they were not persisted. Should always be zero. */
  rejected: number;
}

export function applyEmission(
  current: BriefEmissionView[],
  incoming: BriefEmissionView,
): BriefEmissionView[] {
  if (!incoming.persisted_at) return current;
  if (current.some((e) => e.version === incoming.version)) return current;
  return [...current, incoming].sort((a, b) => a.version - b.version);
}

export function useBriefStream(incidentId: string | null): BriefStream {
  const [emissions, setEmissions] = useState<BriefEmissionView[]>([]);
  const [state, setState] = useState<StreamState>('idle');
  const [rejected, setRejected] = useState(0);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    // A new incident is a new brief, and the old one's frames must go.
    //
    // Versions restart at 1 for every incident, and `applyEmission` drops a
    // version it has already seen -- so carrying the previous incident's
    // emissions across does not merely show the wrong building for a moment.
    // Every frame of the *new* incident is discarded as a redelivery, and the
    // panel keeps rendering the previous building's construction, height and
    // occupancy under the new address's header until the page is reloaded.
    //
    // Clearing on `null` too: closing an incident must not leave the last
    // brief on screen for the next one to inherit.
    setEmissions([]);
    setRejected(0);

    // Checked as a *constructor*, not as a key: a browser that exposes the name
    // without an implementation would otherwise throw here and blank the
    // console at exactly the moment it is needed. Without SSE the brief still
    // shows -- it just does not update in place.
    if (!incidentId || typeof window === 'undefined' || typeof window.EventSource !== 'function') {
      setState('idle');
      return;
    }
    setState('connecting');
    const source = new EventSource(gatewayPath(`/api/v1/incidents/${incidentId}/stream`));
    sourceRef.current = source;

    source.addEventListener('open', () => setState('open'));
    source.addEventListener('brief', (event) => {
      try {
        const parsed = JSON.parse((event as MessageEvent<string>).data) as BriefEmissionView;
        if (!parsed.persisted_at) {
          // Should be impossible: the backend gates on this too. Counted rather
          // than shown, because showing it would be the failure.
          setRejected((count) => count + 1);
          return;
        }
        setState('open');
        setEmissions((current) => applyEmission(current, parsed));
      } catch {
        setRejected((count) => count + 1);
      }
    });
    source.addEventListener('error', () => {
      // EventSource retries on its own and replays from Last-Event-ID.
      setState((previous) => (previous === 'open' ? 'reconnecting' : 'failed'));
    });

    return () => {
      source.close();
      sourceRef.current = null;
      setState('idle');
    };
  }, [incidentId]);

  return {
    emissions,
    latest: emissions[emissions.length - 1] ?? null,
    state,
    rejected,
  };
}
