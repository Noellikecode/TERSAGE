/**
 * The command center: standby, the dispatch transition, and back again.
 *
 * The seeded story, driven through the real components with the network
 * stubbed at `fetch`: a conflict the slow loop found, a dispatch that does not
 * navigate away, an instant brief before any prose, an IC resolution, and a
 * close that returns to an updated standby.
 *
 * The layout is a district bar pinned under the header, and beneath it three
 * columns in *both* modes. In standby the slow loop is split across the two
 * flanking columns and the middle carries the region -- fire activity, the
 * ranked structures, whichever structure is open. In an incident the same
 * three columns hold the incident loop left, the structure and brief in the
 * middle, and the slow loop right. The assertions below check that the shape
 * does not change at dispatch, and that the slow loop is still on screen
 * rather than merely still fetched.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

/** A street-level photograph, as the imagery endpoint returns one. */
const IMAGERY = {
  address_id: ADDRESS,
  available: true,
  provider: 'google-street-view',
  content_type: 'image/jpeg',
  data_url: 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAA',
  attribution: 'Imagery © 2026 Google',
  captured_hint: 'June 2025',
  unavailable_reason: null,
};

/**
 * Regional fire activity, shaped like the live answer: an empty city inside a
 * busy region. San Francisco proper returns zero VIIRS detections and Northern
 * California returns hundreds, because a 375 m wildfire pixel does not see a
 * structure fire. Two detections stand in for the 266.
 */
const FIRE_ACTIVITY = {
  district_id: 'sffd-district-03',
  available: true,
  unavailable_reason: null,
  source: 'nasa-firms/viirs-snpp-nrt',
  region_label: 'Northern California',
  city_label: 'San Francisco',
  bbox: { west: -124.4, south: 36.9, east: -119.9, north: 41.2 },
  city_bbox: { west: -122.52, south: 37.7, east: -122.35, north: 37.83 },
  regional_count: 266,
  in_city_count: 0,
  detections: [
    {
      latitude: 39.81,
      longitude: -121.44,
      confidence: 'h',
      frp: 42.6,
      acquired_at: '2026-08-22T21:10:00+00:00',
      satellite: 'N',
    },
    {
      latitude: 38.44,
      longitude: -122.71,
      confidence: 'n',
      frp: 3.1,
      acquired_at: '2026-08-22T21:10:00+00:00',
      satellite: 'N',
    },
  ],
  fire_weather: {
    available: true,
    unavailable_reason: null,
    source: 'nasa-power',
    temperature_c: 24.3,
    relative_humidity_pct: 41,
    wind_speed_ms: 3.6,
    wind_direction_deg: 265,
    observation_start: '2026-08-18T00:00:00+00:00',
    observation_end: '2026-08-19T00:00:00+00:00',
  },
};

/** What one hand-run slow-loop pass reports back. */
const PASS_REPORT = {
  district_id: 'sffd-district-03',
  ran_at: '2026-08-23T08:00:00+00:00',
  facts_written: 6,
  facts_deduped: 2,
  conflicts: ['conflict_0c93'],
  unavailable_sources: [],
  queue_size: 4,
  top_address_id: ADDRESS,
};

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
    if (url.includes('/fire-activity')) return body(FIRE_ACTIVITY);
    if (url.includes('/poll') && init?.method === 'POST') return body(PASS_REPORT);
    if (url.includes('/audit/events')) return body(EVENTS);
    if (url.includes('/audit/decisions')) return body(DECISIONS);
    if (url.includes('/registry/agents')) return body({ agents: AGENTS, count: AGENTS.length });
    if (url.includes('/registry/subscriptions')) {
      return body({ subscriptions: SUBSCRIPTIONS, count: SUBSCRIPTIONS.length });
    }
    if (url.includes('/imagery')) return body(IMAGERY);
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

