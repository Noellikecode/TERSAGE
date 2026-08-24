/**
 * The demo runs itself in standby, and only in fake mode.
 *
 * The gate is the point of this file. Auto-dispatch invents a 911 call, which
 * is acceptable in a credential-free demo and would be the worst thing this
 * software could do on a real deployment. So the check is positive -- the
 * backend must call itself `fake` -- rather than an absence of a live flag, and
 * these tests hold that shape rather than the behaviour it happens to produce.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CommandCenter } from '@/components/CommandCenter';
import {
  AGENTS,
  DECISIONS,
  EVENTS,
  QUEUE,
  STATS,
  STATUS,
  SUBSCRIPTIONS,
} from './fixtures';

function renderAt(mode: 'fake' | 'live' | null) {
  return render(
    <CommandCenter
      status={mode === null ? null : { ...STATUS, mode }}
      readiness={null}
      error={null}
      initialStats={STATS}
      initialQueue={QUEUE}
      initialAgents={AGENTS}
      initialSubscriptions={SUBSCRIPTIONS}
      initialEvents={EVENTS}
      initialDecisions={DECISIONS}
      forceSvgGeometry
    />,
  );
}

/** Every POST the console made, so a dispatch is detectable by path. */
function postedPaths(): string[] {
  const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  return calls
    .filter((call) => (call[1] as RequestInit | undefined)?.method === 'POST')
    .map((call) => String(call[0]));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // Path-aware, because a blanket `{}` writes a non-array into the events and
  // decisions state and the fleet then crashes on `.filter`.
  globalThis.fetch = vi.fn(async (input: unknown) => {
    const url = String(input);
    let body: unknown = {};
    if (url.includes('/audit/events')) body = EVENTS;
    else if (url.includes('/audit/decisions')) body = DECISIONS;
    else if (url.includes('/queue')) body = QUEUE;
    else if (url.includes('/stats')) body = STATS;
    else if (url.includes('/fire-activity')) body = { available: false, unavailable_reason: 'no key' };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('auto-dispatch is gated on the backend calling itself fake', () => {
  it('never opens an incident when the backend reports live mode', async () => {
    renderAt('live');
    await vi.advanceTimersByTimeAsync(120_000);
    expect(postedPaths().filter((p) => p.includes('/incidents'))).toHaveLength(0);
  });

  it('never opens an incident when the mode is unknown', async () => {
    // An unread status is not permission. A console that dispatched while it
    // did not yet know what it was talking to would be dispatching on a guess.
    renderAt(null);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(postedPaths().filter((p) => p.includes('/incidents'))).toHaveLength(0);
  });

  it('warns before it dispatches, so the transition is never a snap', async () => {
    renderAt('fake');
    await vi.advanceTimersByTimeAsync(45_000);
    await waitFor(() =>
      expect(screen.getByText(/Simulated 911 call arriving/)).toBeInTheDocument(),
    );
  });

  it('stands down for the rest of the session when the viewer says so', async () => {
    renderAt('fake');
    await vi.advanceTimersByTimeAsync(45_000);
    const stay = await waitFor(() => screen.getByRole('button', { name: /Stay in standby/ }));
    stay.click();
    await vi.advanceTimersByTimeAsync(180_000);
    expect(postedPaths().filter((p) => p.includes('/incidents'))).toHaveLength(0);
  });
});

describe('standby does real work on its own', () => {
  it('runs slow-loop passes on an interval, in fake mode and in live mode', async () => {
    // A pass is ordinary scheduled work, not a simulated event, so it is not
    // behind the fake-mode gate -- in production a scheduler drives the same
    // endpoint.
    renderAt('live');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(postedPaths().filter((p) => p.includes('/poll')).length).toBeGreaterThan(0);
  });
});
