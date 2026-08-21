/**
 * The command center: standby, the dispatch transition, and back again.
 *
 * The seeded story, driven through the real components with the network
 * stubbed at `fetch`: a conflict the slow loop found, a dispatch that does not
 * navigate away, an instant brief before any prose, an IC resolution, and a
 * close that returns to an updated standby.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CommandCenter } from '@/components/CommandCenter';
import {
  ADDRESS,
  AGENTS,
  DECISIONS,
  EVENTS,
  GEOMETRY,
  INCIDENT,
  PROFILE,
  QUEUE,
  STATS,
  STATUS,
  SUBSCRIPTIONS,
  TIMELINE,
  emission,
} from './fixtures';

/** Routes the console's own gateway paths to fixtures. */
function stubFetch(overrides: Record<string, unknown> = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = (value: unknown, status = 200) =>
      new Response(JSON.stringify(value), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });

    for (const [fragment, value] of Object.entries(overrides)) {
      if (url.includes(fragment)) return body(value);
    }
    if (url.includes('/readyz')) {
      return body({ status: 'ready', ready: true, mode: 'fake', checks: [] });
    }
    if (url.includes('/healthz')) return body({ status: 'alive', app: 'firstdue', version: '0.1.0' });
    if (url.includes('/districts/') && url.includes('/stats')) return body(STATS);
    if (url.includes('/districts/') && url.includes('/queue')) return body(QUEUE);
    if (url.includes('/audit/events')) return body(EVENTS);
    if (url.includes('/audit/decisions')) return body(DECISIONS);
    if (url.includes('/registry/agents')) return body({ agents: AGENTS, count: AGENTS.length });
    if (url.includes('/registry/subscriptions')) {
      return body({ subscriptions: SUBSCRIPTIONS, count: SUBSCRIPTIONS.length });
    }
    if (url.includes('/timeline')) return body(TIMELINE);
    if (url.includes('/geometry')) return body(GEOMETRY);
    if (url.includes(`/buildings/${ADDRESS}`)) return body(PROFILE);
    if (url.includes('/incidents') && init?.method === 'POST' && url.endsWith('/incidents')) {
      return body(INCIDENT, 201);
    }
    if (url.includes('/brief/enrich')) return body(emission({ version: 2, stage: 'ENRICHED' }));
    if (url.includes('/resolutions')) {
      return body(
        { conflict_id: 'conflict_0c93', fact_id: 'f', profile_version: 17, brief_version: 3, resolved_by: 'bc-09' },
        201,
      );
    }
    if (url.includes('/close')) {
      return body({
        incident: {},
        grant_revoked_at: '2026-08-20T09:00:00+00:00',
        log_sealed_at: '2026-08-20T09:00:00+00:00',
        log_entries: 9,
        neris_draft: {},
        rms_still_buffered: 0,
      });
    }
    if (url.includes('/log')) return body({ incident_id: 'inc-1', sealed_at: null, entries: [], unflushed: 0 });
    return body({});
  });
}