/** Standby, a profile opened from the queue, and a dispatch on top of it. */
async function dispatchIncident() {
  renderConsole();
  fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
  await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
  fireEvent.click(screen.getByTestId('dispatch-button'));
  await waitFor(() => expect(screen.getByLabelText('Active incident')).toBeInTheDocument());
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

  it('draws a meter only where the backend reports both halves of the ratio', () => {
    renderConsole();
    const bar = screen.getByTestId('district-bar');
    // Four of the six counts have an honest denominator on the same payload;
    // facts and companies-out do not, and get a dashed track rather than a
    // fill against a scale nobody measured.
    expect(within(bar).getAllByTestId('meter')).toHaveLength(4);
    expect(within(bar).getAllByTestId('meter-unscaled')).toHaveLength(2);
    expect(within(bar).getByTestId('source-ring')).toBeInTheDocument();
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

  it('offers every ranked structure as a chip, in rank order', () => {
    renderConsole();
    const strip = screen.getByLabelText('Ranked structures');
    const chips = Array.from(strip.children) as HTMLElement[];
    expect(chips).toHaveLength(2);
    // The rank is a backend value and stays; the score and the rule text went
    // with the queue panel rather than being restated without their reasons.
    expect(within(chips[0]!).getByText('1')).toBeInTheDocument();
    expect(within(chips[0]!).getByRole('button', { name: ADDRESS })).toBeInTheDocument();
    expect(within(chips[1]!).getByRole('button', { name: 'sf-1215-fell' })).toBeInTheDocument();
    expect(screen.queryByText('rank.open-conflict-severity')).not.toBeInTheDocument();
  });

  it('carries no ranked survey queue panel any more', () => {
    renderConsole();
    expect(screen.queryByLabelText('Ranked survey queue')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^why$/i })).not.toBeInTheDocument();
  });
});

