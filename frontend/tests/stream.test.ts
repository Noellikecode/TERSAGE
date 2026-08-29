/**
 * The brief stream's two rules.
 *
 * Nothing is rendered that is not in the log, and the screen never goes
 * backwards.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { applyEmission, useBriefStream, useIncidentLogStream } from '@/lib/api/stream';
import { emission } from './fixtures';

describe('applying a streamed emission', () => {
  it('keeps versions in order however they arrive', () => {
    const three = emission({ version: 3, stage: 'AMENDMENT' });
    const one = emission({ version: 1 });
    const two = emission({ version: 2, stage: 'ENRICHED' });

    let state = applyEmission([], three);
    state = applyEmission(state, one);
    state = applyEmission(state, two);

    expect(state.map((e) => e.version)).toEqual([1, 2, 3]);
  });

  it('drops an emission that is not in the log', () => {
    const unpersisted = emission({ version: 2, persisted_at: null });
    expect(applyEmission([], unpersisted)).toEqual([]);
  });

  it('ignores a redelivered version rather than duplicating it', () => {
    const first = applyEmission([], emission({ version: 1 }));
    const again = applyEmission(first, emission({ version: 1, emission_id: 'other' }));
    expect(again).toHaveLength(1);
    expect(again[0]!.emission_id).toBe('emission-1');
  });

  it('never rewinds the commander’s screen on reconnect', () => {
    let state = applyEmission([], emission({ version: 1 }));
    state = applyEmission(state, emission({ version: 2, stage: 'ENRICHED' }));
    // A resume that redelivers version 1 must not become the latest.
    state = applyEmission(state, emission({ version: 1 }));
    expect(state[state.length - 1]!.version).toBe(2);
  });
});


/** A stand-in for the browser's EventSource that lets a test push frames. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners: Record<string, ((event: unknown) => void)[]> = {};
  closed = false;

  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: unknown) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() {
    this.closed = true;
  }

  /** Deliver one `brief` frame, the way the gateway does. */
  send(payload: unknown) {
    for (const handler of this.listeners.brief ?? []) {
      handler({ data: JSON.stringify(payload) });
    }
  }
}

afterEach(() => {
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
});

describe('a new incident is a new brief', () => {
  it('does not carry the previous incident’s emissions into the next one', () => {
    // The failure this prevents put one building's construction, height and
    // occupancy on screen under a different building's address. Versions
    // restart at 1 per incident and `applyEmission` drops a version it has
    // already seen, so the second incident's frames were swallowed as
    // redeliveries and the panel never updated at all.
    vi.stubGlobal('EventSource', FakeEventSource);

    const { result, rerender } = renderHook(({ id }) => useBriefStream(id), {
      initialProps: { id: 'incident-first' as string | null },
    });

    act(() => {
      FakeEventSource.instances[0]!.send(emission({ version: 1 }));
    });
    expect(result.current.emissions).toHaveLength(1);

    rerender({ id: 'incident-second' });
    // Cleared the moment the incident changes, before any frame arrives.
    expect(result.current.emissions).toHaveLength(0);

    act(() => {
      FakeEventSource.instances[1]!.send(emission({ version: 1 }));
    });
    // The new incident's version 1 lands. It used to be discarded.
    expect(result.current.emissions).toHaveLength(1);
  });

  it('closes the old stream when the incident changes', () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const { rerender } = renderHook(({ id }) => useBriefStream(id), {
      initialProps: { id: 'incident-first' as string | null },
    });
    rerender({ id: 'incident-second' });
    expect(FakeEventSource.instances[0]!.closed).toBe(true);
  });

  it('leaves no brief on screen when the incident closes', () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const { result, rerender } = renderHook(({ id }) => useBriefStream(id), {
      initialProps: { id: 'incident-first' as string | null },
    });
    act(() => {
      FakeEventSource.instances[0]!.send(emission({ version: 1 }));
    });
    rerender({ id: null });
    expect(result.current.emissions).toEqual([]);
    expect(result.current.latest).toBeNull();
  });
});

/**
 * A stream that can fail the way a gateway actually fails it.
 *
 * `EventSource` retries a dropped connection, and that retry is the mechanism
 * the incident log feed runs on — the backend sends a snapshot and ends. But a
 * response that is non-2xx, or 2xx with the wrong MIME, *fails* the connection
 * instead: `readyState` goes CLOSED and the browser never tries again. The
 * console's own gateway returns a JSON envelope on an unreachable backend, an
 * unavailable credential, or any upstream non-200. One of those, once, and the
 * feed is dead for the whole incident — silently, because it looks exactly like
 * a fireground where nothing is happening.
 */
class FailingEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FailingEventSource[] = [];

  listeners: Record<string, ((event: unknown) => void)[]> = {};
  onerror: (() => void) | null = null;
  readyState = 1;
  closed = false;

  constructor(public url: string) {
    FailingEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: unknown) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() {
    this.closed = true;
  }

  /** The gateway answered with an envelope: closed for good, no retry coming. */
  failPermanently() {
    this.readyState = FailingEventSource.CLOSED;
    this.onerror?.();
  }

  /** The ordinary end of a snapshot: the browser is already reconnecting. */
  endNormally() {
    this.readyState = FailingEventSource.CONNECTING;
    this.onerror?.();
  }

  sendEntry(payload: unknown) {
    for (const handler of this.listeners.entry ?? []) {
      handler({ data: JSON.stringify(payload) });
    }
  }
}

describe('an incident log feed the browser has given up on', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    FailingEventSource.instances = [];
    vi.stubGlobal('EventSource', FailingEventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function entry(sequence: number) {
    return {
      sequence,
      entry_type: 'ENTRY_PACKAGE',
      occurred_at: '2026-08-28T09:00:00Z',
      agent_versions: {},
      content_hash: 'h',
      content: {},
    };
  }

  it('re-opens a stream that closed for good, rather than going quiet forever', async () => {
    const { result } = renderHook(() => useIncidentLogStream('inc_1'));
    expect(FailingEventSource.instances).toHaveLength(1);

    await act(async () => {
      FailingEventSource.instances[0]!.failPermanently();
      await vi.advanceTimersByTimeAsync(3500);
    });

    // A second socket, because nothing else was ever going to open one.
    expect(FailingEventSource.instances.length).toBeGreaterThan(1);

    await act(async () => {
      FailingEventSource.instances[1]!.sendEntry(entry(7));
    });
    expect(result.current.entries.map((e) => e.sequence)).toEqual([7]);
  });

  it('re-asks for the snapshot within a second, rather than at the browser’s pace', async () => {
    // The ordinary end of a snapshot. The browser would retry in ~3s, which is
    // a spec default with nothing to do with how fast this incident is
    // recording -- and during a sweep that turned a continuous log into eight
    // cards every three seconds. The close is taken back and re-asked on this
    // hook's own cadence.
    renderHook(() => useIncidentLogStream('inc_1'));
    await act(async () => {
      FailingEventSource.instances[0]!.endNormally();
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(FailingEventSource.instances).toHaveLength(2);
    // Exactly one socket at a time: the browser's own retry was cancelled by
    // `close()` before this one was scheduled, so no frame can arrive twice.
    expect(FailingEventSource.instances[0]!.closed).toBe(true);
  });

  it('resumes by sequence, so a poll of an unchanged log transfers nothing', async () => {
    renderHook(() => useIncidentLogStream('inc_1'));
    expect(FailingEventSource.instances[0]!.url).not.toContain('after_sequence');

    await act(async () => {
      FailingEventSource.instances[0]!.sendEntry(entry(11));
      FailingEventSource.instances[0]!.endNormally();
      await vi.advanceTimersByTimeAsync(900);
    });
    // A socket this hook opened by hand carries no `Last-Event-ID` -- that is
    // per-socket state the browser keeps for its own retries -- so the resume
    // point travels in the query instead.
    expect(FailingEventSource.instances[1]!.url).toContain('after_sequence=11');
  });

  it('slows down while the log is quiet, and speeds back up when it moves', async () => {
    // A console outlives the incident on its screen. A fixed sub-second poll
    // would ask a closed incident for new entries all night.
    renderHook(() => useIncidentLogStream('inc_1'));
    for (let i = 0; i < 6; i += 1) {
      await act(async () => {
        FailingEventSource.instances.at(-1)!.endNormally();
        await vi.advanceTimersByTimeAsync(3100);
      });
    }
    const quiet = FailingEventSource.instances.length;

    // One frame, and the feed is fast again: the fleet is working, which is the
    // whole reason for the fast cadence.
    await act(async () => {
      FailingEventSource.instances.at(-1)!.sendEntry(entry(3));
      FailingEventSource.instances.at(-1)!.endNormally();
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(FailingEventSource.instances.length).toBe(quiet + 1);
  });

  it('backs off rather than hammering a gateway that is refusing everything', async () => {
    renderHook(() => useIncidentLogStream('inc_1'));
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        FailingEventSource.instances.at(-1)!.failPermanently();
        await vi.advanceTimersByTimeAsync(60_000);
      });
    }
    // Five sockets over four failures, not one per tick of the clock.
    expect(FailingEventSource.instances).toHaveLength(5);
  });

  it('schedules nothing more once the incident is gone', async () => {
    const { unmount } = renderHook(() => useIncidentLogStream('inc_1'));
    await act(async () => {
      FailingEventSource.instances[0]!.failPermanently();
    });
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    // The pending retry was cancelled; nothing opened into a torn-down console.
    expect(FailingEventSource.instances).toHaveLength(1);
  });
});

/**
 * The brief stream, when the server has said everything it has.
 *
 * `GET /incidents/{id}/stream` is not a long-lived socket. It yields the
 * emissions the log already holds and returns -- so the connection ends
 * immediately, every time, for an open incident and a closed one alike. That
 * is the same shape the incident log feed has, and `useIncidentLogStream`
 * already answers it correctly: it takes the socket back, waits, and re-asks
 * on a decaying schedule with a ceiling.
 *
 * `useBriefStream` did not. It opened a bare `EventSource` and left the
 * *browser's* automatic retry to do the reconnecting -- and that retry has no
 * backoff, no ceiling, and no stop condition. Against an endpoint that closes
 * on every connection, the result is an unbounded reconnect loop for as long
 * as the console holds an incident id: measured at tens of requests per
 * second, which starves every other request the page needs, including the
 * chunk load for the map renderer.
 *
 * So the rule these tests hold: **the hook owns the reconnect, not the
 * browser.** A snapshot that ends is closed by us and re-asked on our clock.
 */
class SnapshotEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: SnapshotEventSource[] = [];

  listeners: Record<string, ((event: unknown) => void)[]> = {};
  onerror: (() => void) | null = null;
  readyState = 1;
  closed = false;

  constructor(public url: string) {
    SnapshotEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: unknown) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() {
    this.closed = true;
  }

  send(payload: unknown) {
    for (const handler of this.listeners.brief ?? []) {
      handler({ data: JSON.stringify(payload) });
    }
  }

  /** The server sent what it had and ended. The browser is about to retry. */
  endNormally() {
    this.readyState = SnapshotEventSource.CONNECTING;
    this.onerror?.();
    for (const handler of this.listeners.error ?? []) handler({});
  }
}

