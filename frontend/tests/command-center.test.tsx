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

/**
 * How a structure is opened from standby now.
 *
 * The ranked chips are gone, so the disagreement list is the only path to a
 * profile on this screen, and its buttons name themselves fully -- two
 * controls with one accessible name is a screen reader saying the same address
 * twice with no way to tell them apart.
 */
const openDisagreement = (addressId: string) => `Open ${addressId}, records disagree`;

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
  fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
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
    const bar = screen.getByTestId('district-bar');
    // Four numbers, no captions. The bar carried six metrics and a caption
    // under each, all at eleven pixels -- twelve pieces of text for six
    // numbers, and the most important figure on the page set at the size of
    // its own footnote. `Facts` and `Dispatched` left: a count of records is a
    // vanity number, and companies-out belongs to the incident view.
    expect(within(bar).getByText('Structures')).toBeInTheDocument();
    expect(within(bar).getByText('Open conflicts')).toBeInTheDocument();
    expect(within(bar).getByText('Queued')).toBeInTheDocument();
    expect(within(bar).getByText('Never surveyed')).toBeInTheDocument();
    expect(within(bar).queryByText('Facts')).not.toBeInTheDocument();
  });

  it('draws a meter only where the backend reports both halves of the ratio', () => {
    renderConsole();
    const bar = screen.getByTestId('district-bar');
    // Every remaining count has an honest denominator on this payload, so all
    // four fill. The dashed track is still the answer for a ratio nobody
    // measured -- see the empty-district case below, which is the one that
    // exercises it now.
    expect(within(bar).getAllByTestId('meter')).toHaveLength(4);
    expect(within(bar).queryAllByTestId('meter-unscaled')).toHaveLength(0);
    expect(within(bar).getByTestId('source-ring')).toBeInTheDocument();
  });

  it('refuses to draw a proportion against a denominator of zero', () => {
    // A district with nothing on file has no proportion to draw, and drawing
    // one anyway would be inventing a scale. The tile keeps the rhythm of the
    // bar with a dashed track instead.
    renderConsole({
      initialStats: {
        ...STATS,
        profiles: 0,
        surveyed: 0,
        queued_for_survey: 0,
        profiles_never_surveyed: 0,
        open_conflicts: 0,
        high_severity_conflicts: 0,
      },
    });
    const bar = screen.getByTestId('district-bar');
    expect(within(bar).queryAllByTestId('meter')).toHaveLength(0);
    expect(within(bar).getAllByTestId('meter-unscaled')).toHaveLength(4);
  });

  it('does not call a source with no endpoint an outage', () => {
    // `tier-ii-confidential` is UNCONFIGURED: Tier II filings are confidential
    // under EPCRA, so there is no endpoint and there never will be. Counting it
    // beside a real outage put a red "3 UNAVAILABLE" on a district where
    // nothing was wrong, permanently -- which teaches an officer to read the
    // alarm colour as decoration. Why each source has no endpoint is stated on
    // the source panel, where a standing policy belongs.
    renderConsole();
    expect(screen.queryByText(/unreachable/)).not.toBeInTheDocument();
    expect(screen.getByText(/1 simulated/)).toBeInTheDocument();
  });

  it('still names a source that should have answered and did not', () => {
    // The other half of the same rule: suppressing the permanent case must not
    // suppress an outage. An absent record from a dead source is not an absent
    // record, and which source died is the part an officer can act on.
    renderConsole({
      initialStats: {
        ...STATS,
        sources: [
          ...STATS.sources,
          {
            source_id: 'sf-permits-live',
            mode: 'LIVE' as const,
            circuit_state: 'OPEN' as const,
            available: false,
            cache_hits: 0,
            upstream_calls: 3,
            last_snapshot_id: null,
          },
        ],
      },
    });
    expect(screen.getByText(/1 unreachable: sf-permits-live/)).toBeInTheDocument();
  });

  it('shows the fleet with publisher and pinned version', () => {
    renderConsole();
    expect(screen.getAllByText('records-watcher').length).toBeGreaterThan(0);
    expect(screen.getByText('building')).toBeInTheDocument();
    expect(screen.getAllByText('@1.0.0').length).toBeGreaterThan(0);
  });

  it('carries no survey ranking on screen at all', () => {
    // The backend still ranks -- `structure-watch` scores the district on every
    // pass and the queue endpoint still answers -- and none of it is drawn. A
    // rank, a score and a band of tied structures asked an officer to read an
    // ordering they could not act on differently row to row, and it took the
    // middle of the display to say so. What survives is the reason a structure
    // is worth looking at, which `RecordsDisagree` states in words.
    renderConsole();
    expect(screen.queryByLabelText('Ranked survey queue')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Ranked structures')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Ranked for survey' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^why$/i })).not.toBeInTheDocument();
    // The score the ranker produced for the top structure is nowhere on screen.
    expect(screen.queryByText('0.87')).not.toBeInTheDocument();
  });

  it('keeps the hand-run slow-loop pass, in the column that owns the loop', () => {
    renderConsole();
    const [fleet] = screen.getAllByRole('region', { name: /loop/i }) as [HTMLElement];
    expect(within(fleet).getByTestId('run-slow-loop-pass')).toBeInTheDocument();
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

  it('gives standby the same shape an incident uses: fleet, then the region', () => {
    renderConsole();
    // One fleet column, then everything else. The shape does not change at
    // dispatch, so an officer does not re-learn the screen at the moment a fire
    // starts -- only which loop the column is showing.
    const columns = screen.getAllByRole('region', { name: /loop/i });
    expect(columns).toHaveLength(1);

    const [fleet] = columns as [HTMLElement];
    const activity = screen.getByRole('region', { name: 'Regional fire activity' });
    const structures = screen.getByRole('region', { name: 'Records disagree' });

    expect(fleet.compareDocumentPosition(activity) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      activity.compareDocumentPosition(structures) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('gives the whole slow loop one column, never a half', () => {
    // The loop used to be cut down the middle across two flanks. One panel owns
    // one selection, so two panels would mean two panes for one fleet -- and a
    // column holding half the fleet attributes shared write targets against
    // half the fleet, which is how one agent's work surfaces in another's box.
    renderConsole();
    const [fleet] = screen.getAllByRole('region', { name: /loop/i }) as [HTMLElement];
    expect(fleet).toHaveAccessibleName('Slow loop');

    expect(within(fleet).getAllByText(/records-watcher/).length).toBeGreaterThan(0);
    expect(within(fleet).queryByText(/empty catalog/)).not.toBeInTheDocument();

    // It does not claim the incident loop, which is not running.
    expect(within(fleet).queryByText(/incident-controller/)).not.toBeInTheDocument();
  });

  it('lists a real slow fleet whole, in catalog order', () => {
    const slow = ['a', 'b', 'c', 'd', 'e'].map((id) => ({
      ...AGENTS[0]!,
      agent_id: `slow-${id}`,
      ref: `slow-${id}@1.0.0`,
    }));
    renderConsole({ initialAgents: [...slow, AGENTS[1]!] });
    const [fleet] = screen.getAllByRole('region', { name: /loop/i }) as [HTMLElement];

    // All five, in the order the registry published them. Catalog order and not
    // activity: a row that moved when its agent wrote a fact would be
    // unreadable at exactly the moment it was worth reading.
    const rows = within(fleet).getAllByTestId(/^fleet-row-slow-/);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'fleet-row-slow-a',
      'fleet-row-slow-b',
      'fleet-row-slow-c',
      'fleet-row-slow-d',
      'fleet-row-slow-e',
    ]);
    expect(fleet).toHaveTextContent('5 agents');
  });

  it('keeps the massing model out of standby until a structure is selected', async () => {
    renderConsole();
    expect(screen.queryByRole('region', { name: 'Structure' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Structure' })).toBeInTheDocument(),
    );
    // The camera views belong to the model wherever it is rendered.
    const model = screen.getByRole('region', { name: 'Structure' });
    expect(within(model).getByRole('group', { name: /fixed camera views/i })).toBeInTheDocument();
    // The fleet column did not go anywhere to make room for it.
    expect(screen.getAllByRole('region', { name: /loop/i })).toHaveLength(1);
    // The structure opens in the *middle* column, under the regional heat map
    // and before the findings rail -- which is the column it occupies during an
    // incident too, so the subject of the screen never changes place. It used
    // to open under the disagreement list, when both shared one column.
    const map = screen.getByRole('region', { name: 'Regional heat map' });
    const disagree = screen.getByRole('region', { name: 'Records disagree' });
    expect(
      map.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      model.compareDocumentPosition(disagree) & Node.DOCUMENT_POSITION_FOLLOWING,
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

  it('reports the detections as counts, with no map to misread', async () => {
    // The scatter drew dots on a black rectangle with no coastline, no grid
    // and no coordinates, and stretched to fill the column -- so the largest
    // thing on the console said "nothing is happening in your city" and gave
    // nobody a way to check where anything was. The counts say it plainly.
    renderConsole();
    const panel = await screen.findByRole('region', { name: 'Regional fire activity' });
    expect(within(panel).queryByTestId('fire-activity-scatter')).not.toBeInTheDocument();
    expect(within(panel).getByTestId('fire-activity-counts')).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));

    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    // Still the same page: the disagreement list is right where it was.
    expect(screen.getByLabelText('Structures where records disagree')).toBeInTheDocument();
  });

  it('shows provenance and all three assertion states', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
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
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
    await waitFor(() =>
      expect(screen.getAllByText(/Permit records 2 storeys/).length).toBeGreaterThan(0),
    );
    expect(screen.getByText('case REF-00001')).toBeInTheDocument();
    expect(screen.getByLabelText('Profile timeline, newest first')).toBeInTheDocument();
  });

  it('will not offer to settle a conflict outside an incident', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
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

  it('reorganises at dispatch: the incident fleet, then the structure', async () => {
    await dispatch();
    const columns = screen.getAllByRole('region', { name: /loop/i });
    expect(columns).toHaveLength(1);

    const [incidentFleet] = columns as [HTMLElement];
    // The column carries the agents acting right now, whole and unsplit.
    expect(within(incidentFleet).getAllByText(/incident-controller/).length).toBeGreaterThan(0);
    // Not the slow loop: that leaves the screen at dispatch and says so in a
    // line of its own, which the test below this one pins.
    expect(within(incidentFleet).queryByText(/records-watcher/)).not.toBeInTheDocument();

    // And the structure sits beside it, still split model-then-photograph.
    const model = screen.getByRole('region', { name: 'Structure' });
    const imagery = screen.getByRole('region', { name: 'Building imagery' });
    expect(
      model.compareDocumentPosition(incidentFleet) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    expect(
      model.compareDocumentPosition(imagery) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('gives the brief a column of its own, ahead of the building in the source', async () => {
    await dispatch();
    const [incidentFleet] = screen.getAllByRole('region', { name: /loop/i }) as [HTMLElement];
    const brief = screen.getByRole('region', { name: 'Incident brief' });
    const model = screen.getByRole('region', { name: 'Structure' });

    // Three columns, and the brief is not inside the one carrying the model
    // any more: a brief that ran the full width under the building pushed the
    // building off the top of the screen as its three stages filled in.
    expect(brief.contains(model)).toBe(false);
    expect(model.contains(brief)).toBe(false);

    // Source order is the reading order when the columns stack on a tablet, so
    // the brief sits above the model rather than under the profile timeline.
    // On a wide screen two explicit column starts put it back on the right.
    expect(
      incidentFleet.compareDocumentPosition(brief) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(brief.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('names the fleet column after the loop it was asked for', async () => {
    await dispatch();
    // The console names the loop and the panel answers which agents match.
    // Nothing here filters the agent list -- two places deciding that is how
    // the two answers drift apart -- so the heading is the contract.
    const [incidentFleet] = screen.getAllByRole('region', { name: /loop/i }) as [HTMLElement];
    expect(incidentFleet).toHaveAccessibleName(/incident loop/i);
  });

  it('shows the elapsed clock and the incident identity', async () => {
    await dispatch();
    const banner = screen.getByLabelText('Active incident');
    // The street address leads: nobody rolls to a slug. The id stays on screen
    // in parentheses, because it is what every event and log entry is keyed by.
    expect(within(banner).getByText('450 Hayes St')).toBeInTheDocument();
    expect(within(banner).getByText(`(${ADDRESS})`)).toBeInTheDocument();
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
    expect(screen.getByLabelText('Structures where records disagree')).toBeInTheDocument();
    expect(screen.getAllByRole('status')[0]).toHaveTextContent(/Grant revoked, log sealed/);
  });

  /**
   * Hold the console's POST to `fragment` open, and hand back the release.
   *
   * The in-flight label exists only while a request is open, so a test that
   * wants to read it has to keep one open. Against the ordinary stub, which
   * answers on the next microtask, the write is finished before an assertion
   * can run and the test would pass just as happily against the bug below.
   */
  function holdOpen(fragment: string) {
    const base = stubFetch();
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes(fragment) && init?.method === 'POST') await held;
      return base(input, init);
    });
    return release;
  }

  /**
   * Every write on this screen shared one busy flag, and the only control that
   * put that flag into words was the banner's close button -- so a chief who
   * asked `agency-notifier` to tell the water department was told, at the top
   * of a live incident, that the incident was being closed.
   */
  it('says it is notifying, not closing, while a resource request is open', async () => {
    const release = holdOpen('/resources');
    await dispatch();

    fireEvent.click(screen.getByRole('button', { name: /water department/i }));

    const label = await screen.findByTestId('in-flight-status');
    expect(label).toHaveTextContent(/notifying/i);
    expect(label).not.toHaveTextContent(/closing/i);
    // And the banner is not quietly saying it either: the close control is
    // still offering to close, because nobody has asked it to.
    expect(screen.getByRole('button', { name: /close incident/i })).toBeInTheDocument();

    await act(async () => {
      release();
    });
  });

  it('says it is closing while the close is open', async () => {
    const release = holdOpen('/close');
    await dispatch();

    fireEvent.click(screen.getByRole('button', { name: /close incident/i }));

    const label = await screen.findByTestId('in-flight-status');
    expect(label).toHaveTextContent(/closing/i);
    expect(label).not.toHaveTextContent(/notifying/i);

    await act(async () => {
      release();
    });
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

  /**
   * Open the panel and switch it to a photograph.
   *
   * The panel opens on the `3d` tile view, which streams in the browser and
   * contacts no imagery endpoint. These tests are about what the *photograph*
   * path does, so they ask for it explicitly rather than relying on whichever
   * viewpoint happens to be the default.
   */
  async function openPhotograph() {
    await dispatchIncident();
    const panel = screen.getByRole('region', { name: 'Building imagery' });
    fireEvent.click(within(panel).getByTestId('imagery-view-street'));
    return panel;
  }

  it('shows the photograph of the incident address, with its attribution', async () => {
    const panel = await openPhotograph();
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
    const panel = await openPhotograph();
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
    const panel = await openPhotograph();
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
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
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

  it('shows an honest empty district rather than invented rows', () => {
    renderConsole({ initialQueue: { district_id: 'd', entries: [], count: 0 } });
    expect(
      screen.getByText('No structure in this district has an open disagreement.'),
    ).toBeInTheDocument();
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

  /** The fleet's own evidence, which is read on its own timer. */
  const auditReads = (mock: ReturnType<typeof stubFetch>) =>
    mock.mock.calls.filter(([input]) => String(input).includes('/audit/events')).length;

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
      // Live mode, so the heartbeat is measured on its own.
      //
      // Under the demo choreography a slow-loop pass runs shortly after load
      // and re-reads the district when it finishes, which is a second reader of
      // the same endpoint. That is intended -- see `FIRST_PASS_MS` -- and it is
      // not what this test is about, so the choreography is off here and every
      // read counted below is the interval's own.
      renderConsole({ status: { ...STATUS, mode: 'live' } });
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

  it('keeps reading the fleet audit log while an incident is open', async () => {
    // The district poll stops when an incident opens, and the fleet read used
    // to ride it. So for the whole incident the console held the audit log as
    // it stood at dispatch, and `incident-interceptor`, `incident-recorder` and
    // `sensor-fusion` -- which record everything they do there -- were drawn
    // idle through the ninety seconds they were busiest. They were working;
    // nobody asked again.
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole();
      fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
      await tick(0);
      fireEvent.click(screen.getByTestId('dispatch-button'));
      await tick(0);
      expect(screen.getByLabelText('Active incident')).toBeInTheDocument();

      const before = auditReads(fetchMock);
      await tick(20000);
      expect(auditReads(fetchMock)).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('starts the choreography’s first pass shortly after load, not a minute in', async () => {
    // Twenty-five seconds of an untouched screen, with every slow-loop agent
    // reading idle, is indistinguishable from a console that is broken.
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole();
      const polls = () =>
        fetchMock.mock.calls.filter(([input]) => String(input).includes('/poll')).length;
      expect(polls()).toBe(0);
      await tick(4000);
      expect(polls()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('leaves the operator’s own pass button alone while the page is arriving', async () => {
    // The lead-in is a few seconds rather than immediate for this reason: a
    // pass fired on mount disables the one manual control on screen before
    // anyone has had a chance to reach it.
    renderConsole();
    expect(screen.getByTestId('run-slow-loop-pass')).not.toBeDisabled();
  });

  it('reads the fleet immediately on load rather than one interval later', async () => {
    // A fresh console draws the fleet from an empty list. Waiting for the first
    // tick showed every agent in the catalog as idle for the opening seconds of
    // the demo, which is when someone is looking hardest.
    vi.useFakeTimers();
    const fetchMock = stubFetch();
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole();
      await tick(0);
      expect(auditReads(fetchMock)).toBeGreaterThan(0);
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
      fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
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

/**
 * A restart onto a log that outlived it.
 *
 * `make live-demo` runs against a real Firestore, which keeps the audit log
 * across server restarts, and every other test in this suite starts from an
 * empty log -- so the state a live console actually mounts into, a log already
 * holding a complete previous pass, had no coverage at all. That is the gap two
 * "fixed" attempts fell through: both scoped a counter to a unit of work read
 * *out of* the log, and on a fresh restart the newest pass in the log belongs
 * to the last run, so the console anchored on it and displayed its totals.
 *
 * Mounted with no injected events, so the floor is taken from the first audit
 * read the way it is on a real load.
 */
describe('a console restarted onto a live log it did not watch fill', () => {
  const WATCHER = 'records-watcher';
  const OTHER = 'structure-watch';

  /** A second slow agent, so "every agent" means more than one row. */
  const FLEET_AGENTS = [
    ...AGENTS,
    { ...AGENTS[0]!, agent_id: OTHER, ref: `${OTHER}@1.0.0` },
  ];

  function step(over: Record<string, unknown>) {
    return {
      audit_id: `audit_${Math.random().toString(36).slice(2)}`,
      kind: 'agent_step',
      occurred_at: '2026-08-20T07:00:00+00:00',
      actor: WATCHER,
      target: ADDRESS,
      incident_id: null,
      correlation_id: 'corr_last_run',
      detail: {},
      ...over,
    };
  }

  /** What Firestore hands back the instant the console reconnects: a complete
   *  slow-loop pass, every event of it from the run before this one. */
  const LAST_RUN = [
    step({ occurred_at: '2026-08-20T07:00:00+00:00' }),
    step({ occurred_at: '2026-08-20T07:00:01+00:00' }),
    step({ occurred_at: '2026-08-20T07:00:02+00:00', kind: 'agent_pass' }),
    step({ occurred_at: '2026-08-20T07:00:03+00:00', actor: OTHER }),
    step({ occurred_at: '2026-08-20T07:00:04+00:00', actor: OTHER, kind: 'agent_pass' }),
  ];

  /** The same log, plus one step this session watched land. */
  const THIS_SESSION = [
    ...LAST_RUN,
    step({ occurred_at: '2026-08-20T08:30:00+00:00', correlation_id: 'corr_this_run' }),
  ];

  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it('reads every agent idle at zero recorded, whatever the log already held', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', stubFetch({ '/audit/events': LAST_RUN, '/audit/decisions': [] }));
    try {
      renderConsole({ initialEvents: [], initialDecisions: [], initialAgents: FLEET_AGENTS });
      await tick(0);
      for (const id of [WATCHER, OTHER]) {
        const row = screen.getByTestId(`fleet-row-${id}`);
        expect(row).toHaveTextContent('0 recorded');
        expect(row).toHaveTextContent('idle');
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('climbs in real time once the fleet writes something this session watched', async () => {
    vi.useFakeTimers();
    // The first read returns the previous run and every read after it returns
    // that plus one new step, which is what a poll against an accumulating log
    // looks like.
    let reads = 0;
    const fallback = stubFetch({ '/audit/decisions': [] });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/audit/events')) {
        reads += 1;
        return new Response(JSON.stringify(reads === 1 ? LAST_RUN : THIS_SESSION), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return fallback(input, init);
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      renderConsole({ initialEvents: [], initialDecisions: [], initialAgents: FLEET_AGENTS });
      await tick(0);
      expect(screen.getByTestId(`fleet-row-${WATCHER}`)).toHaveTextContent('0 recorded');

      await tick(3000);
      const row = screen.getByTestId(`fleet-row-${WATCHER}`);
      expect(row).toHaveTextContent('1 recorded');
      expect(row).toHaveTextContent('active');
      // And the last run is still not this session's, however long it stays in
      // the log the console is accumulating.
      expect(screen.getByTestId(`fleet-row-${OTHER}`)).toHaveTextContent('0 recorded');
    } finally {
      vi.useRealTimers();
    }
  });
});

/**
 * Opening a structure has to look like something happened.
 *
 * The middle column holds the region above and the structure below, and the
 * map is deliberately tall -- it is the subject of the standby screen and it
 * takes the whole column. So a profile opened from a conflict card renders
 * six hundred pixels below the fold, inside a pane that scrolls on its own.
 * Everything worked; nothing moved; the card just took focus and sat there.
 * An officer clicks the disagreement they were told to look at and concludes
 * the console is broken.
 *
 * The profile is brought to the top of its column when it opens, which is the
 * one thing that makes the click legible as having done anything.
 */
describe('opening a structure from a conflict', () => {
  it('scrolls the pane the profile is in, and never the document', async () => {
    // `scrollIntoView` walks every scrollable ancestor, document included. The
    // shell is one viewport tall and the document does not scroll, so a
    // document moved by script has no scrollbar to put it back: it stays
    // there, the shell hangs off the top of the window, and the space it used
    // to fill shows as a band of empty ground under the footer -- growing
    // every time somebody opens a building.
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
    const scrollBy = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollBy', {
      configurable: true,
      writable: true,
      value: scrollBy,
    });

    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: openDisagreement(ADDRESS) }));
    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());

    // The guard that matters, and the one this environment can actually make:
    // `scrollIntoView` is the call that moves the page, and it is never used.
    // Which pane gets moved instead is a question about real layout -- jsdom
    // loads no stylesheet, so nothing here computes as scrollable and the walk
    // correctly finds nothing to move. That half is verified in a browser.
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(scrollBy).not.toHaveBeenCalled();
  });
});