describe('the layout', () => {
  it('pins the district bar under the header, above the mode switch', () => {
    renderConsole();
    const bar = screen.getByRole('region', { name: 'District readiness' });
    // Inside the main landmark, and the first region in it: everything else on
    // screen is one mode or the other, and this is true in both.
    const main = screen.getByRole('main');
    expect(main).toContainElement(bar);
    expect(within(main).getAllByRole('region')[0]).toBe(bar);
    expect(within(bar).getByText('Structures')).toBeInTheDocument();
  });

  it('gives standby the same three columns an incident uses', () => {
    renderConsole();
    // Two fleet columns flanking, both the slow loop, and the region between
    // them. The shape does not change at dispatch, so an officer does not
    // re-learn the screen at the moment a fire starts.
    const columns = screen.getAllByRole('region', { name: /fleet/i });
    expect(columns).toHaveLength(2);

    const [left, right] = columns as [HTMLElement, HTMLElement];
    const activity = screen.getByRole('region', { name: 'Regional fire activity' });
    const structures = screen.getByRole('region', { name: 'Ranked for survey' });

    expect(left.compareDocumentPosition(activity) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      activity.compareDocumentPosition(structures) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(structures.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('splits the slow loop across the two columns, in catalog order, without repeating a card', () => {
    // The panel scopes itself by loop, and both standby columns are the same
    // loop -- so the layout hands each an explicit half. The fixture publishes
    // one slow agent, which lands on the left; the right column says where the
    // fleet went rather than falling through to "empty catalog", which would be
    // true of the catalog and false of the column.
    renderConsole();
    const [left, right] = screen.getAllByRole('region', { name: /fleet/i }) as [
      HTMLElement,
      HTMLElement,
    ];
    expect(left).toHaveAccessibleName('Fleet — slow loop');
    expect(right).toHaveAccessibleName('Fleet — slow loop, continued');

    expect(within(left).getAllByText(/records-watcher/).length).toBeGreaterThan(0);
    expect(within(right).queryByText(/records-watcher/)).not.toBeInTheDocument();
    expect(
      within(right).getByText(/slow-loop agent, and it is in the column on the left/),
    ).toBeInTheDocument();
    expect(within(right).queryByText(/empty catalog/)).not.toBeInTheDocument();

    // Neither column claims the incident loop, which is not running.
    expect(within(left).queryByText(/incident-controller/)).not.toBeInTheDocument();
    expect(within(right).queryByText(/incident-controller/)).not.toBeInTheDocument();
  });

  it('cuts a real slow fleet in half, left column first', () => {
    const slow = ['a', 'b', 'c', 'd', 'e'].map((id) => ({
      ...AGENTS[0]!,
      agent_id: `slow-${id}`,
      ref: `slow-${id}@1.0.0`,
    }));
    renderConsole({ initialAgents: [...slow, AGENTS[1]!] });
    const [left, right] = screen.getAllByRole('region', { name: /fleet/i }) as [
      HTMLElement,
      HTMLElement,
    ];
    // Five agents split three/two, in the order the registry published them.
    for (const id of ['slow-a', 'slow-b', 'slow-c']) {
      expect(within(left).getAllByText(new RegExp(id)).length).toBeGreaterThan(0);
      expect(within(right).queryByText(new RegExp(`${id}\\b`))).not.toBeInTheDocument();
    }
    for (const id of ['slow-d', 'slow-e']) {
      expect(within(right).getAllByText(new RegExp(id)).length).toBeGreaterThan(0);
      expect(within(left).queryByText(new RegExp(`${id}\\b`))).not.toBeInTheDocument();
    }
    expect(left).toHaveTextContent('3 of 5');
    expect(right).toHaveTextContent('2 of 5');
  });

  it('keeps the massing model out of standby until a structure is selected', async () => {
    renderConsole();
    expect(screen.queryByRole('region', { name: 'Massing model' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Massing model' })).toBeInTheDocument(),
    );
    // The camera views belong to the model wherever it is rendered.
    const model = screen.getByRole('region', { name: 'Massing model' });
    expect(within(model).getByRole('group', { name: /fixed camera views/i })).toBeInTheDocument();
    // The fleet columns did not go anywhere to make room for it: the structure
    // opens inside the middle column, under the ranked strip.
    expect(screen.getAllByRole('region', { name: /fleet/i })).toHaveLength(2);
    expect(
      screen
        .getByRole('region', { name: 'Ranked for survey' })
        .compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('shows no building photograph in standby rather than a box explaining itself', () => {
    renderConsole();
    expect(screen.queryByRole('region', { name: 'Building imagery' })).not.toBeInTheDocument();
    expect(screen.queryByText(/No incident open/)).not.toBeInTheDocument();
  });

  it('stacks nothing under the fold: no audit console, no activity stream', () => {
    renderConsole();
    expect(screen.queryByLabelText('Activity and audit stream')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Audit' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Filter decisions by outcome')).not.toBeInTheDocument();
  });

  it('carries no row of status chips in the header', () => {
    renderConsole();
    expect(screen.queryByText('fake mode')).not.toBeInTheDocument();
    expect(screen.queryByText('store: memory')).not.toBeInTheDocument();
    expect(screen.queryByText('events: memory')).not.toBeInTheDocument();
  });

  it('still says whether the backend is reachable at all', async () => {
    renderConsole();
    // An empty district and a dead backend must not read the same. The signal
    // is one dot, and its state is available to a screen reader either way.
    const signal = screen.getByTestId('backend-signal');
    expect(signal).toHaveTextContent(/backend/i);
    await waitFor(() => expect(signal).toHaveTextContent('Backend reachable'));
  });
});

describe('regional fire activity', () => {
  it('reads the region on the console gateway path, folded into standby', async () => {
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    renderConsole();
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes('/api/gateway/api/v1/districts/sffd-district-03/fire-activity'),
        ),
      ).toBe(true),
    );
  });

  it('states an empty city inside a busy region as a fact, not an alarm', async () => {
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    const counts = within(panel).getByTestId('fire-activity-counts');
    expect(counts).toHaveTextContent('0');
    expect(counts).toHaveTextContent(/active detections in San Francisco/);
    expect(counts).toHaveTextContent('266');
    expect(counts).toHaveTextContent(/across Northern California/);
    // The reason a zero is the normal reading is on screen with the zero.
    expect(within(panel).getByText(/375 m and built for wildfire/)).toBeInTheDocument();
  });

  it('plots the detections and outlines the city without inventing one inside it', async () => {
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    const svg = within(panel).getByTestId('fire-activity-scatter');
    // One circle per reported detection, and none of them stands for the city.
    expect(svg.querySelectorAll('circle')).toHaveLength(2);
    expect(within(panel).getByTestId('fire-activity-city-outline')).toBeInTheDocument();
    expect(svg.querySelector('[data-band="high"]')).toBeTruthy();
    expect(svg.querySelector('[data-band="nominal"]')).toBeTruthy();
  });

  it('labels the fire weather with its window and refuses to pass as current', async () => {
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    const weather = within(panel).getByTestId('fire-weather');
    expect(within(weather).getByText('24.3')).toBeInTheDocument();
    expect(within(weather).getByText('41')).toBeInTheDocument();
    expect(within(weather).getByText('3.6')).toBeInTheDocument();
    // The observation window is on every reading, not on the block alone.
    expect(within(weather).getAllByText('2026-08-18 → 2026-08-19')).toHaveLength(3);
    expect(within(weather).getByText(/recent conditions, not current/)).toBeInTheDocument();
    expect(within(weather).getByText(/not the live NWS wind/)).toBeInTheDocument();
  });

  it('renders a refusal as a renderable state rather than a failure', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/fire-activity': {
          ...FIRE_ACTIVITY,
          available: false,
          unavailable_reason: 'FIRMS map key is not configured for this deployment.',
          detections: [],
        },
      }),
    );
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    expect(await within(panel).findByText(/FIRMS map key is not configured/)).toBeInTheDocument();
    expect(within(panel).getByText(/UNAVAILABLE/)).toBeInTheDocument();
    expect(within(panel).queryByText(/request failed/i)).not.toBeInTheDocument();
  });

  it('separates a failed request from an answered refusal', async () => {
    const base = stubFetch();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes('/fire-activity')) {
          return new Response(
            JSON.stringify({
              error: { code: 'BACKEND_UNREACHABLE', message: 'fire activity timed out', details: {} },
            }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return base(input, init);
      }),
    );
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    expect(
      await within(panel).findByText(/Fire-activity request failed: fire activity timed out/),
    ).toBeInTheDocument();
  });

  it('is a standby surface: an open incident has the structure there instead', async () => {
    await dispatchIncident();
    expect(
      screen.queryByRole('region', { name: 'Regional fire activity' }),
    ).not.toBeInTheDocument();
  });
});

