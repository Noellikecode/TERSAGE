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

/**
 * How long to wait before re-asking for a snapshot that carried nothing new.
 *
 * `GET /incidents/{id}/stream` is not a long-lived socket. It yields the
 * emissions the log already holds and *returns*, so the connection ends
 * immediately -- on every connection, for an open incident and a closed one
 * alike. Left to the browser's own `EventSource` retry that becomes an
 * unbounded reconnect loop: the spec's retry has no backoff, no ceiling, and
 * no stop condition, and a clean end-of-stream is precisely the case it
 * treats as "try again". Measured against a held incident id it ran at tens
 * of requests a second and starved the rest of the page -- including the
 * chunk load for the map renderer, which then sat on "Drawing the region..."
 * forever because its `import()` never got a socket.
 *
 * So the hook takes the reconnect back. Same discipline the incident log feed
 * already uses: close what the server ended, wait, and re-ask -- fast while
 * the brief is moving, decaying to a floor while it is not.
 */
const BRIEF_POLL_MS = 1000;
const BRIEF_POLL_CEILING_MS = 5000;
const BRIEF_POLL_DECAY = 1.6;

/** A connection that *failed* rather than ended. Backed off harder, and capped. */
const BRIEF_RECONNECT_BASE_MS = 3000;
const BRIEF_RECONNECT_CEILING_MS = 30_000;

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

    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    /** Highest version this hook has taken, so a re-open resumes rather than replays. */
    let after = -1;
    /** Frames the currently open socket delivered, to tell a productive snapshot from an empty one. */
    let delivered = 0;
    /** Consecutive hard failures, for the backoff exponent. */
    let attempt = 0;
    let quiet = BRIEF_POLL_MS;

    const open = () => {
      if (stopped) return;
      delivered = 0;
      const resume = after >= 0 ? `?after_version=${after}` : '';
      const source = new EventSource(
        gatewayPath(`/api/v1/incidents/${incidentId}/stream${resume}`),
      );
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
          // A frame means the path is working, so the next failure starts its
          // backoff from the bottom rather than from where the last one ended.
          attempt = 0;
          delivered += 1;
          if (parsed.version > after) after = parsed.version;
          setState('open');
          setEmissions((current) => applyEmission(current, parsed));
        } catch {
          setRejected((count) => count + 1);
        }
      });

      // One handler, two completely different events.
      //
      // `readyState` separates them. CLOSED means the connection *failed* --
      // a non-2xx, or a 2xx with the wrong MIME, which is what this console's
      // gateway returns when the backend is unreachable. The browser will
      // never retry that one, so this hook must. Anything else is the ordinary
      // end of a snapshot, and the browser is already about to retry it -- so
      // the socket is closed first, to take that retry away from it.
      // Both `onerror` and an `error` listener are wired, because the two are
      // not interchangeable across the browsers and the test doubles this runs
      // against. A real browser fires both, so this must do its work once:
      // scheduling twice would open two sockets and rebuild the loop it exists
      // to remove.
      let settled = false;
      const ended = () => {
        if (stopped || settled) return;
        settled = true;
        const failed = source.readyState === (source.constructor as typeof EventSource).CLOSED;
        source.close();
        if (sourceRef.current === source) sourceRef.current = null;
        setState((previous) => (previous === 'open' || delivered > 0 ? 'reconnecting' : 'failed'));
        if (failed) {
          const wait = Math.min(
            BRIEF_RECONNECT_CEILING_MS,
            BRIEF_RECONNECT_BASE_MS * 2 ** attempt,
          );
          attempt += 1;
          retry = setTimeout(open, wait);
          return;
        }
        // The ordinary end. A snapshot that carried something resets to the
        // fast cadence, because the brief is moving and that is the whole
        // reason for asking quickly. The decay is applied after scheduling, so
        // the first re-ask following any close is the fast one and only a run
        // of empty snapshots slows down.
        if (delivered) quiet = BRIEF_POLL_MS;
        retry = setTimeout(open, quiet);
        if (!delivered) {
          quiet = Math.min(BRIEF_POLL_CEILING_MS, Math.round(quiet * BRIEF_POLL_DECAY));
        }
      };

      source.addEventListener('error', ended);
      source.onerror = ended;
    };

    open();

    return () => {
      stopped = true;
      if (retry !== null) clearTimeout(retry);
      sourceRef.current?.close();
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

/**
 * The incident log, as it is written.
 *
 * The brief stream above answers *what does the commander know*. This answers
 * *what is the fleet doing* -- every entry the incident records, in order, as
 * it lands: the intake being read, the focus the head composed, each agent it
 * woke and under which rule, notifications, resolutions, benchmarks.
 *
 * **Resumed by sequence, which is the log's own guarantee.** Entries are
 * monotonic and gapless, so `Last-Event-ID` cannot skip one and cannot replay
 * one twice. That is the same property the brief stream gets from a version
 * number, and it is why the log is the right thing to stream rather than a
 * side-channel invented for the console.
 *
 * Entries accumulate and are never dropped. The log is append-only and the
 * panel above it derives per-agent state from the whole run -- a card showing
 * "what this agent last did" needs the entries before the last one to know it.
 */
export interface IncidentLogEntryFrame {
  sequence: number;
  entry_type: string;
  occurred_at: string;
  /** Agent id to pinned version. How an entry is attributed to an agent. */
  agent_versions: Record<string, string>;
  content_hash: string;
  content: Record<string, unknown>;
}

export interface IncidentLogStream {
  entries: IncidentLogEntryFrame[];
  /** True once a frame has arrived, so an empty log is distinguishable. */
  started: boolean;
}

/**
 * How long to wait before re-opening a stream the browser will not retry.
 *
 * The base sits at the browser's own reconnect cadence (~3s in Chrome, Safari
 * and undici; 5s in Firefox), so a hand-rolled retry on a healthy feed is
 * indistinguishable from the one the browser was already doing. It doubles from
 * there and stops at the ceiling, because a gateway that is refusing every
 * request must not be asked four times a second for the length of an incident.
 */
const RECONNECT_BASE_MS = 3000;
const RECONNECT_CEILING_MS = 30_000;

/**
 * How soon a *healthy* snapshot is asked for again.
 *
 * This is the tempo of the whole panel, and it was the browser's: an ordinary
 * end-of-snapshot was left to `EventSource`'s own reconnect, which is ~3s in
 * Chrome and Safari and 5s in Firefox. The incident loop records something
 * every few hundred milliseconds during a sweep, so what a commander saw was
 * eight actions appearing at once, then nothing for three seconds, then eight
 * more -- the fleet working in stop-motion, at a cadence set by a spec default
 * that has nothing to do with this incident.
 *
 * So the ordinary close is taken back from the browser. It costs one request
 * per interval and almost no bytes, because the reopen carries `after_sequence`
 * and the backend answers with what has been appended since -- which is usually
 * nothing at all.
 */
const SNAPSHOT_POLL_MS = 800;

/**
 * How far the healthy poll decays while the log is quiet, and how fast.
 *
 * An incident is bounded at two minutes but a console is not: the card stays on
 * screen afterwards, and a fixed 800 ms poll would ask a closed incident for
 * new entries a hundred thousand times overnight. Each empty snapshot slows the
 * next one by a step until it reaches the browser's own cadence, which is where
 * this started and is a perfectly good rate for a log nobody is writing to. One
 * frame resets it -- the fleet has started working again, and that is exactly
 * when the fast cadence is worth paying for.
 */
const SNAPSHOT_POLL_CEILING_MS = 3000;
const SNAPSHOT_POLL_DECAY = 1.5;

export function useIncidentLogStream(incidentId: string | null): IncidentLogStream {
  const [entries, setEntries] = useState<IncidentLogEntryFrame[]>([]);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    setEntries([]);
    setStarted(false);
    // Same guard as the streams above: no incident, no window, or a runtime
    // with no `EventSource` simply gets no feed. Nothing else depends on it.
    if (!incidentId || typeof window === 'undefined' || typeof window.EventSource !== 'function') {
      return;
    }

    // Re-opened by hand, because `EventSource`'s own retry is fail-permanent.
    //
    // The backend's log stream is snapshot-and-close: it sends what the log
    // holds and ends. The browser treats that as a disconnect and reconnects
    // with `Last-Event-ID`, which is the polling loop this feed actually runs
    // on -- so the retry is not an edge case here, it is the mechanism.
    //
    // But the spec only schedules a retry when the connection *drops*. A
    // response that is non-2xx, or 2xx with the wrong MIME, **fails** the
    // connection instead: `readyState` goes to CLOSED and the browser never
    // tries again. This console's own gateway produces exactly that shape --
    // a JSON error envelope -- on an unreachable backend, an unavailable
    // credential, or any upstream non-200 such as a 401 on a grant that has
    // not landed yet. One of those, once, and the feed is dead for the rest of
    // the incident: no agent cards, no log, no entry package. And it looked
    // identical to a quiet fireground, because the old handler could not tell
    // the two apart and deliberately said nothing about either.
    //
    // So a close that the browser will not retry is retried here, on a backoff
    // that starts near the browser's own ~3s cadence.
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;
    // The highest sequence this console holds. Sent as `after_sequence` on
    // every reopen, so a poll of a log that has not moved transfers nothing --
    // which is what makes polling it four times a second affordable. It is
    // *not* the same mechanism as `Last-Event-ID`: that is per-socket state the
    // browser keeps for its own retries, and a socket this hook opens by hand
    // starts with none. Both resume points mean the same thing to the backend.
    let after = -1;
    // Frames delivered by the socket currently open, so an empty snapshot can
    // be told from a productive one without reaching into React state.
    let delivered = 0;
    let quiet = SNAPSHOT_POLL_MS;

    const open = () => {
      if (stopped) return;
      delivered = 0;
      const resume = after >= 0 ? `?after_sequence=${after}` : '';
      source = new EventSource(
        gatewayPath(`/api/v1/incidents/${incidentId}/log/stream${resume}`),
      );
      wire(source);
    };

    const reopen = () => {
      if (stopped) return;
      // Backed off, and capped: a gateway that is refusing every request must
      // not be asked four times a second for the length of an incident.
      const wait = Math.min(RECONNECT_CEILING_MS, RECONNECT_BASE_MS * 2 ** attempt);
      attempt += 1;
      retry = setTimeout(open, wait);
    };

    /** The ordinary end of a snapshot: ask again, soon, and cheaply. */
    const poll = () => {
      if (stopped) return;
      // A snapshot that carried anything at all resets to the fast cadence,
      // because the fleet is working and that is the whole reason for it. The
      // decay is applied *after* scheduling, so the first re-ask following any
      // close is always the fast one and only a run of empty ones slows down.
      if (delivered) quiet = SNAPSHOT_POLL_MS;
      retry = setTimeout(open, quiet);
      if (!delivered) {
        quiet = Math.min(SNAPSHOT_POLL_CEILING_MS, Math.round(quiet * SNAPSHOT_POLL_DECAY));
      }
    };

    const wire = (source: EventSource) => {
    source.addEventListener('entry', (event) => {
      try {
        const frame = JSON.parse((event as MessageEvent).data) as IncidentLogEntryFrame;
        if (typeof frame.sequence !== 'number') return;
        // A frame means the path is working again, so the next failure starts
        // its backoff from the bottom rather than from where the last one
        // ended -- otherwise one bad minute permanently slows a healthy feed.
        attempt = 0;
        delivered += 1;
        if (frame.sequence > after) after = frame.sequence;
        setStarted(true);
        setEntries((current) => {
          // The stream reconnects and replays from `Last-Event-ID`, and a
          // reconnect that raced an append can deliver one twice. Sequence is
          // the log's identity, so a duplicate is dropped rather than drawn as
          // a second step the agent did not take.
          if (current.some((e) => e.sequence === frame.sequence)) return current;
          return [...current, frame].sort((a, b) => a.sequence - b.sequence);
        });
      } catch {
        // A malformed frame is dropped. The log document endpoint remains the
        // record; this is a view of it.
      }
    });

    source.onerror = () => {
      // `readyState` separates the two completely different things that arrive
      // on this one handler.
      //
      // CLOSED is the fail-permanent case -- a non-2xx, or 2xx with the wrong
      // MIME, which this console's gateway produces on an unreachable backend
      // or an unavailable credential. The browser will never try again, so the
      // backoff above is the only thing that reopens it.
      //
      // CONNECTING is the ordinary end of a snapshot, and the browser *has*
      // scheduled its own retry -- in ~3s, a spec default that has nothing to
      // do with how fast this incident is recording. `close()` cancels that
      // retry before anything is scheduled beside it, so there is exactly one
      // socket at a time and no frame arrives twice; then it is reopened on
      // this hook's own cadence instead of the browser's.
      if (source.readyState === EventSource.CLOSED) {
        source.close();
        reopen();
        return;
      }
      source.close();
      poll();
    };
    };

    open();

    return () => {
      stopped = true;
      if (retry !== undefined) clearTimeout(retry);
      source?.close();
    };
  }, [incidentId]);

  return { entries, started };
}