function renderConsole(props: Partial<Parameters<typeof CommandCenter>[0]> = {}) {
  return render(
    <CommandCenter
      status={STATUS}
      readiness={null}
      error={null}
      initialStats={STATS}
      initialQueue={QUEUE}
      initialAgents={AGENTS}
      initialSubscriptions={SUBSCRIPTIONS}
      initialEvents={EVENTS}
      initialDecisions={DECISIONS}
      forceSvgGeometry
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.stubGlobal('fetch', stubFetch());
  // jsdom has no EventSource; the console falls back to the emission the open
  // call already returned, which is the degraded path a locked-down tablet hits.
  vi.stubGlobal('EventSource', undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('standby', () => {
  it('shows the district metric strip with real counts', () => {
    renderConsole();
    expect(screen.getByText('Structures')).toBeInTheDocument();
    expect(screen.getByText('43')).toBeInTheDocument();
    expect(screen.getByText('1 at severity 4+')).toBeInTheDocument();
  });

  it('reports source availability honestly, including unconfigured ones', () => {
    renderConsole();
    expect(screen.getByText(/1 UNAVAILABLE: tier-ii-confidential/)).toBeInTheDocument();
    expect(screen.getByText(/1 fixture-backed/)).toBeInTheDocument();
  });

  it('shows the fleet with publisher and pinned version', () => {
    renderConsole();
    expect(screen.getAllByText('records-watcher').length).toBeGreaterThan(0);
    expect(screen.getByText('building')).toBeInTheDocument();
    expect(screen.getAllByText('@1.0.0').length).toBeGreaterThan(0);
  });

  it('ranks the queue and shows the reasons inline', () => {
    renderConsole();
    const queue = screen.getByLabelText('Ranked survey queue');
    // Direct children only: reason lines are nested list items.
    const rows = Array.from(queue.children) as HTMLElement[];
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText(ADDRESS)).toBeInTheDocument();
    expect(
      within(rows[0]!).getByText(/Severity 4 conflict open/),
    ).toBeInTheDocument();
    expect(within(rows[0]!).getByText('rank.open-conflict-severity')).toBeInTheDocument();
  });

  it('shows the timestamped activity and audit stream', () => {
    renderConsole();
    const stream = screen.getByLabelText('Activity and audit stream');
    expect(within(stream).getByText('injection blocked')).toBeInTheDocument();
    expect(within(stream).getByText('require approval')).toBeInTheDocument();
  });
});

describe('the building profile', () => {
  it('opens from the queue without navigating away', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));

    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    // Still the same page: the queue is right where it was.
    expect(screen.getByLabelText('Ranked survey queue')).toBeInTheDocument();
  });

  it('shows provenance and all three assertion states', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() =>
      expect(screen.getAllByText('structure.stories').length).toBeGreaterThan(0),
    );

    const grid = screen.getByRole('table');
    expect(within(grid).getAllByText('PERMIT').length).toBeGreaterThan(0);
    expect(within(grid).getByText('disputed')).toBeInTheDocument();
    expect(within(grid).getByText('confirmed')).toBeInTheDocument();
    expect(within(grid).getByText('unknown')).toBeInTheDocument();
    // Provenance, not just a value.
    expect(within(grid).getByText('3 facts on record')).toBeInTheDocument();
  });

  it('shows the conflict, the referral case number, and the timeline', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() =>
      expect(screen.getAllByText(/Permit records 2 storeys/).length).toBeGreaterThan(0),
    );
    expect(screen.getByText('case REF-00001')).toBeInTheDocument();
    expect(screen.getByLabelText('Profile timeline, newest first')).toBeInTheDocument();
  });

  it('will not offer to settle a conflict outside an incident', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() =>
      expect(screen.getByText(/Open an incident to settle this on scene/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: 'Settle on scene' })).not.toBeInTheDocument();
  });
});