describe('running a slow-loop pass by hand', () => {
  it('is labelled for what it does, not for what the screen does', () => {
    renderConsole();
    const button = screen.getByTestId('run-slow-loop-pass');
    expect(button).toHaveTextContent('Run a slow-loop pass');
    expect(button).not.toBeDisabled();
    expect(screen.queryByRole('button', { name: /^refresh$/i })).not.toBeInTheDocument();
  });

  it('posts the district poll, then re-reads the district and reports the pass', async () => {
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    renderConsole();
    fireEvent.click(screen.getByTestId('run-slow-loop-pass'));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            String(input).endsWith('/api/gateway/api/v1/districts/sffd-district-03/poll') &&
            (init as RequestInit | undefined)?.method === 'POST',
        ),
      ).toBe(true),
    );
    // The queue is re-read before the pass is called done, so the counts on
    // screen are the ones the pass produced.
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/queue')).length)
        .toBeGreaterThan(0),
    );
    await waitFor(() =>
      expect(screen.getByTestId('slow-loop-pass-status')).toHaveTextContent(
        /Slow-loop pass complete: 6 facts written, 1 conflicts detected, queue re-ranked to 4/,
      ),
    );
  });

  it('disables itself and says it is running while the pass is in flight', async () => {
    // A gate the test opens by hand, so the in-flight state is observable.
    const gate: { release: () => void } = { release: () => {} };
    const held = new Promise<void>((resolve) => {
      gate.release = resolve;
    });
    const base = stubFetch();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes('/poll')) await held;
        return base(input, init);
      }),
    );
    renderConsole();
    fireEvent.click(screen.getByTestId('run-slow-loop-pass'));

    await waitFor(() => expect(screen.getByTestId('run-slow-loop-pass')).toBeDisabled());
    expect(screen.getByTestId('run-slow-loop-pass')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('slow-loop-pass-status')).toHaveTextContent(/pass running/i);

    gate.release();
    await waitFor(() => expect(screen.getByTestId('run-slow-loop-pass')).not.toBeDisabled());
  });

  it('surfaces a refused pass as a message rather than a silent no-op', async () => {
    const base = stubFetch();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes('/poll')) {
          return new Response(
            JSON.stringify({
              error: { code: 'FORBIDDEN', message: 'write:profile not held', details: {} },
            }),
            { status: 403, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return base(input, init);
      }),
    );
    renderConsole();
    fireEvent.click(screen.getByTestId('run-slow-loop-pass'));

    await waitFor(() =>
      expect(screen.getByTestId('slow-loop-pass-status')).toHaveTextContent(
        /Slow-loop pass failed: write:profile not held/,
      ),
    );
    expect(screen.getByTestId('run-slow-loop-pass')).not.toBeDisabled();
  });
});

