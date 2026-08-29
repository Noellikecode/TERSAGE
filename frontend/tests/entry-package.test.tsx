/**
 * The entry package, from the log frame that raises it to the crew it is sent to.
 *
 * Five claims are under test here, and every one of them is a thing that would
 * be dangerous rather than merely wrong if it broke:
 *
 * 1. A NOT-READY package is marked as one. The whole readiness assessment
 *    exists to stop a gap going unstated, and a console that rendered a failed
 *    verdict as a neutral badge would be the failure it was built to prevent.
 * 2. The send is refused until both halves are signed. The backend refuses with
 *    a 422; a console that let the button through would put a crew's package one
 *    request away from an error it had already been told about.
 * 3. A refused path renders its reason and **no route**. There is no fallback
 *    route in the backend, so anything drawn here would be an invention.
 * 4. Waypoints with no WGS-84 coordinates say so. The city could not place the
 *    address, and no origin is supplied from anywhere else.
 * 5. Nothing is called sent until the send returned, and only a send that
 *    returned resolves the incident back to standby.
 */

import { useState } from 'react';

import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CommandCenter } from '@/components/CommandCenter';
import { EntryPackageModal } from '@/components/incident/EntryPackageModal';
import {
  EntryPathSummary,
  ReadinessVerdict,
} from '@/components/incident/EntryPackageParts';
import {
  ROUTE_DRAW_BUDGET_MS,
  ROUTE_EGRESS_GAP_MS,
  ROUTE_LEG_MS,
  StructureModel,
  routeDrawSchedule,
  routeDrawState,
  type RouteOverlay,
} from '@/components/StructureModel';
import { gatewayTargetPath } from '@/lib/api/gateway-allowlist';
import {
  ENTRY_PACKAGE_POLL_MS,
  downloadEntryPackagePdf,
  foldPackages,
  foldPolled,
  packageFromLogEntry,
  packageProgress,
  summaryProgress,
  useEntryPackages,
} from '@/lib/api/entry-packages';
import type {
  CriterionView,
  EntryPackageView,
  EntryPathPlanView,
  PackageSummaryView,
  ReadinessAssessmentView,
  RouteView,
} from '@/lib/api/types';
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
} from './fixtures';

// ------------------------------------------------------------- fixtures --

function criterion(
  id: string,
  title: string,
  passed: boolean,
  reason: string,
): CriterionView {
  return { criterion_id: id, title, passed, reason, refs: [`${id}-ref`] };
}

const PASSING: CriterionView[] = [
  criterion('geometry.present', 'Pre-incident geometry exists', true, '4 footprint vertices'),
  criterion('thermal.coverage', 'Every face has current thermal coverage', true, 'all 4 faces'),
  criterion('hazard.resolved', 'Hazard attributes are resolved', true, 'all 5 known'),
  criterion('conflicts.load-bearing', 'No open conflict on a load-bearing attribute', true, 'none open'),
  criterion('snapshot.fresh', 'The profile snapshot is current', true, 'read 40 s ago'),
  criterion('intake.access-bound', 'A narrative bound an access attribute', true, '1 narrative read'),
];

function assessment(ready: boolean): ReadinessAssessmentView {
  const criteria = ready
    ? PASSING
    : [
        ...PASSING.slice(0, 1),
        criterion(
          'thermal.coverage',
          'Every face has current thermal coverage',
          false,
          'CHARLIE is UNSCANNED and lapsed. UNSCANNED is unknown, never safe.',
        ),
        ...PASSING.slice(2, 4),
        criterion(
          'snapshot.fresh',
          'The profile snapshot is current',
          false,
          'the snapshot was read 2400 s ago, outside the 900 s window',
        ),
        PASSING[5]!,
      ];
  const failed = criteria.filter((c) => !c.passed).map((c) => c.criterion_id);
  return {
    incident_id: 'inc-1',
    address_id: ADDRESS,
    assessed_at: '2026-08-20T08:02:00+00:00',
    assessed_by: 'incident-interceptor',
    assessed_by_version: '1.0.0',
    profile_snapshot_id: 'snap_abc',
    criteria,
    ready,
    failed_ids: failed,
    summary: ready
      ? 'READY - all 6 criteria pass'
      : `NOT READY - 4 of 6 criteria pass; outstanding: ${failed.join(', ')}`,
  };
}

/** A two-leg route. `placed` decides whether the city could geocode the parcel. */
function route(placed: boolean): RouteView {
  const at = (n: number) => ({
    longitude: placed ? -122.42 + n * 0.0001 : null,
    latitude: placed ? 37.777 + n * 0.0001 : null,
  });
  return {
    waypoints: [
      { node_id: 'stage-ALPHA', kind: 'staging', face: 'ALPHA', level: null, x_m: 5.75, y_m: -6, z_m: 0, ...at(0) },
      { node_id: 'door-ALPHA', kind: 'door', face: 'ALPHA', level: 0, x_m: 5.75, y_m: 0, z_m: 0, ...at(1) },
      { node_id: 'core-L0', kind: 'core', face: '', level: 0, x_m: 5.75, y_m: 11, z_m: 1.6, ...at(2) },
    ],
    legs: [
      {
        from_id: 'stage-ALPHA',
        to_id: 'door-ALPHA',
        distance_m: 6,
        cost: 6,
        multiplier: 1,
        terms: [],
        avoided: [],
        chose_because: 'distance and nothing else: ALPHA measured 31 C, below the baseline.',
      },
      {
        from_id: 'door-ALPHA',
        to_id: 'core-L0',
        distance_m: 11.1,
        cost: 18.4,
        multiplier: 1.66,
        terms: [
          {
            term_id: 'thermal.measured',
            weight: 0.66,
            detail: 'ALPHA measured 210 C peak surface temperature',
            refs: ['ALPHA'],
          },
        ],
        avoided: ['a CHARLIE approach nobody has flown'],
        chose_because: 'the interior leg is priced for a wall measured 210 C and still beats the unflown side.',
      },
    ],
    total_cost: 24.4,
    total_distance_m: 17.1,
    expanded_nodes: 9,
  };
}

function path(overrides: Partial<EntryPathPlanView> = {}): EntryPathPlanView {
  return {
    incident_id: 'inc-1',
    address_id: ADDRESS,
    algorithm: 'A*',
    heuristic: 'euclidean-3d',
    target_level: 0,
    refused: false,
    refusal_reason: '',
    refusal_refs: [],
    entry: route(true),
    egress: null,
    egress_note: 'No second way out: every other face is at or above the thermal barrier.',
    barriers: [],
    unscanned_faces: ['CHARLIE'],
    node_count: 12,
    edge_count: 21,
    entry_face: 'ALPHA',
    ...overrides,
  };
}