describe('the dispatch transition', () => {
  async function dispatch() {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /simulate cad dispatch/i }));
    await waitFor(() => expect(screen.getByLabelText('Active incident')).toBeInTheDocument());
  }

  it('opens the incident in place, without leaving the page', async () => {
    await dispatch();
    // The fleet rail is still there, compressed rather than gone.
    expect(screen.getByLabelText('Slow-loop fleet, still running')).toBeInTheDocument();
    expect(screen.getByText('Incident agents')).toBeInTheDocument();
    expect(screen.getByRole('banner')).toBeInTheDocument();
  });

  it('shows the elapsed clock and the incident identity', async () => {
    await dispatch();
    const banner = screen.getByLabelText('Active incident');
    expect(within(banner).getByText(ADDRESS)).toBeInTheDocument();
    expect(within(banner).getByText('inc-1')).toBeInTheDocument();
    expect(within(banner).getByRole('status')).toBeInTheDocument();
  });

  it('shows instant facts before any prose', async () => {
    await dispatch();
    // The deterministic values are on screen.
    expect(screen.getByText('v1 instant · no model')).toBeInTheDocument();
    expect(
      screen.getByText(/The instant stage contains no model call/),
    ).toBeInTheDocument();
    // And UNKNOWN is rendered as UNKNOWN, not as an absence.
    expect(screen.getByText('UNKNOWN - no record found')).toBeInTheDocument();
  });

  it('announces the stage to a screen reader', async () => {
    await dispatch();
    // The announcement is written by an effect that runs *after* the incident
    // renders, so it lands a paint later than the banner `dispatch()` waits
    // for. Asserting it synchronously passed on Node 24 and failed on Node 20
    // -- a flake in the test, not a change in what an officer hears.
    const announcer = screen.getByTestId('brief-announcer');
    await waitFor(() =>
      expect(announcer).toHaveTextContent(/Instant brief ready, version 1/),
    );
    expect(announcer).toHaveTextContent(/no model was invoked/);
  });

  it('offers notifications and staged commitments separately', async () => {
    await dispatch();
    expect(screen.getByText('Notify — autonomous')).toBeInTheDocument();
    expect(screen.getByText('Commit — requires a human')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /water department/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /gas shutoff/i })).toBeInTheDocument();
  });

  it('records an IC resolution and reports the new profile version', async () => {
    await dispatch();
    fireEvent.click(screen.getByRole('button', { name: 'Settle on scene' }));
    fireEvent.change(screen.getByLabelText('What did you observe?'), {
      target: { value: '3' },
    });
    fireEvent.change(screen.getByLabelText('Who observed it?'), {
      target: { value: 'bc-09' },
    });
    fireEvent.click(screen.getByRole('button', { name: /record observation/i }));

    await waitFor(() =>
      expect(
        screen.getAllByRole('status').some((node) =>
          /Profile is now version 17/.test(node.textContent ?? ''),
        ),
      ).toBe(true),
    );
  });

  it('returns to an updated standby when the incident closes', async () => {
    await dispatch();
    fireEvent.click(screen.getByRole('button', { name: /close incident/i }));

    await waitFor(() =>
      expect(screen.queryByLabelText('Active incident')).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText('Ranked survey queue')).toBeInTheDocument();
    expect(screen.getAllByRole('status')[0]).toHaveTextContent(/Grant revoked, log sealed/);
  });
});

describe('degraded states', () => {
  it('renders an explicit failure when the backend is unreachable', () => {
    render(
      <CommandCenter
        status={null}
        readiness={null}
        error="Backend status unavailable: fetch failed"
      />,
    );
    expect(screen.getByText('No backend status')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/Backend status unavailable/);
  });

  it('says so when a dispatch is refused rather than failing silently', async () => {
    const base = stubFetch();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith('/incidents') && init?.method === 'POST') {
          return new Response(
            JSON.stringify({
              error: { code: 'NOT_FOUND', message: 'address did not resolve', details: {} },
            }),
            { status: 404, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return base(input, init);
      }),
    );

    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /simulate cad dispatch/i }));

    await waitFor(() =>
      expect(
        screen.getAllByRole('status').some((node) =>
          /Could not open an incident/.test(node.textContent ?? ''),
        ),
      ).toBe(true),
    );
  });

  it('shows an honest empty queue rather than invented rows', () => {
    renderConsole({ initialQueue: { district_id: 'd', entries: [], count: 0 } });
    expect(screen.getByText('No ranked structures yet')).toBeInTheDocument();
  });
});

describe('a live deployment that cannot reach Workspace says so', () => {
  /**
   * Calendar and Gmail act as a *user*, so a live deployment without delegated
   * Workspace authority records those two write actions and sends neither.
   *
   * That is the same shape as rendering an absent record as "none present": the
   * work order, the referral, and the pre-plan are genuinely executed, and if
   * the crew notification sits beside them looking identical, the console is
   * asserting a notification nobody received. So it has to be on screen.
   */
  beforeEach(() => {
    vi.stubGlobal('fetch', stubFetch());
  });

  it('marks calendar and mail simulated when live mode holds no Workspace authority', () => {
    renderConsole({ status: { ...STATUS, mode: 'live', workspace_writes: 'fake' } });
    expect(screen.getByText('calendar + mail: simulated')).toBeInTheDocument();
  });

  it('says nothing when a live deployment does reach Workspace', () => {
    renderConsole({ status: { ...STATUS, mode: 'live', workspace_writes: 'google' } });
    expect(screen.queryByText('calendar + mail: simulated')).not.toBeInTheDocument();
  });

  it('says nothing in fake mode, where the mode pill already carries it', () => {
    /** Every adapter is simulated there; a second badge would be noise. */
    renderConsole({ status: { ...STATUS, mode: 'fake', workspace_writes: 'fake' } });
    expect(screen.queryByText('calendar + mail: simulated')).not.toBeInTheDocument();
  });
});