describe('the building profile', () => {
  it('opens from the queue without navigating away', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: ADDRESS }));

    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    // Still the same page: the structure strip is right where it was.
    expect(screen.getByLabelText('Ranked structures')).toBeInTheDocument();
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
  const dispatch = dispatchIncident;

  it('opens the incident in place, without leaving the page', async () => {
    await dispatch();
    expect(screen.getByRole('banner')).toBeInTheDocument();
    // The district bar is not a standby surface: it is still there.
    expect(screen.getByRole('region', { name: 'District readiness' })).toBeInTheDocument();
    // The slow loop is off screen during an incident, and says so rather than
    // leaving an officer to assume it stopped.
    expect(screen.queryByRole('region', { name: /slow loop/i })).not.toBeInTheDocument();
    const offscreen = screen.getByTestId('slow-loop-offscreen');
    expect(offscreen).toHaveTextContent(/off screen, still running/);
    expect(offscreen).toHaveTextContent(/agents/);
  });

  it('reorganises into three columns: incident fleet, structure, incident fleet', async () => {
    await dispatch();
    const columns = screen.getAllByRole('region', { name: /fleet/i });
    expect(columns).toHaveLength(2);

    const [incidentFleet, continuedFleet] = columns as [HTMLElement, HTMLElement];
    // The left column carries the agents acting right now.
    expect(within(incidentFleet).getAllByText(/incident-controller/).length).toBeGreaterThan(0);
    expect(within(incidentFleet).queryByText(/records-watcher/)).not.toBeInTheDocument();
    // Both flanking columns now carry the incident loop, split in catalog
    // order. Neither carries a slow-loop agent.
    expect(within(continuedFleet).queryByText(/records-watcher/)).not.toBeInTheDocument();
    // The right column carries the loop that did not stop because a fire did.


    // And the structure sits between them, still split model-then-photograph.
    const model = screen.getByRole('region', { name: 'Massing model' });
    const imagery = screen.getByRole('region', { name: 'Building imagery' });
    expect(
      model.compareDocumentPosition(incidentFleet) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    expect(
      model.compareDocumentPosition(imagery) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      imagery.compareDocumentPosition(continuedFleet) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('names each fleet column after the loop it was asked for', async () => {
    await dispatch();
    // The console names the loop per column and the panel answers which agents
    // match. Nothing here filters the agent list -- two places deciding that is
    // how the two answers drift apart -- so the headings are the contract.
    const [incidentFleet, continuedFleet] = screen.getAllByRole('region', { name: /fleet/i }) as [
      HTMLElement,
      HTMLElement,
    ];
    expect(incidentFleet).toHaveAccessibleName(/incident loop/i);
    expect(continuedFleet).toHaveAccessibleName(/incident loop, continued/i);
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
    expect(screen.getByLabelText('Ranked structures')).toBeInTheDocument();
    expect(screen.getAllByRole('status')[0]).toHaveTextContent(/Grant revoked, log sealed/);
  });
});

describe('the building imagery panel', () => {
  it('is not on screen at all until there is an address to photograph', () => {
    renderConsole();
    // The panel used to sit in standby explaining, in a paragraph, that it had
    // nothing to show. A region that is only ever an explanation of itself is
    // not a region; it arrives with the incident that gives it an address.
    expect(screen.queryByRole('region', { name: 'Building imagery' })).not.toBeInTheDocument();
  });

  it('shows the photograph of the incident address, with its attribution', async () => {
    await dispatchIncident();
    const panel = screen.getByRole('region', { name: 'Building imagery' });
    const photo = await within(panel).findByRole('img');
    expect(photo).toHaveAttribute('src', IMAGERY.data_url);
    expect(photo).toHaveAccessibleName(new RegExp(ADDRESS));
    // The provider's terms require the credit to be on screen with the image.
    expect(within(panel).getByText('Imagery © 2026 Google')).toBeInTheDocument();
  });

  it('renders the reason when the backend answers 200 with no imagery', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/imagery': {
          ...IMAGERY,
          available: false,
          data_url: null,
          attribution: null,
          captured_hint: null,
          unavailable_reason: 'No street-level coverage at this address.',
        },
      }),
    );
    await dispatchIncident();
    const panel = screen.getByRole('region', { name: 'Building imagery' });
    expect(await within(panel).findByText(/No street-level coverage/)).toBeInTheDocument();
    expect(within(panel).queryByRole('img')).not.toBeInTheDocument();
    // A 200 carrying a reason is an answer, not a failed request.
    expect(within(panel).queryByText(/request failed/i)).not.toBeInTheDocument();
  });

  it('separates a failed request from an honest absence of coverage', async () => {
    const base = stubFetch();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/imagery')) {
          return new Response(
            JSON.stringify({
              error: { code: 'BACKEND_UNREACHABLE', message: 'imagery service timed out', details: {} },
            }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return base(input, init);
      }),
    );
    await dispatchIncident();
    const panel = screen.getByRole('region', { name: 'Building imagery' });
    expect(await within(panel).findByText(/Imagery request failed/)).toBeInTheDocument();
    expect(within(panel).getByText(/imagery service timed out/)).toBeInTheDocument();
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
    // The chips are gone; the one signal that separates a dead backend from an
    // empty district is not.
    expect(screen.getByTestId('backend-signal')).toHaveTextContent('Backend unreachable');
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
    fireEvent.click(screen.getByTestId('dispatch-button'));

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
   *
   * The header chip that used to carry it is gone with the rest of the status
   * chrome; the disclosure line carries it now, which is where the other
   * statement about what this console is and is not already lives.
   */
  beforeEach(() => {
    vi.stubGlobal('fetch', stubFetch());
  });

  it('marks calendar and mail simulated when live mode holds no Workspace authority', () => {
    renderConsole({ status: { ...STATUS, mode: 'live', workspace_writes: 'fake' } });
    expect(screen.getByRole('contentinfo')).toHaveTextContent(
      /calendar \+ mail: simulated/,
    );
    expect(screen.getByRole('contentinfo')).toHaveTextContent(/neither is sent/);
  });

  it('says nothing when a live deployment does reach Workspace', () => {
    renderConsole({ status: { ...STATUS, mode: 'live', workspace_writes: 'google' } });
    expect(screen.getByRole('contentinfo')).not.toHaveTextContent(/simulated/);
  });

  it('says nothing in fake mode, where every adapter is simulated anyway', () => {
    renderConsole({ status: { ...STATUS, mode: 'fake', workspace_writes: 'fake' } });
    expect(screen.getByRole('contentinfo')).not.toHaveTextContent(/simulated/);
  });
});