function entryPackage(overrides: Partial<EntryPackageView> = {}): EntryPackageView {
  const base: EntryPackageView = {
    package_id: 'pkg_1a2b',
    incident_id: 'inc-1',
    address_id: ADDRESS,
    created_at: '2026-08-20T08:02:00+00:00',
    created_by: 'incident-interceptor',
    created_by_version: '1.0.0',
    assessment: assessment(false),
    path: path(),
    brief: {
      brief_id: 'brf_1',
      incident_id: 'inc-1',
      address_id: ADDRESS,
      composed_at: '2026-08-20T08:02:00+00:00',
      composed_by: 'incident-interceptor',
      composed_by_version: '1.0.0',
      profile_snapshot_id: 'snap_abc',
      claims: [
        {
          claim_id: 'readiness.verdict',
          section: 'READINESS',
          text: 'NOT READY - 4 of 6 criteria pass',
          refs: ['snap_abc'],
        },
        {
          claim_id: 'route.entry',
          section: 'ROUTE',
          text: 'The cheapest traverse enters on ALPHA and reaches storey 1.',
          refs: ['door-ALPHA'],
        },
      ],
      prose: 'Three storeys over a four-sided footprint.\nCHARLIE has not been flown.',
      prose_source: 'deterministic',
      prose_rejection: '',
      model_ref: '',
      unknowns: ['hazard.solar_array'],
      readiness_summary: 'NOT READY - 4 of 6 criteria pass',
      claim_refs: ['snap_abc', 'door-ALPHA'],
    },
    path_approval_id: 'apr_pkg_1a2b_entry-path',
    brief_approval_id: 'apr_pkg_1a2b_crew-brief',
    path_approved_by: null,
    path_approved_at: null,
    brief_approved_by: null,
    brief_approved_at: null,
    sent_at: null,
    sent_by: null,
    dispatch_decision_id: '',
    disclaimer: 'Decision support. Every tactical decision belongs to the incident commander.',
    path_approved: false,
    brief_approved: false,
    status: 'AWAITING_APPROVAL',
    outstanding_halves: ['entry-path', 'crew-brief'],
  };
  return { ...base, ...overrides };
}

/** The same package with one half stamped, exactly as the backend returns it. */
function withHalf(base: EntryPackageView, half: 'entry-path' | 'crew-brief'): EntryPackageView {
  const pathApproved = base.path_approved || half === 'entry-path';
  const briefApproved = base.brief_approved || half === 'crew-brief';
  const outstanding: EntryPackageView['outstanding_halves'] = [];
  if (!pathApproved) outstanding.push('entry-path');
  if (!briefApproved) outstanding.push('crew-brief');
  return {
    ...base,
    path_approved: pathApproved,
    path_approved_at: pathApproved ? '2026-08-20T08:03:00+00:00' : null,
    path_approved_by: pathApproved ? 'bc-09' : null,
    brief_approved: briefApproved,
    brief_approved_at: briefApproved ? '2026-08-20T08:03:30+00:00' : null,
    brief_approved_by: briefApproved ? 'bc-09' : null,
    outstanding_halves: outstanding,
    status: outstanding.length === 0 ? 'READY_TO_SEND' : 'AWAITING_APPROVAL',
  };
}

/**
 * The row the *list* endpoint returns for a package: ids, statuses and counts.
 *
 * Built from the document rather than authored beside it, so a test cannot
 * accidentally describe a summary the backend would never send for that
 * package -- which is the one way a poll test could pass against a fiction.
 */
function summaryOf(held: EntryPackageView): PackageSummaryView {
  return {
    package_id: held.package_id,
    status: held.status,
    created_at: held.created_at,
    ready: held.assessment.ready,
    path_refused: held.path.refused,
    outstanding: [...held.outstanding_halves],
    sent_at: held.sent_at,
  };
}

// -------------------------------------------------------------- the modal --

/** Renders the modal and folds every write's answer back in, like the console. */
function renderModal(initial: EntryPackageView, onDispatched = vi.fn()) {
  function Harness() {
    const [held, setHeld] = useState(initial);
    return (
      <EntryPackageModal
        incidentId="inc-1"
        entryPackage={held}
        autonomyTrigger="deadline"
        onUpdated={setHeld}
        onClose={() => {}}
        onDispatched={onDispatched}
      />
    );
  }
  return render(<Harness />);
}

describe('the readiness verdict', () => {
  it('marks a NOT READY package as not ready and names what failed', () => {
    render(<ReadinessVerdict assessment={assessment(false)} />);
    const banner = screen.getByTestId('readiness-banner');
    // The attribute is the machine-readable half; the words are the half an
    // officer reads, and both have to say the same thing.
    expect(banner).toHaveAttribute('data-ready', 'false');
    expect(within(banner).getByText('Not ready')).toBeInTheDocument();
    expect(within(banner).getByText(/NOT READY - 4 of 6 criteria pass/)).toBeInTheDocument();
    expect(within(banner).queryByText('Ready')).not.toBeInTheDocument();
    // The failed criteria carry their reasons, not just their names: "we could
    // not check" and "we checked and it is fine" are the two things this whole
    // system exists to keep apart.
    expect(screen.getByText(/CHARLIE is UNSCANNED and lapsed/)).toBeInTheDocument();
    expect(screen.getByText(/outside the 900 s window/)).toBeInTheDocument();
  });

  it('sorts the failures above the passes so they cannot be scrolled past', () => {
    render(<ReadinessVerdict assessment={assessment(false)} />);
    const items = screen.getAllByTestId(/^criterion-/);
    expect(items[0]).toHaveAttribute('data-testid', 'criterion-thermal.coverage');
    expect(items[1]).toHaveAttribute('data-testid', 'criterion-snapshot.fresh');
  });

  it('renders all six criteria whichever way the verdict went', () => {
    render(<ReadinessVerdict assessment={assessment(true)} />);
    expect(screen.getAllByTestId(/^criterion-/)).toHaveLength(6);
    expect(screen.getByTestId('readiness-banner')).toHaveAttribute('data-ready', 'true');
  });
});

