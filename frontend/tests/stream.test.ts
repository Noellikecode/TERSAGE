/**
 * The brief stream's two rules.
 *
 * Nothing is rendered that is not in the log, and the screen never goes
 * backwards.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { applyEmission, useBriefStream } from '@/lib/api/stream';
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