describe('the standby heartbeat', () => {
  /**
   * There was no polling at all: `refreshStandby` ran on mount and after an
   * action, so a console left open showed the district as it had been when the
   * page loaded. A screen that has stopped updating and a district where
   * nothing is happening looked identical, which is the same failure as an
   * empty district and a dead backend reading the same.
   */
  const districtReads = (mock: ReturnType<typeof stubFetch>) =>
    mock.mock.calls.filter(([input]) => String(input).includes('/queue')).length;

  /** Fake timers, and a flush that lets the fetches settle inside `act`. */
  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it('re-reads the district on an interval while nothing is burning', async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole();
      // Standby data was injected, so nothing has been fetched yet.
      expect(districtReads(fetchMock)).toBe(0);
      await tick(8000);
      expect(districtReads(fetchMock)).toBe(1);
      await tick(8000);
      expect(districtReads(fetchMock)).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops polling while an incident is open, leaving the stream uncontested', async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole();
      fireEvent.click(screen.getByRole('button', { name: ADDRESS }));
      await tick(0);
      fireEvent.click(screen.getByTestId('dispatch-button'));
      await tick(0);
      expect(screen.getByLabelText('Active incident')).toBeInTheDocument();

      const before = districtReads(fetchMock);
      await tick(40000);
      // The brief arrives on its own stream; a second loop against the same
      // backend would compete with it for a tablet's connection.
      expect(districtReads(fetchMock)).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears the interval on unmount', async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      const { unmount } = renderConsole();
      await tick(8000);
      const before = districtReads(fetchMock);
      expect(before).toBeGreaterThan(0);
      unmount();
      await tick(40000);
      expect(districtReads(fetchMock)).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });
});