describe('the card is the interceptor asking', () => {
  it('names incident-interceptor in its own title', () => {
    renderModal(entryPackage());
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const title = document.getElementById('entry-package-title');
    expect(title).toHaveTextContent('incident-interceptor');
    expect(title).toHaveTextContent(/asks for approval to send an entry package/i);
  });

  it('says how the loop decided to compose it rather than leaving it implied', () => {
    renderModal(entryPackage());
    // `deadline` is not `ready`. A console that could not tell them apart would
    // render "the clock ran out" with the confidence of "all six passed".
    expect(screen.getByText(/compose deadline ran out/i)).toBeInTheDocument();
  });

  it('carries the NOT READY verdict onto the card, unsoftened', () => {
    renderModal(entryPackage());
    expect(screen.getByTestId('readiness-banner')).toHaveAttribute('data-ready', 'false');
  });

  /**
   * A caveat printed before the thing it qualifies reads as a refusal.
   *
   * The dialog opened on the readiness verdict, so a commander met the words
   * "Not ready" before a single line of the plan. The verdict does not block a
   * send and the copy says so at length -- it belongs after the brief and the
   * route, as a note on them. This pins the order, because it is the kind of
   * thing a later edit reshuffles without noticing what it costs.
   */
  it('leads with the plan and puts the record check after it', () => {
    renderModal(entryPackage());
    const modal = screen.getByTestId('entry-package-modal');
    const brief = screen.getByTestId('crew-brief');
    const readiness = screen.getByTestId('readiness-verdict');
    // `compareDocumentPosition` is the honest check: it asks the DOM which
    // comes first rather than trusting a query order.
    expect(brief.compareDocumentPosition(readiness) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(modal).toContainElement(brief);
    expect(modal).toContainElement(readiness);
  });

  it('does not paint an incomplete record in the colour reserved for faults', () => {
    renderModal(entryPackage());
    // `alarm` is for something that has gone wrong. An unconfirmed record has
    // not gone wrong, and the banner's own sentence says so.
    const banner = screen.getByTestId('readiness-banner');
    expect(banner.className).toContain('border-disputed');
    expect(banner.className).not.toContain('border-alarm');
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    render(
      <EntryPackageModal
        incidentId="inc-1"
        entryPackage={entryPackage()}
        onUpdated={() => {}}
        onClose={onClose}
        onDispatched={() => {}}
      />,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('two approvals and one send', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const answer = (value: unknown) =>
          new Response(JSON.stringify(value), {
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.endsWith('/approvals/entry-path')) {
          return answer(withHalf(entryPackage(), 'entry-path'));
        }
        if (url.endsWith('/approvals/crew-brief')) {
          return answer(withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief'));
        }
        if (url.endsWith('/dispatch')) {
          const both = withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief');
          return answer({
            ...both,
            status: 'SENT',
            sent_at: '2026-08-20T08:04:00+00:00',
            sent_by: 'bc-09',
            dispatch_decision_id: 'dec_77',
            outstanding_halves: [],
          });
        }
        return answer({});
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it('refuses the release while either half is unsigned', () => {
    renderModal(entryPackage());
    expect(screen.getByTestId('entry-package-release')).toBeDisabled();
  });

  it('leaves the release disabled after only one half is approved', async () => {
    renderModal(entryPackage());
    fireEvent.click(screen.getByTestId('approve-entry-path'));
    await screen.findByTestId('approve-entry-path-granted');
    // One judgement is not the other. The path being a route somebody would
    // send a crew down says nothing about the brief being accurate.
    expect(screen.getByTestId('entry-package-release')).toBeDisabled();
    expect(screen.getByTestId('outstanding-line')).toHaveTextContent(/crew-brief/);
  });

  it('enables the release only once both halves are granted', async () => {
    renderModal(entryPackage());
    fireEvent.click(screen.getByTestId('approve-entry-path'));
    await screen.findByTestId('approve-entry-path-granted');
    fireEvent.click(screen.getByTestId('approve-crew-brief'));
    await screen.findByTestId('approve-crew-brief-granted');
    await waitFor(() => expect(screen.getByTestId('entry-package-release')).toBeEnabled());
  });

  it('takes one tap for both halves and still writes both approvals', async () => {
    renderModal(entryPackage());
    fireEvent.click(screen.getByTestId('approve-both'));
    await screen.findByTestId('approve-crew-brief-granted');
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((call) =>
      String(call[0]),
    );
    expect(calls).toContain('/api/gateway/api/v1/incidents/inc-1/entry-packages/pkg_1a2b/approvals/entry-path');
    expect(calls).toContain('/api/gateway/api/v1/incidents/inc-1/entry-packages/pkg_1a2b/approvals/crew-brief');
  });

  it('reports a send only after the dispatch call returns', async () => {
    const onDispatched = vi.fn();
    renderModal(withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief'), onDispatched);
    const release = screen.getByTestId('entry-package-release');
    expect(release).toBeEnabled();
    // Before the tap nothing claims a send, however signed the package is.
    expect(screen.getByTestId('outstanding-line')).not.toHaveTextContent(/sent to live dispatch/i);
    fireEvent.click(release);
    await waitFor(() => expect(onDispatched).toHaveBeenCalledTimes(1));
    expect(onDispatched.mock.calls[0]![0].status).toBe('SENT');
    expect(screen.getByTestId('outstanding-line')).toHaveTextContent(/sent to live dispatch units/i);
  });

  it('says nothing was sent when the send is refused', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: {
                code: 'UNPROCESSABLE',
                message: 'both halves must be approved',
                details: {},
                request_id: null,
                correlation_id: null,
              },
            }),
            { status: 422, headers: { 'Content-Type': 'application/json' } },
          ),
      ),
    );
    const onDispatched = vi.fn();
    renderModal(withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief'), onDispatched);
    fireEvent.click(screen.getByTestId('entry-package-release'));
    expect(await screen.findByRole('alert')).toHaveTextContent(/nothing was sent/i);
    expect(onDispatched).not.toHaveBeenCalled();
  });
});

describe('a refused path', () => {
  it('renders the reason instead of a route, and draws no legs', () => {
    render(
      <EntryPathSummary
        path={path({
          refused: true,
          refusal_reason:
            'no pre-incident geometry: the slow loop never measured this address, so there is nothing a route could be computed over',
          refusal_refs: [ADDRESS, 'snap_abc'],
          entry: null,
          egress: null,
        })}
        selection={null}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByTestId('path-refused')).toBeInTheDocument();
    expect(screen.getByText(/never measured this address/)).toBeInTheDocument();
    expect(screen.queryByTestId('path-summary')).not.toBeInTheDocument();
    expect(screen.queryByTestId('route-leg-0')).not.toBeInTheDocument();
  });

  it('says outright that nothing is drawn on the model for it', () => {
    render(
      <EntryPathSummary
        path={path({ refused: true, refusal_reason: 'the goal is unreachable', entry: null })}
        selection={null}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/no fallback route and no straight line/i)).toBeInTheDocument();
  });
});

describe('the reasoning on each leg', () => {
  it('puts chose_because and what it beat on every leg', () => {
    render(<EntryPathSummary path={path()} selection={null} onSelect={() => {}} />);
    expect(screen.getByTestId('route-leg-1')).toHaveTextContent(
      /priced for a wall measured 210 C and still beats the unflown side/,
    );
    expect(screen.getByTestId('route-leg-1')).toHaveTextContent(
      /avoided: a CHARLIE approach nobody has flown/,
    );
  });

  it('reports selecting a leg so the model can draw it brighter', () => {
    const onSelect = vi.fn();
    render(<EntryPathSummary path={path()} selection={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('route-leg-0'));
    expect(onSelect).toHaveBeenCalledWith({ route: 'entry', leg: 0 });
  });
});

describe('coordinates the city could not supply', () => {
  it('states that an unplaced route cannot be put on a map, and invents no origin', () => {
    render(
      <EntryPathSummary path={path({ entry: route(false) })} selection={null} onSelect={() => {}} />,
    );
    expect(screen.getByText(/the city could not place this address/i)).toBeInTheDocument();
    expect(screen.getByText(/it cannot be put on a map/i)).toBeInTheDocument();
  });

  it('says so plainly when every waypoint was placed', () => {
    render(<EntryPathSummary path={path()} selection={null} onSelect={() => {}} />);
    expect(screen.getByText(/Every waypoint carries WGS-84 coordinates/)).toBeInTheDocument();
  });
});

describe('the route on the structure model', () => {
  it('draws nothing before a package exists', () => {
    render(<StructureModel geometry={GEOMETRY} forceFallback route={null} />);
    expect(screen.queryByTestId('route-caption')).not.toBeInTheDocument();
  });

  it('accounts for the route once a package carries one', () => {
    render(
      <StructureModel
        geometry={GEOMETRY}
        forceFallback
        route={{ entry: route(true), egress: null, highlight: null, drawKey: 'pkg_1a2b' }}
      />,
    );
    // WebGL is unavailable in jsdom, so this is the static elevation -- which
    // cannot carry an overlay and says so rather than going quiet about a route
    // the console is holding.
    expect(screen.getByTestId('route-caption')).toHaveTextContent(
      /not on the static elevation/i,
    );
  });
});

// --------------------------------------------------- the route's draw ------

/**
 * A route of `legs` legs, in the order the search returns its waypoints.
 *
 * Positions are only ever read as "one leg apart" here: what is under test is
 * the *timetable*, and the timetable counts legs the plan contains and never
 * measures them. jsdom has no WebGL, so this is the half of the animation that
 * can be checked honestly -- which is also the half that decides when a human
 * gets interrupted.
 */
function routeOfLegs(legs: number): RouteView {
  return {
    waypoints: Array.from({ length: legs + 1 }, (_, index) => ({
      node_id: `n-${index}`,
      kind: index === 0 ? 'staging' : index === 1 ? 'door' : 'interior',
      face: 'ALPHA',
      level: 0,
      x_m: index,
      y_m: 0,
      z_m: 0,
      longitude: null,
      latitude: null,
    })),
    legs: [],
    total_cost: 0,
    total_distance_m: 0,
    expanded_nodes: 0,
  };
}

function overlayOf(entryLegs: number, egressLegs = 0): RouteOverlay {
  return {
    entry: entryLegs > 0 ? routeOfLegs(entryLegs) : null,
    egress: egressLegs > 0 ? routeOfLegs(egressLegs) : null,
    highlight: null,
    drawKey: 'pkg_1a2b',
  };
}

describe('the schedule the route is drawn on', () => {
  it('paces a short route at the full per-leg pace', () => {
    const schedule = routeDrawSchedule(overlayOf(4));
    expect(schedule.legMs).toBe(ROUTE_LEG_MS);
    expect(schedule.totalMs).toBe(4 * ROUTE_LEG_MS);
  });

  it('holds a twenty-leg route to roughly the same wait as a four-leg one', () => {
    // The whole point of the budget. A route computed through a deep building
    // must not cost a commander five times the attention of a route into a
    // shopfront: the leg count moves the *pace*, never the wait.
    const short = routeDrawSchedule(overlayOf(4));
    const long = routeDrawSchedule(overlayOf(20));
    expect(long.totalMs).toBe(ROUTE_DRAW_BUDGET_MS);
    expect(long.totalMs / short.totalMs).toBeLessThan(2);
    expect(long.legMs).toBeLessThan(short.legMs);
  });

  it('never runs past the budget, whatever the plan contains', () => {
    for (let legs = 1; legs <= 60; legs += 1) {
      expect(routeDrawSchedule(overlayOf(legs)).totalMs).toBeLessThanOrEqual(
        ROUTE_DRAW_BUDGET_MS,
      );
      expect(routeDrawSchedule(overlayOf(legs, legs)).totalMs).toBeLessThanOrEqual(
        ROUTE_DRAW_BUDGET_MS,
      );
    }
  });

  it('extends one leg at a time, in the order the waypoints arrived', () => {
    const schedule = routeDrawSchedule(overlayOf(3));
    // Half a leg in: the first leg is half drawn and nothing else exists.
    expect(routeDrawState(schedule, ROUTE_LEG_MS * 0.5).entry.legs).toBeCloseTo(0.5);
    expect(routeDrawState(schedule, ROUTE_LEG_MS * 2.5).entry.legs).toBeCloseTo(2.5);
    // And it stops at the last waypoint rather than running on past it.
    expect(routeDrawState(schedule, ROUTE_LEG_MS * 9).entry.legs).toBe(3);
  });

  it('starts the egress only after the entry route is whole, and after a beat', () => {
    const schedule = routeDrawSchedule(overlayOf(3, 2));
    const entryEnds = schedule.entryLegs * schedule.legMs;
    expect(schedule.gapMs).toBe(ROUTE_EGRESS_GAP_MS);
    // The moment the entry route finishes, the second way out has not begun.
    expect(routeDrawState(schedule, entryEnds).entry.legs).toBe(3);
    expect(routeDrawState(schedule, entryEnds).egress.begun).toBe(false);
    expect(routeDrawState(schedule, entryEnds + ROUTE_EGRESS_GAP_MS).egress.begun).toBe(true);
    expect(routeDrawState(schedule, schedule.totalMs).egress.legs).toBe(2);
  });

  it('reports the sequence complete at the total and not a millisecond before', () => {
    const schedule = routeDrawSchedule(overlayOf(3, 2));
    expect(routeDrawState(schedule, schedule.totalMs - 1).complete).toBe(false);
    expect(routeDrawState(schedule, schedule.totalMs).complete).toBe(true);
  });

  it('draws the whole route at once when the reader asked for no motion', () => {
    const schedule = routeDrawSchedule(overlayOf(6, 3), { reducedMotion: true });
    // No wait at all, and every leg already there -- reduced motion is not a
    // faster animation, it is the finished picture.
    expect(schedule.totalMs).toBe(0);
    const state = routeDrawState(schedule, 0);
    expect(state.entry.legs).toBe(6);
    expect(state.egress.legs).toBe(3);
    expect(state.complete).toBe(true);
  });

  it('has nothing to draw, and no wait, when there is no route', () => {
    // A refused path reaches the model as no overlay at all. There is no
    // fallback route in the backend, so there is nothing to animate.
    const schedule = routeDrawSchedule(null);
    expect(schedule.totalMs).toBe(0);
    expect(routeDrawState(schedule, 0).complete).toBe(true);
  });
});

describe('the printed brief', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('downloads from the package’s own PDF endpoint', async () => {
    const created = vi.fn(() => 'blob:pdf');
    vi.stubGlobal('URL', { ...URL, createObjectURL: created, revokeObjectURL: vi.fn() });
    const doFetch = vi.fn(
      async (input: RequestInfo | URL) =>
        new Response(`%PDF-1.4 ${String(input)}`, {
          headers: { 'Content-Type': 'application/pdf' },
        }),
    );
    const result = await downloadEntryPackagePdf('inc-1', 'pkg_1a2b', { fetchImpl: doFetch });
    expect(result.ok).toBe(true);
    expect(String(doFetch.mock.calls[0]![0])).toBe(
      '/api/gateway/api/v1/incidents/inc-1/entry-packages/pkg_1a2b/pdf',
    );
    expect(created).toHaveBeenCalled();
  });

  it('refuses to save an error envelope under a .pdf name', async () => {
    // The gateway answers everything off its allowlist with a JSON envelope and
    // a 404. Saving that as `crew-brief-*.pdf` would put a file in a records
    // system that nothing opens and nothing explains.
    const doFetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'no such route' } }), {
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const result = await downloadEntryPackagePdf('inc-1', 'pkg_1a2b', { fetchImpl: doFetch });
    expect(result.ok).toBe(false);
    expect(result.message).toMatch(/not a PDF/);
  });
});