describe('the brief stream owns its own reconnect', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    SnapshotEventSource.instances = [];
    vi.stubGlobal('EventSource', SnapshotEventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('closes the socket when the snapshot ends, so the browser stops retrying', () => {
    // The whole bug in one assertion. An open socket left to the browser is a
    // reconnect loop nobody is throttling.
    renderHook(() => useBriefStream('inc_1'));
    act(() => {
      SnapshotEventSource.instances[0]!.endNormally();
    });
    expect(SnapshotEventSource.instances[0]!.closed).toBe(true);
  });

  it('does not re-open in the same tick', async () => {
    renderHook(() => useBriefStream('inc_1'));
    act(() => {
      SnapshotEventSource.instances[0]!.endNormally();
    });
    // Still one. A second socket here is the storm.
    expect(SnapshotEventSource.instances).toHaveLength(1);
  });

  it('re-asks after a wait, so the feed still moves', async () => {
    renderHook(() => useBriefStream('inc_1'));
    await act(async () => {
      SnapshotEventSource.instances[0]!.endNormally();
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(SnapshotEventSource.instances.length).toBeGreaterThan(1);
  });

  it('stays bounded over a quiet minute rather than hammering the gateway', async () => {
    // An incident that is open but not emitting is the ordinary case between
    // amendments. Sixty seconds of it must not cost hundreds of requests.
    renderHook(() => useBriefStream('inc_1'));
    await act(async () => {
      for (let i = 0; i < 60; i += 1) {
        const latest = SnapshotEventSource.instances[SnapshotEventSource.instances.length - 1]!;
        latest.endNormally();
        await vi.advanceTimersByTimeAsync(1000);
      }
    });
    expect(SnapshotEventSource.instances.length).toBeLessThanOrEqual(40);
  });

  it('cancels a pending re-open when the incident goes away', async () => {
    const { rerender } = renderHook(({ id }) => useBriefStream(id), {
      initialProps: { id: 'inc_1' as string | null },
    });
    act(() => {
      SnapshotEventSource.instances[0]!.endNormally();
    });
    rerender({ id: null });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // Nothing opened into a console that has no incident.
    expect(SnapshotEventSource.instances).toHaveLength(1);
  });
});
