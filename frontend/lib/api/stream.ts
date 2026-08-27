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

/**
 * The prose being written, token by token.
 *
 * The backend has streamed this since the reconciler was built -- `GET
 * /brief/stream-enriched` yields provisional `narrative` frames as Gemini
 * composes and a final persisted `brief` frame at the end. The console asked
 * for prose with a blocking POST instead and rendered the finished paragraph in
 * one go, which is why the brief read as arriving in slabs: the one part of it
 * that genuinely is written over time was being waited for in silence.
 *
 * Two rules, both from the frame contract:
 *
 * - **Narrative frames are provisional.** They carry no version, no content
 *   hash and nothing the incident log stores, so they are held separately from
 *   emissions and are never merged into one. The panel renders them as prose in
 *   progress and the persisted emission replaces them when it lands.
 * - **A chunk for a version we have moved past is dropped.** `for_version` says
 *   which emission the prose belongs to; a late chunk from an earlier
 *   composition must not append itself to a newer one.
 *
 * Chunks are deltas, not snapshots -- the backend accumulates and joins them --
 * so they are appended here in arrival order.
 */
export interface NarrativeStream {
  /** Prose composed so far. Empty before the first chunk. */
  text: string;
  /** The emission version this prose is being written for. */
  forVersion: number;
  /** True while chunks are still arriving. */
  writing: boolean;
}

export function useNarrativeStream(incidentId: string | null): NarrativeStream {
  const [text, setText] = useState('');
  const [forVersion, setForVersion] = useState(0);
  const [writing, setWriting] = useState(false);

  useEffect(() => {
    setText('');
    setForVersion(0);
    setWriting(false);
    // Same guard as the brief stream above: no incident, no window, or a
    // runtime with no `EventSource` (jsdom, and any server render) simply gets
    // no prose. The deterministic brief does not depend on it.
    if (!incidentId || typeof window === 'undefined' || typeof window.EventSource !== 'function') {
      return;
    }

    const source = new EventSource(
      gatewayPath(`/api/v1/incidents/${incidentId}/brief/stream-enriched`),
    );
    let done = false;
    setWriting(true);

    source.addEventListener('narrative', (event) => {
      if (done) return;
      try {
        const chunk = JSON.parse((event as MessageEvent).data) as {
          text?: string;
          for_version?: number;
        };
        const version = chunk.for_version ?? 0;
        setForVersion((current) => {
          // A chunk for an older composition is dropped rather than appended.
          if (version < current) return current;
          if (version > current) setText('');
          return version;
        });
        setText((current) => current + (chunk.text ?? ''));
      } catch {
        // A malformed frame is dropped. Provisional prose is not the record,
        // and half a sentence is not worth failing the panel over.
      }
    });

    // The composition finished. The persisted emission arrives on the brief
    // stream; this only stops the cursor.
    source.addEventListener('brief', () => {
      done = true;
      setWriting(false);
      source.close();
    });

    source.onerror = () => {
      // Enrichment is one pass, and the endpoint closes when it ends. An error
      // after `done` is that ordinary close; before it, the prose is simply
      // unavailable and the deterministic brief is unaffected.
      setWriting(false);
      source.close();
    };

    return () => {
      done = true;
      source.close();
    };
  }, [incidentId]);

  return { text, forVersion, writing };
}
