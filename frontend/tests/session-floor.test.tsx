/**
 * The session floor, and the two ways it was hiding the fleet's own work.
 *
 * The floor exists because `make live-demo` runs against a Firestore that keeps
 * the audit log across restarts, so a freshly loaded console reads hours of a
 * previous run and a counter over it opens at a number nobody in the room has
 * watched produce. Everything it drops is supposed to be work from before this
 * console arrived. These are the cases where it dropped work from *after*.
 *
 * **Both were invisible to the rest of the suite, and the reason is the clock.**
 * Fake mode runs a `SteppingClock` from `DEMO_EPOCH` and live mode a
 * `SystemClock`, and the two mint different-looking timestamps: the stepping
 * clock advances 50 ms from a whole second, so one reading in twenty lands on
 * an exact second and `datetime.isoformat()` writes it with no fractional part
 * at all, while `datetime.now(UTC)` essentially always has one. Every fixture in
 * `fleet.test.tsx` is whole-second on both sides of every comparison, which is
 * the one shape in which the ordering defect below cannot appear. So the tests
 * here are written across both shapes deliberately, and a fixture that is
 * uniform in either direction is not a fixture that covers this.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CommandCenter } from '@/components/CommandCenter';
import { FleetPanel } from '@/components/fleet/FleetPanel';
import { compareAt, eventsSince, sessionFloor } from '@/components/fleet/derive';
import type { AuditEventView } from '@/lib/api/types';

import { AGENTS, QUEUE, STATS, STATUS, SUBSCRIPTIONS } from './fixtures';

const WATCHER = 'records-watcher';

function ev(at: string, over: Partial<AuditEventView> = {}): AuditEventView {
  return {
    audit_id: `audit_${at}_${over.actor ?? WATCHER}`,
    kind: 'agent_step',
    occurred_at: at,
    actor: WATCHER,
    target: 'sffd-district-03',
    incident_id: null,
    correlation_id: 'corr_this_run',
    detail: {},
    ...over,
  };
}

/**
 * The same log written by each of the two clocks this system runs on.
 *
 * `stepping` is what `SteppingClock(DEMO_EPOCH, step=50ms)` produces when the
 * previous run's last reading happened to land on an exact second -- one
 * reading in twenty does. `system` is what `datetime.now(UTC)` produces, which
 * is the same instants with a microsecond field that is never zero.
 */
const SHAPES = {
  stepping: {
    priorRun: ['2026-08-20T07:59:59.950000+00:00', '2026-08-20T08:00:00+00:00'],
    thisRun: ['2026-08-20T08:00:00.050000+00:00', '2026-08-20T08:00:00.100000+00:00'],
  },
  system: {
    priorRun: ['2026-08-20T07:59:59.912004+00:00', '2026-08-20T08:00:00.418771+00:00'],
    thisRun: ['2026-08-20T08:00:00.664209+00:00', '2026-08-20T08:00:00.902118+00:00'],
  },
} as const;

describe('backend instants are ordered by what they are, not by collation', () => {
  it.each(Object.entries(SHAPES))(
    'puts a later instant after an earlier one (%s clock)',
    (_shape, { priorRun, thisRun }) => {
      expect(compareAt(priorRun[1], thisRun[0])).toBeLessThan(0);
      expect(compareAt(thisRun[0], priorRun[1])).toBeGreaterThan(0);
    },
  );

  it('orders a fractional instant after the whole second it falls in', () => {
    // The defect, named. `localeCompare` gives punctuation its own collation
    // weights, and in those `.` sorts before `+` -- so it reports
    // `08:00:00.050000+00:00` as *earlier* than `08:00:00+00:00`, which is a
    // floor that swallows the rest of its own second. Asserted against
    // `localeCompare` directly so this file states what it is defending
    // against rather than only that the replacement works.
    const whole = '2026-08-20T08:00:00+00:00';
    const fraction = '2026-08-20T08:00:00.050000+00:00';
    expect(whole.localeCompare(fraction)).toBeGreaterThan(0);
    expect(compareAt(whole, fraction)).toBeLessThan(0);
  });
});

