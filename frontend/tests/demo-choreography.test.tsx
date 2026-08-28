/**
 * The demo runs itself in standby, and only in fake mode.
 *
 * The gate is the point of this file. Auto-dispatch invents a 911 call, which
 * is acceptable in a credential-free demo and would be the worst thing this
 * software could do on a real deployment. So the check is positive -- the
 * backend must call itself `fake` -- rather than an absence of a live flag, and
 * these tests hold that shape rather than the behaviour it happens to produce.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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

/** What `POST /incidents` answers, in the shape the endpoint actually returns. */
const INCIDENT = {
  incident_id: 'incident_demo_1',
  address_id: 'sf-0450-hayes',
  address_display: '450 Hayes St, San Francisco, CA 94102',
  profile_snapshot_id: 'snap_demo',
  grant_id: 'grant_demo',
  cold_start: false,
  opened_at: '2026-08-27T08:00:00Z',
  alarm_level: 2,
};

/**
 * What `POST /incidents/{id}/intake` answers.
 *
 * Modelled separately because the narrative no longer rides along with the
 * open: a `/incidents` catch-all matched the intake path too and answered with
 * the open's shape, and the console died reading `screen_findings` off it --
 * which is the exact failure the 404 branch below exists to prevent.
 */
const INTAKE = {
  incident_id: 'incident_demo_1',
  channel: 'CALL_911',
  source_ref: 'intake/CAD-000001',
  accepted: true,
  rejection_reason: null,
  model_ref: 'fake-extractor@1',
  screen: 'CLEAN',
  screen_findings: [],
  screened: false,
  reported: [],
  unknowns: [],
  fired_rule_ids: [],
  unmatched_rule_ids: [],
  woken: [],
  withheld: [],
};

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

/** The body of the first POST to a path, parsed. */
function postedBody(match: string): Record<string, unknown> | null {
  const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  const call = calls.find(
    (c) =>
      (c[1] as RequestInit | undefined)?.method === 'POST' && String(c[0]).includes(match),
  );
  if (!call) return null;
  const body = (call[1] as RequestInit).body;
  return typeof body === 'string' ? JSON.parse(body) : null;
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
    // Before the general `/incidents` match, which would otherwise swallow it.
    else if (url.includes('/intake')) body = INTAKE;
    else if (url.includes('/incidents')) body = INCIDENT;
    else {
      // 404, not `{}`.
      //
      // An empty object is *truthy*, so a console reading `geometry.spec` off
      // one dies on a field the API guarantees. The endpoints this harness does
      // not model are ones that legitimately 404 -- a building with no derived
      // geometry -- and answering the way the backend answers is what keeps the
      // fixture from inventing a shape the contract does not allow.
      return new Response(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'not modelled' } }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }
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

  it('opens one in live mode when the operator asked for it on the URL', async () => {
    // A completed dispatch renders the incident view, which mounts the
    // structure model. `vitest.setup.ts` stubs the canvas globally so that is
    // a fallback render rather than an unhandled jsdom error.

    // The escape hatch, and the only one. `?demo=1` is checked in the browser
    // at load, so a console already running against real services can be told
    // to run the choreography -- which a build-time `NEXT_PUBLIC_*` flag cannot
    // do, and which is the difference between a demo that works on stage and
    // one that silently does not.
    const search = window.location.search;
    window.history.replaceState({}, '', '/?demo=1');
    try {
      renderAt('live');
      await vi.advanceTimersByTimeAsync(120_000);
      expect(postedPaths().filter((p) => p.includes('/incidents')).length).toBeGreaterThan(0);
    } finally {
      window.history.replaceState({}, '', search || '/');
    }
  });

  it('opens the incident before the model reads the call, not after it', async () => {
    // One request used to carry both, and it aborted at the client's default
    // timeout against real services -- reported as "Could not open an incident:
    // signal is aborted without reason" for an incident that had opened. The
    // open is a sub-second write; reading the transcript is a Gemini call. They
    // are separate requests so the banner is never behind the model.
    window.history.replaceState({}, '', '/?demo=1');
    try {
      renderAt('live');
      await vi.advanceTimersByTimeAsync(120_000);

      const open = postedBody('/api/v1/incidents');
      expect(open).not.toBeNull();
      // The narrative is not on the open. If it were, the backend would read it
      // inline and the two would be one slow request again.
      expect(open).not.toHaveProperty('intake_narrative');

      const intake = postedBody('/intake');
      expect(intake).not.toBeNull();
      expect(typeof intake?.narrative).toBe('string');
      expect(String(intake?.narrative).length).toBeGreaterThan(0);
    } finally {
      window.history.replaceState({}, '', '/');
    }
  });

  it('still refuses in live mode when the URL says anything else', async () => {
    // Permission is the exact string. `?demo=0`, `?demo=true`, a stray `demo`
    // with no value: none of them is somebody asking for a simulated 911 call.
    window.history.replaceState({}, '', '/?demo=0');
    try {
      renderAt('live');
      await vi.advanceTimersByTimeAsync(120_000);
      expect(postedPaths().filter((p) => p.includes('/incidents'))).toHaveLength(0);
    } finally {
      window.history.replaceState({}, '', '/');
    }
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

describe('the call the demo dispatches can actually be heard', () => {
  it('hands the recording to dispatch rather than setting it alongside', () => {
    // The defect this catches was an ordering one, and every unit test passed
    // through it: the timer set the recording, then called `dispatch` with
    // three arguments. `dispatch` owns that state and cleared it back to null a
    // line later, so the arriving-call panel rendered with no player on it and
    // there was nothing on screen to press.
    //
    // A structural check, deliberately. The behaviour needs a fifty-second
    // timer, a mocked dispatch round trip and an overlay that only mounts once
    // the incident lands; asserting on the call site is the honest version of
    // what can actually be pinned here, and it is exactly the line that broke.
    const source = readFileSync(
      resolve(__dirname, '../components/CommandCenter.tsx'),
      'utf8',
    );
    expect(source).toContain(
      'dispatch(top, sample.text, sample.channel, sample.audioSrc)',
    );
    // And nothing sets it beside the call, which is what went wrong.
    expect(source).not.toContain('setCallAudioSrc(sample.audioSrc');
  });
});