describe('the gateway allows every entry-package route', () => {
  const allowed = (path: string, method: 'GET' | 'POST') =>
    gatewayTargetPath(path.replace(/^\//, '').split('/'), method);

  it('admits the six the console calls, by their own methods', () => {
    const base = '/api/v1/incidents/inc-1/entry-packages';
    expect(allowed(base, 'POST')).not.toBeNull();
    expect(allowed(base, 'GET')).not.toBeNull();
    expect(allowed(`${base}/pkg_1a2b`, 'GET')).not.toBeNull();
    expect(allowed(`${base}/pkg_1a2b/approvals/entry-path`, 'POST')).not.toBeNull();
    expect(allowed(`${base}/pkg_1a2b/approvals/crew-brief`, 'POST')).not.toBeNull();
    expect(allowed(`${base}/pkg_1a2b/dispatch`, 'POST')).not.toBeNull();
    expect(allowed(`${base}/pkg_1a2b/pdf`, 'GET')).not.toBeNull();
  });

  it('is method-scoped: none of the writes is readable and no read is writable', () => {
    const base = '/api/v1/incidents/inc-1/entry-packages';
    expect(allowed(`${base}/pkg_1a2b/dispatch`, 'GET')).toBeNull();
    expect(allowed(`${base}/pkg_1a2b/approvals/entry-path`, 'GET')).toBeNull();
    expect(allowed(`${base}/pkg_1a2b/pdf`, 'POST')).toBeNull();
  });

  it('admits only the two halves the backend has', () => {
    const base = '/api/v1/incidents/inc-1/entry-packages/pkg_1a2b/approvals';
    expect(allowed(`${base}/gas-shutoff`, 'POST')).toBeNull();
    expect(allowed(`${base}/entry-path`, 'POST')).not.toBeNull();
  });
});

describe('reading packages off the incident log stream', () => {
  const frame = (sequence: number, content: Record<string, unknown>) => ({
    sequence,
    entry_type: 'ENTRY_PACKAGE',
    occurred_at: '2026-08-20T08:02:00+00:00',
    agent_versions: { 'incident-interceptor': '1.0.0' },
    content_hash: 'abc',
    content,
  });

  it('takes the whole document out of one frame', () => {
    const held = entryPackage();
    const decoded = packageFromLogEntry(frame(4, { package: held, autonomy_trigger: 'deadline' }));
    expect(decoded?.package_id).toBe('pkg_1a2b');
    expect(decoded?.status).toBe('AWAITING_APPROVAL');
  });

  it('returns null rather than half a package when the frame carries none', () => {
    // The signal the fallback reads. A half-decoded package rendered as a
    // whole one would be a console claiming a document it does not have.
    expect(packageFromLogEntry(frame(5, { package_id: 'pkg_1a2b' }))).toBeNull();
    expect(packageFromLogEntry(frame(6, { package: { package_id: 'x' } }))).toBeNull();
  });

  it('keeps the latest state of each package in composed order', () => {
    const first = entryPackage();
    const second = entryPackage({ package_id: 'pkg_9z' });
    let held = foldPackages([], first);
    held = foldPackages(held, second);
    held = foldPackages(held, withHalf(first, 'entry-path'));
    expect(held.map((p) => p.package_id)).toEqual(['pkg_1a2b', 'pkg_9z']);
    expect(held[0]!.path_approved).toBe(true);
  });
});

// ------------------------------------------- the poll under the stream --

/**
 * The second path to the package, and the reason there is one.
 *
 * `GET /log/stream` is snapshot-and-close: the backend reads the log once,
 * yields it and ends the response, so every entry appended after the connect --
 * including the `ENTRY_PACKAGE` one the loop writes about 46 s in -- reaches
 * the browser only on `EventSource`'s own reconnect. That reconnect is a
 * browser default and it is fail-permanent: a single answer on that URL which
 * is not a 2xx `text/event-stream` closes it for good with no retry, and the
 * console's own gateway answers exactly that shape for an unavailable
 * credential (503) and for anything off the allowlist (404).
 *
 * When that happened, nothing else in the console was looking. The tests below
 * are the guarantee that something is now.
 */
describe('a package the poll finds and the stream never carried', () => {
  it('scores a package by how far it has got, and only ever upward', () => {
    const fresh = entryPackage();
    const oneHalf = withHalf(fresh, 'entry-path');
    const both = withHalf(oneHalf, 'crew-brief');
    const gone = { ...both, sent_at: '2026-08-20T08:04:00+00:00', status: 'SENT' as const };
    expect(packageProgress(fresh)).toBe(0);
    expect(packageProgress(oneHalf)).toBe(1);
    expect(packageProgress(both)).toBe(2);
    expect(packageProgress(gone)).toBeGreaterThan(packageProgress(both));
    // The list row has to answer the same question off counts alone, or the
    // poll cannot decide whether a full read is even worth making.
    expect(summaryProgress(summaryOf(oneHalf))).toBe(packageProgress(oneHalf));
    expect(summaryProgress(summaryOf(gone))).toBe(packageProgress(gone));
  });

  it('refuses to fold a read that would un-sign a half an officer signed', () => {
    const fresh = entryPackage();
    const signed = withHalf(fresh, 'entry-path');
    const held = foldPolled([], signed);
    // The poll was in flight when the officer tapped. It comes back holding the
    // state from before the tap, and it is discarded -- by identity, so the
    // console does not even re-render, let alone un-tick the half.
    const after = foldPolled(held, fresh);
    expect(after).toBe(held);
    expect(after[0]!.path_approved).toBe(true);
    // Forward still moves.
    expect(foldPolled(held, withHalf(signed, 'crew-brief'))[0]!.brief_approved).toBe(true);
  });

  it('reads the list, then the document, and stops asking once one is sent', async () => {
    vi.useFakeTimers();
    try {
      let staged: EntryPackageView = entryPackage();
      const listReads: string[] = [];
      const detailReads: string[] = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.endsWith('/entry-packages')) {
            listReads.push(url);
            return new Response(
              JSON.stringify({ incident_id: 'inc-1', packages: [summaryOf(staged)] }),
              { headers: { 'Content-Type': 'application/json' } },
            );
          }
          detailReads.push(url);
          return new Response(JSON.stringify(staged), {
            headers: { 'Content-Type': 'application/json' },
          });
        }),
      );

      // No frames, ever. This is the hook with the stream switched off.
      const view = renderHook(() => useEntryPackages('inc-1', []));
      expect(view.result.current.awaiting).toBeNull();
      expect(listReads).toHaveLength(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS + 10);
      });
      expect(listReads).toHaveLength(1);
      expect(detailReads).toHaveLength(1);
      expect(view.result.current.awaiting?.package_id).toBe('pkg_1a2b');
      // The document arrived whole, off the detail endpoint. The list row could
      // never have fed the card: it carries counts, not six criteria.
      expect(view.result.current.awaiting?.assessment.criteria).toHaveLength(6);

      // A tick that learns nothing costs one small read and no document.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS + 10);
      });
      expect(listReads).toHaveLength(2);
      expect(detailReads).toHaveLength(1);

      // The send. The loop this poll exists to catch is over.
      staged = {
        ...withHalf(withHalf(staged, 'entry-path'), 'crew-brief'),
        status: 'SENT',
        sent_at: '2026-08-20T08:04:00+00:00',
      };
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS + 10);
      });
      expect(listReads).toHaveLength(3);
      expect(detailReads).toHaveLength(2);

      const readsAtSend = listReads.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS * 4);
      });
      expect(listReads).toHaveLength(readsAtSend);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('does not poll without an incident, and stops when one closes', async () => {
    vi.useFakeTimers();
    try {
      const reads: string[] = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          reads.push(String(input));
          return new Response(JSON.stringify({ incident_id: 'inc-1', packages: [] }), {
            headers: { 'Content-Type': 'application/json' },
          });
        }),
      );

      const view = renderHook(({ id }: { id: string | null }) => useEntryPackages(id, []), {
        initialProps: { id: null as string | null },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS * 3);
      });
      // Standby asks the backend nothing about packages. There is no incident
      // for one to belong to.
      expect(reads).toHaveLength(0);

      view.rerender({ id: 'inc-1' });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS + 10);
      });
      expect(reads).toHaveLength(1);

      // The incident closes.
      view.rerender({ id: null });
      const readsAtClose = reads.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS * 3);
      });
      expect(reads).toHaveLength(readsAtClose);

      // And nothing is left running after the console goes away.
      view.rerender({ id: 'inc-1' });
      view.unmount();
      const readsAtUnmount = reads.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS * 3);
      });
      expect(reads).toHaveLength(readsAtUnmount);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('says nothing when the endpoint refuses, rather than clearing what is held', async () => {
    vi.useFakeTimers();
    try {
      let refuse = false;
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          if (refuse) {
            return new Response(
              JSON.stringify({
                error: {
                  code: 'BACKEND_UNREACHABLE',
                  message: 'no',
                  details: {},
                  request_id: null,
                  correlation_id: null,
                },
              }),
              { status: 503, headers: { 'Content-Type': 'application/json' } },
            );
          }
          const url = String(input);
          const held = entryPackage();
          return new Response(
            JSON.stringify(
              url.endsWith('/entry-packages')
                ? { incident_id: 'inc-1', packages: [summaryOf(held)] }
                : held,
            ),
            { headers: { 'Content-Type': 'application/json' } },
          );
        }),
      );

      const view = renderHook(() => useEntryPackages('inc-1', []));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS + 10);
      });
      expect(view.result.current.awaiting?.package_id).toBe('pkg_1a2b');

      refuse = true;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ENTRY_PACKAGE_POLL_MS * 2);
      });
      // A list that failed is not a list that came back empty. The package the
      // console already has is still a package the backend returned.
      expect(view.result.current.packages).toHaveLength(1);
      expect(view.result.current.awaiting?.package_id).toBe('pkg_1a2b');
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});