describe('a console whose first read holds a pass still counts the next one', () => {
  it.each(Object.entries(SHAPES))(
    'counts events written after the floor (%s clock)',
    (_shape, { priorRun, thisRun }) => {
      const before = priorRun.map((at) => ev(at, { correlation_id: 'corr_last_run' }));
      const floor = sessionFloor(before, []);
      expect(floor).toBe(priorRun[1]);

      const after = thisRun.map((at) => ev(at));
      expect(eventsSince([...before, ...after], floor)).toHaveLength(after.length);
    },
  );

  it.each(Object.entries(SHAPES))(
    'draws the agent active rather than idle (%s clock)',
    (_shape, { priorRun, thisRun }) => {
      const before = priorRun.map((at) => ev(at, { correlation_id: 'corr_last_run' }));
      const floor = sessionFloor(before, []);

      const { rerender } = render(
        <FleetPanel agents={AGENTS} subscriptions={[]} loop="SLOW" events={before} since={floor} />,
      );
      expect(screen.getByTestId(`fleet-row-${WATCHER}`)).toHaveTextContent('0 recorded');

      rerender(
        <FleetPanel
          agents={AGENTS}
          subscriptions={[]}
          loop="SLOW"
          events={[...before, ...thisRun.map((at) => ev(at))]}
          since={floor}
        />,
      );

      const row = screen.getByTestId(`fleet-row-${WATCHER}`);
      expect(row).toHaveTextContent('2 recorded');
      expect(row).toHaveTextContent('active');
    },
  );
});

/**
 * The second failure, which no amount of comparison arithmetic fixes.
 *
 * The floor is taken from the first audit read that *answers*, and against a
 * live backend that read is the slowest thing on the screen -- `list_events`
 * reads the whole audit collection and decodes it document by document. The
 * choreography starts a pass three seconds after load. So when the first read
 * times out, the next one to come back has the console's own pass in it,
 * `sessionFloor` anchors on the newest instant in that answer, and the console
 * floors out the work it just commissioned: every agent `0 recorded`, every
 * agent idle, for a pass that ran in full.
 */
describe('a console does not anchor its floor on a pass it started itself', () => {
  const PRIOR = [ev('2026-08-20T07:59:00.100000+00:00', { correlation_id: 'corr_last_run' })];
  const THIS_PASS = [
    ev('2026-08-20T08:00:01.100000+00:00'),
    ev('2026-08-20T08:00:02.200000+00:00', { kind: 'agent_pass' }),
  ];

  /** Whether the console has asked the backend to run a pass yet. */
  let passRequested = false;

  beforeEach(() => {
    passRequested = false;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    globalThis.fetch = vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });

      if (init?.method === 'POST' && url.includes('/poll')) {
        passRequested = true;
        return json({ district_id: 'sffd-district-03', facts_written: 2 });
      }
      if (url.includes('/audit/events')) {
        // The first read never answers -- which is what a twenty-second
        // timeout against a full audit collection looks like from here. By the
        // time one does, the pass has been writing.
        if (!passRequested) return Promise.reject(new Error('read timed out'));
        return json([...THIS_PASS, ...PRIOR]);
      }
      if (url.includes('/audit/decisions')) {
        if (!passRequested) return Promise.reject(new Error('read timed out'));
        return json([]);
      }
      if (url.includes('/queue')) return json(QUEUE);
      if (url.includes('/stats')) return json(STATS);
      if (url.includes('/fire-activity')) {
        return json({ available: false, unavailable_reason: 'no key' });
      }
      return new Response(
        JSON.stringify({ error: { code: 'NOT_FOUND', message: 'not modelled' } }),
        { status: 404, headers: { 'Content-Type': 'application/json' } },
      );
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('shows the work of a pass whose first fleet read did not come back', async () => {
    render(
      <CommandCenter
        status={STATUS}
        readiness={null}
        error={null}
        initialStats={STATS}
        initialQueue={QUEUE}
        initialAgents={AGENTS}
        initialSubscriptions={SUBSCRIPTIONS}
        // Both empty: a console that was handed a log was handed one from
        // before it mounted and floors on it at construction, which is the
        // path the panel tests above already cover. The one under test here is
        // the console that arrives with nothing and has to ask.
        initialEvents={[]}
        initialDecisions={[]}
        forceSvgGeometry
      />,
    );

    // Past the choreography's lead-in and a couple of fleet polls.
    await vi.advanceTimersByTimeAsync(12_000);

    await waitFor(() => {
      const row = screen.getByTestId(`fleet-row-${WATCHER}`);
      // Three, not two: within a session the counter no longer narrows to
      // the pass in flight. The floor still keeps a previous run's totals
      // out, but work from an earlier pass of the *same* session now stays
      // counted -- which is the point. An officer watching the fleet build
      // a district should not see the number reset when a pass rolls over,
      // and least of all on the far side of an incident.
      expect(row).toHaveTextContent('3 recorded');
      expect(row).toHaveTextContent('active');
    });
  });
});