// ------------------------------------------------- the console end to end --

/** A stand-in for EventSource that lets a test push log frames. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners: Record<string, ((event: unknown) => void)[]> = {};
  onerror: ((event: unknown) => void) | null = null;
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

  emit(type: string, payload: unknown) {
    for (const handler of this.listeners[type] ?? []) {
      handler({ data: JSON.stringify(payload) });
    }
  }
}

function logStream(): FakeEventSource {
  const found = FakeEventSource.instances.find((source) => source.url.includes('/log/stream'));
  if (!found) throw new Error('the console never opened the incident log stream');
  return found;
}

describe('the console, from the composed package to standby', () => {
  const SENT = {
    ...withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief'),
    status: 'SENT' as const,
    sent_at: '2026-08-20T08:04:00+00:00',
    sent_by: 'bc-09',
    dispatch_decision_id: 'dec_77',
  };

  function stubFetch(dispatchFails = false) {
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.endsWith('/approvals/entry-path')) {
        return body(withHalf(entryPackage(), 'entry-path'));
      }
      if (url.endsWith('/approvals/crew-brief')) {
        return body(withHalf(withHalf(entryPackage(), 'entry-path'), 'crew-brief'));
      }
      if (url.endsWith('/dispatch')) {
        return dispatchFails
          ? body(
              {
                error: {
                  code: 'UNPROCESSABLE',
                  message: 'both halves must be approved',
                  details: {},
                  request_id: null,
                  correlation_id: null,
                },
              },
              422,
            )
          : body(SENT);
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
      if (url.includes('/readyz')) {
        return body({ status: 'ready', ready: true, mode: 'fake', checks: [] });
      }
      if (url.includes('/districts/') && url.includes('/stats')) return body(STATS);
      if (url.includes('/districts/') && url.includes('/queue')) return body(QUEUE);
      if (url.includes('/fire-activity')) return body({ available: false, detections: [] });
      if (url.includes('/audit/events')) return body(EVENTS);
      if (url.includes('/audit/decisions')) return body(DECISIONS);
      if (url.includes('/registry/agents')) return body({ agents: AGENTS, count: AGENTS.length });
      if (url.includes('/registry/subscriptions')) {
        return body({ subscriptions: SUBSCRIPTIONS, count: SUBSCRIPTIONS.length });
      }
      if (url.includes('/imagery')) return body({ available: false, unavailable_reason: 'off' });
      if (url.includes('/timeline')) return body(TIMELINE);
      if (url.includes('/geometry')) return body(GEOMETRY);
      if (url.includes(`/buildings/${ADDRESS}`)) return body(PROFILE);
      if (url.endsWith('/incidents') && init?.method === 'POST') return body(INCIDENT, 201);
      return body({});
    });
  }

  async function openIncident() {
    const view = render(
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
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: `Open ${ADDRESS}, records disagree` }));
    await waitFor(() => expect(screen.getByText(/profile v16/)).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('dispatch-button'));
    await waitFor(() => expect(screen.getByLabelText('Active incident')).toBeInTheDocument());
    return view;
  }

  /** Push one ENTRY_PACKAGE frame, the way the interceptor writes one. */
  async function composePackage(held: EntryPackageView, trigger = 'deadline') {
    await act(async () => {
      logStream().emit('entry', {
        sequence: 7,
        entry_type: 'ENTRY_PACKAGE',
        occurred_at: '2026-08-20T08:02:00+00:00',
        agent_versions: { 'incident-interceptor': '1.0.0' },
        content_hash: 'abc',
        content: {
          package: held,
          package_id: held.package_id,
          status: held.status,
          autonomy_trigger: trigger,
        },
      });
    });
  }

  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('fetch', stubFetch());
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    FakeEventSource.instances = [];
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('draws no route until the interceptor has composed a package', async () => {
    await openIncident();
    // An incident is open, the structure is on screen, and there is no route:
    // the drawing is the outcome of a decision the fleet has not made yet.
    expect(screen.queryByTestId('route-caption')).not.toBeInTheDocument();
    expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
    expect(screen.getByText(/No entry package yet/i)).toBeInTheDocument();
  });

  it('raises the card and the route off one ENTRY_PACKAGE frame', async () => {
    await openIncident();
    await composePackage(entryPackage());
    expect(await screen.findByTestId('entry-package-modal')).toBeInTheDocument();
    expect(document.getElementById('entry-package-title')).toHaveTextContent(
      'incident-interceptor',
    );
    expect(screen.getByTestId('route-caption')).toBeInTheDocument();
    expect(screen.getByTestId(`entry-package-row-${entryPackage().package_id}`)).toBeInTheDocument();
  });

  /** What the console waits for, computed the way the console computes it. */
  const drawMsFor = (held: EntryPackageView) =>
    routeDrawSchedule({
      entry: held.path.entry,
      egress: held.path.egress,
      highlight: null,
      drawKey: held.package_id,
    }).totalMs;

  /** Answer `prefers-reduced-motion` the way a reader who set it would. */
  const answerReducedMotion = (reduce: boolean) =>
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: reduce && query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }));

  it('holds the approval card back while the route is still drawing', async () => {
    await openIncident();
    await composePackage(entryPackage());
    // The route is on screen and the ask is not. This is the whole sequence:
    // an officer sees where the crew is being sent before being asked to sign
    // for it, and the card does not land on top of the drawing it explains.
    expect(screen.getByTestId('route-caption')).toBeInTheDocument();
    expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
    expect(drawMsFor(entryPackage())).toBeGreaterThan(0);
  });

  it('raises the approval card once the route has finished drawing', async () => {
    await openIncident();
    await composePackage(entryPackage());
    expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
    // Bounded: the wait is the schedule's own total, not an open-ended one.
    expect(
      await screen.findByTestId('entry-package-modal', undefined, {
        timeout: ROUTE_DRAW_BUDGET_MS * 2,
      }),
    ).toBeInTheDocument();
  });

  it('raises the card at once for a refused path, and draws no route', async () => {
    await openIncident();
    await composePackage(
      entryPackage({
        path: path({
          refused: true,
          refusal_reason:
            'no pre-incident geometry: the slow loop never measured this address',
          refusal_refs: [ADDRESS],
          entry: null,
          egress: null,
        }),
      }),
    );
    // There is nothing to watch, so there is nothing to wait through: the
    // refusal is the finding and it goes to a human immediately.
    expect(screen.getByTestId('entry-package-modal')).toBeInTheDocument();
    expect(screen.getByTestId('path-refused')).toHaveTextContent(/never measured this address/);
    expect(screen.queryByTestId('route-caption')).not.toBeInTheDocument();
  });

  it('draws instantly and raises the card at once under prefers-reduced-motion', async () => {
    answerReducedMotion(true);
    await openIncident();
    await composePackage(entryPackage());
    // No wait, on a package that would otherwise have taken two legs to draw.
    expect(screen.getByTestId('entry-package-modal')).toBeInTheDocument();
    expect(screen.getByTestId('route-caption')).toBeInTheDocument();
  });

  it('clears the draw timer when the console tears down mid-draw', async () => {
    const drawMs = drawMsFor(entryPackage());
    expect(drawMs).toBeGreaterThan(0);
    const scheduled = new Set<unknown>();
    const cleared = new Set<unknown>();
    const realSetTimeout = globalThis.setTimeout;
    const realClearTimeout = globalThis.clearTimeout;
    vi.stubGlobal('setTimeout', ((handler: () => void, ms?: number, ...rest: unknown[]) => {
      const id = (realSetTimeout as (...args: unknown[]) => unknown)(handler, ms, ...rest);
      if (ms === drawMs) scheduled.add(id);
      return id;
    }) as unknown as typeof setTimeout);
    vi.stubGlobal('clearTimeout', ((id?: unknown) => {
      cleared.add(id);
      (realClearTimeout as (...args: unknown[]) => void)(id);
    }) as unknown as typeof clearTimeout);

    const view = await openIncident();
    await composePackage(entryPackage());
    expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
    expect(scheduled.size).toBe(1);

    view.unmount();
    // Nothing is left to fire into a console that is gone: the card would be
    // raised for a fire somebody has already walked away from, and the state
    // behind it written after unmount.
    for (const id of scheduled) expect(cleared.has(id)).toBe(true);
  });

  it('returns to standby once the send comes back, not before', async () => {
    await openIncident();
    await composePackage(entryPackage());
    await screen.findByTestId('entry-package-modal');

    fireEvent.click(screen.getByTestId('approve-both'));
    await screen.findByTestId('approve-crew-brief-granted');
    await waitFor(() => expect(screen.getByTestId('entry-package-release')).toBeEnabled());

    fireEvent.click(screen.getByTestId('entry-package-release'));
    // The sheet says what happened; it is not a claim made ahead of the answer.
    expect(await screen.findByTestId('resolve-sheet')).toHaveTextContent(
      /released to live dispatch units/i,
    );
    await waitFor(
      () => expect(screen.queryByLabelText('Active incident')).not.toBeInTheDocument(),
      { timeout: 4000 },
    );
    // The sheet lifts on its own, after its floor and after the close returned.
    await waitFor(() => expect(screen.queryByTestId('resolve-sheet')).not.toBeInTheDocument(), {
      timeout: 4000,
    });
    // Standby is back: the dispatch panel is on screen again.
    expect(screen.getByTestId('dispatch-button')).toBeInTheDocument();
  });

  it('stays on the fireground when the send is refused', async () => {
    vi.stubGlobal('fetch', stubFetch(true));
    await openIncident();
    await composePackage(entryPackage());
    await screen.findByTestId('entry-package-modal');
    fireEvent.click(screen.getByTestId('approve-both'));
    await screen.findByTestId('approve-crew-brief-granted');
    await waitFor(() => expect(screen.getByTestId('entry-package-release')).toBeEnabled());

    fireEvent.click(screen.getByTestId('entry-package-release'));
    expect(await screen.findByRole('alert')).toHaveTextContent(/nothing was sent/i);
    // Nothing resolved. A refused send is not a quieter version of a send.
    expect(screen.queryByTestId('resolve-sheet')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Active incident')).toBeInTheDocument();
  });

  // ------------------------------------------- the stream delivers nothing --

  /**
   * The user's failure, driven end to end.
   *
   * A console on a fireground for 2 min 24 s that never showed the route or the
   * brief, while the backend had a package staged and readable at
   * `GET /incidents/{id}/entry-packages` the whole time. The cause is not that
   * the loop failed to compose; it is that the console's only way of finding
   * out was a log stream that had gone quiet -- snapshot-and-close, so
   * everything after the first frame depends on a reconnect that a single
   * non-SSE answer kills permanently.
   *
   * These run against exactly that: the stream is open, and it never carries an
   * `ENTRY_PACKAGE` frame. Nothing here emits one. Real timers, so the wait the
   * officer actually stands through is the wait being asserted.
   */
  describe('when no ENTRY_PACKAGE frame ever arrives', () => {
    /** Wraps the console stub so only the packages endpoints change. */
    function stubFetchWithStagedPackage(staged: () => EntryPackageView | null) {
      const console = stubFetch();
      const reads = { list: 0, detail: 0 };
      const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const answer = (value: unknown) =>
          new Response(JSON.stringify(value), {
            headers: { 'Content-Type': 'application/json' },
          });
        const held = staged();
        if (init?.method !== 'POST' && url.includes('/entry-packages')) {
          if (url.endsWith('/entry-packages')) {
            reads.list += 1;
            return answer({
              incident_id: 'inc-1',
              packages: held ? [summaryOf(held)] : [],
            });
          }
          if (held && url.endsWith(`/entry-packages/${held.package_id}`)) {
            reads.detail += 1;
            return answer(held);
          }
        }
        return console(input, init);
      });
      return { impl, reads };
    }

    /** Long enough for a tick, a document read and the whole route draw. */
    const CARD_BUDGET_MS = ENTRY_PACKAGE_POLL_MS + ROUTE_DRAW_BUDGET_MS + 4000;

    it(
      'raises the approval card off the packages endpoint alone',
      async () => {
        let staged: EntryPackageView | null = null;
        const { impl } = stubFetchWithStagedPackage(() => staged);
        vi.stubGlobal('fetch', impl);

        await openIncident();
        // The stream is open and it is silent, which is what the console saw
        // for 2 min 24 s. Nothing has been staged yet either.
        expect(logStream().closed).toBe(false);
        expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();

        // The loop composes. On the real backend this is ~46 s in; the only
        // thing that changes for the console is that the endpoint now has one.
        staged = entryPackage();

        expect(
          await screen.findByTestId('entry-package-modal', undefined, {
            timeout: CARD_BUDGET_MS,
          }),
        ).toBeInTheDocument();
        // The whole document, not the summary: the card states six criteria and
        // the list endpoint has never carried them.
        expect(screen.getByTestId('readiness-banner')).toHaveAttribute('data-ready', 'false');
        expect(screen.getByText(/CHARLIE is UNSCANNED and lapsed/)).toBeInTheDocument();
        // No frame said how the loop decided, so nothing claims it did. The
        // trigger is a property of the log entry and the poll never saw one.
        expect(screen.getByText(/is holding it for a human decision/i)).toBeInTheDocument();
        expect(screen.queryByText(/compose deadline ran out/i)).not.toBeInTheDocument();
      },
      CARD_BUDGET_MS + 10000,
    );

    it(
      'draws the route over the model for a package that only the poll found',
      async () => {
        let staged: EntryPackageView | null = null;
        const { impl } = stubFetchWithStagedPackage(() => staged);
        vi.stubGlobal('fetch', impl);

        await openIncident();
        expect(screen.queryByTestId('route-caption')).not.toBeInTheDocument();
        staged = entryPackage();

        // The overlay is gated on a package existing, and it does not care
        // which path the package came down. The route reaches the model and
        // the draw runs on its own schedule, exactly as off a frame.
        await waitFor(() => expect(screen.getByTestId('route-caption')).toBeInTheDocument(), {
          timeout: CARD_BUDGET_MS,
        });
        // The route is up and the ask is not, which is the sequence the whole
        // gate exists for: an officer sees where the crew is being sent before
        // being asked to sign for it. `routeDrawSchedule` is read off the
        // awaiting package, and the poll delivered the same document a frame
        // would have, so the wait is the same wait.
        expect(drawMsFor(entryPackage())).toBeGreaterThan(0);
        expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
        // And the card still waits for the draw rather than landing on top of
        // the drawing it explains -- the same gate, off the same schedule.
        expect(
          await screen.findByTestId('entry-package-modal', undefined, {
            timeout: CARD_BUDGET_MS,
          }),
        ).toBeInTheDocument();
        expect(screen.getByTestId(`entry-package-row-${entryPackage().package_id}`)).toBeInTheDocument();
      },
      CARD_BUDGET_MS + 10000,
    );

    it(
      'raises it once, and a dismissal stays a dismissal across later ticks',
      async () => {
        const staged = entryPackage();
        const { impl } = stubFetchWithStagedPackage(() => staged);
        vi.stubGlobal('fetch', impl);

        await openIncident();
        await screen.findByTestId('entry-package-modal', undefined, { timeout: CARD_BUDGET_MS });

        fireEvent.keyDown(document, { key: 'Escape' });
        await waitFor(() =>
          expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument(),
        );

        // Two more ticks. The endpoint still holds the package -- it is still
        // awaiting approval, because dismissing is not deciding -- and the card
        // does not come back. A modal an officer cannot get out of is the one
        // thing worse than a modal that never arrives.
        await act(async () => {
          await new Promise((done) => setTimeout(done, ENTRY_PACKAGE_POLL_MS * 2 + 500));
        });
        expect(screen.queryByTestId('entry-package-modal')).not.toBeInTheDocument();
        // It is still reachable, which is the point of the list beside the map.
        expect(
          screen.getByTestId(`entry-package-row-${staged.package_id}`),
        ).toBeInTheDocument();
      },
      CARD_BUDGET_MS + 15000,
    );

    it(
      'never un-signs a half under the officer who just signed it',
      async () => {
        // The endpoint keeps answering with the state from before the tap,
        // which is what a read lag looks like from the console.
        const stale = entryPackage();
        const { impl, reads } = stubFetchWithStagedPackage(() => stale);
        vi.stubGlobal('fetch', impl);

        await openIncident();
        await screen.findByTestId('entry-package-modal', undefined, { timeout: CARD_BUDGET_MS });

        fireEvent.click(screen.getByTestId('approve-entry-path'));
        await screen.findByTestId('approve-entry-path-granted');
        const detailReadsAtTap = reads.detail;

        await act(async () => {
          await new Promise((done) => setTimeout(done, ENTRY_PACKAGE_POLL_MS * 2 + 500));
        });
        // The half is still granted and the outstanding line still names only
        // the other one. The poll saw a summary no further along than what the
        // console holds and did not even ask for the document.
        expect(screen.getByTestId('approve-entry-path-granted')).toBeInTheDocument();
        expect(screen.getByTestId('outstanding-line')).toHaveTextContent(/crew-brief/);
        expect(reads.detail).toBe(detailReadsAtTap);
      },
      CARD_BUDGET_MS + 15000,
    );
  });
});
