'use client';

/**
 * The command center. One screen, two modes, no navigation between them.
 *
 * A dispatch does not take an officer somewhere else -- the standby view
 * reorganises and the incident surfaces expand in place. That is deliberate:
 * losing the district context at the moment a fire starts is exactly when
 * losing it costs the most, and a page transition is a moment where a tablet on
 * a bad connection can show nothing at all.
 *
 * The shape, and the screen never scrolls as a whole:
 *
 * - **The district bar, full width, directly under the header.** True in both
 *   modes, so it is above the mode switch rather than inside it. Read as an
 *   instrument panel: a number, a meter, a two-word label.
 * - **Two columns, the same two in both modes.** The fleet down the left, wide
 *   enough that an agent is a thing you press rather than a line of six-point
 *   type, and everything that fleet found to its right. Standby used to be a
 *   fleet grid across the whole width, which meant the screen changed shape
 *   entirely at dispatch; an officer re-learning the layout at the moment a
 *   fire starts is the cost that bought.
 * - **The flanks are cards; the middle is not.** Both rails sit on `surface`,
 *   the tone the header and footer already use, inside a rounded hairline
 *   border with a little air around it. The middle keeps `ground` and is not
 *   outlined at all, so what an officer is looking at reads as the surface of
 *   the screen and the two rails read as instruments placed on it. One step on
 *   a three-tone scale that already existed, no new colour.
 * - **The flanks are sized to be read; the middle is sized to be looked at.**
 *   Both rails scale with the viewport between a floor and a cap -- floored so
 *   a laptop still fits a full agent id and a brief section without wrapping
 *   them to death, capped so an ultrawide does not turn either into a page.
 *   The two incident flanks are the same track, deliberately: the fleet is one
 *   of two things beside the building there, not the only one, and two equal
 *   rails read as instruments either side of a subject rather than as a
 *   hierarchy nobody meant. Standby's rail is the wider one, because standby
 *   has no second flank to share the width with.
 *
 *   Between them the middle takes better than half the screen from 1280 up --
 *   51% at 1280, 57% at 1440, 60% at 1920, and three quarters of standby.
 *   Below that the flanks are on their floors and the middle gives way, which
 *   is the right trade: a rail too narrow to read is not buying the building
 *   anything. That is the point of the sizing. The fleet and the brief are
 *   read, the building is looked at, and the one being looked at should be the
 *   biggest thing on the display.
 *
 *   They were wider. 320 fixed pixels was too narrow -- the fleet whispered
 *   down one edge and the pane about the selected agent had no room to say
 *   anything -- and the correction overshot: at 32vw the two rails together
 *   took as much of a 1920 display as the building did.
 * - **Standby: the region to the right of the fleet.** Satellite fire activity
 *   at the top, the structures whose records disagree under it, and a selected
 *   structure under that. There is no survey ranking on this screen: the
 *   backend still scores the district, but a rank an officer cannot act on
 *   differently from the one below it was taking the middle of the display to
 *   say so.
 * - **Incident: three columns.** The fleet on the left, the building in the
 *   middle -- the computed structure beside the photograph of it -- and the
 *   brief down the right. The brief used to run the full width under the
 *   model, so a three-stage brief filling in pushed the building it described
 *   off the top of the screen. In a column of its own it grows downwards past
 *   nothing. The slow loop leaves the screen and says so in a line of its own
 *   -- it did not stop because a fire started.
 *
 * Which agent belongs to which *loop* is `FleetPanel`'s decision, made from the
 * `loop` prop each column passes. The layout never re-answers that. Standby is
 * the one place it also has to decide *which half* of a single loop a column
 * gets, because both standby columns are the same loop and the panel has no
 * way to tell them apart: that split is done here, once, in catalog order.
 *
 * Everything on screen comes from the backend. Where the backend reports
 * nothing, the console says so rather than inventing a row.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import {
  StructureModel,
  prefersReducedMotion,
  routeDrawSchedule,
  type RouteOverlay,
  type ViewAngle,
} from '@/components/StructureModel';
import { PhotorealisticModel, type GeometryState } from '@/components/PhotorealisticModel';
import { AgentActivity } from '@/components/incident/AgentActivity';
import { BriefPanel, announcementFor } from '@/components/incident/BriefPanel';
import { BuildingImagery, type ImageryView } from '@/components/incident/BuildingImagery';
import { CallAudio } from '@/components/incident/CallAudio';
import { EntryPackageList } from '@/components/incident/EntryPackageList';
import { EntryPackageModal } from '@/components/incident/EntryPackageModal';
import { EntryPackageWatch } from '@/components/incident/EntryPackageWatch';
import type { LegSelection } from '@/components/incident/EntryPackageParts';
import { IncomingCall } from '@/components/incident/IncomingCall';
import { IntakePanel } from '@/components/incident/IntakePanel';
import { IncidentBanner } from '@/components/incident/IncidentBanner';
import { ResourcePanel } from '@/components/incident/ResourcePanel';
import { ThermalPanel } from '@/components/incident/ThermalPanel';
import { AttributeGrid } from '@/components/profile/AttributeGrid';
import { ConflictPanel, type ResolutionSubmission } from '@/components/profile/ConflictPanel';
import { Timeline } from '@/components/profile/Timeline';
import { sessionFloor } from '@/components/fleet/derive';
import { AgentRail } from '@/components/standby/AgentRail';
import { RecordsDisagree } from '@/components/standby/RecordsDisagree';
import { DispatchPanel, SAMPLE_CALLS } from '@/components/standby/DispatchPanel';
import { DistrictStrip } from '@/components/standby/DistrictStrip';
import {
  FireActivityMap,
  normalizeFireActivity,
  type FireActivity,
} from '@/components/standby/FireActivityMap';
import { PanelCard } from '@/components/standby/PanelCard';
import { RegionalHeatMap } from '@/components/standby/RegionalHeatMap';
import { browserGet, browserPost } from '@/lib/api/client';
import { useEntryPackages } from '@/lib/api/entry-packages';
import { useBriefStream, useIncidentLogStream, useNarrativeStream } from '@/lib/api/stream';
import type {
  AgentDescriptorView,
  AgentListResponse,
  AuditEventView,
  BriefEmissionView,
  BuildingProfileView,
  CloseIncidentResponse,
  DistrictStatsView,
  EntryPackageView,
  GeometryView,
  IntakeChannel,
  IntakeResponse,
  OpenIncidentResponse,
  PolicyDecisionView,
  QueueView,
  ReferralSummary,
  Readiness,
  RegionBasemapView,
  ResolutionResponse,
  ResourceOutcomeView,
  SubscriptionListResponse,
  SubscriptionView,
  SystemStatus,
  TimelineEventView,
} from '@/lib/api/types';

const VIEWS: ViewAngle[] = ['ISO', 'ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'];

/** How often the one remaining piece of status chrome re-checks the backend. */
const READINESS_POLL_MS = 5000;

/**
 * How long the resolve sheet stays up while an incident hands back to standby.
 *
 * A floor, not a delay: the close call behind it usually returns faster than
 * this, and a transition that flickered for 40ms would read as a glitch rather
 * than as a conclusion. Deliberately well under a second -- this is punctuation
 * between two screens, and an officer who has just released a package to a crew
 * is not waiting on an animation to find out what happened. Reduced motion
 * flattens the sheet's animation to nothing; the sheet and its words remain.
 */
const RESOLVE_TRANSITION_MS = 420;

/**
 * How often standby re-reads the district while nothing is burning.
 *
 * Without this the console was static: the district counts, the ranked
 * structures and every agent's reasoning box were whatever they had been when
 * the page loaded, and a screen that has stopped updating looks exactly like a
 * district where nothing is happening. Seven seconds is slower than the slow
 * loop produces work and fast enough that a card visibly moves.
 *
 * It runs in standby only. During an incident the brief arrives on an SSE
 * stream and this loop is torn down: a second fetch loop against the same
 * backend would compete with the stream for a tablet's connection while
 * telling the officer nothing the stream is not already saying.
 */
const STANDBY_POLL_MS = 7000;

/**
 * How often the fleet's audit evidence is re-read.
 *
 * Faster than the standby tick because it is two small reads and it is the only
 * thing that moves an agent from idle to active on screen. During an incident
 * the fleet writes several events a second, and a seven-second gap made a burst
 * of work look like one event.
 */
const FLEET_POLL_MS = 2500;

/**
 * How stale regional fire activity is allowed to get before the standby poll
 * re-reads it.
 *
 * There is no second timer: the fire-activity read rides the seven-second
 * standby poll and is skipped on the passes where the last one is still fresh.
 * A satellite overpass is hours apart and the endpoint is backed by an external
 * archive, so asking every seven seconds would be a request-per-tablet against
 * someone else's quota to redraw a map that has not moved. Two minutes keeps
 * the panel honest about the day without pretending to a cadence the data has.
 *
 * Running a slow-loop pass by hand bypasses this: an operator who asked for a
 * pass is asking to see the current state, not a two-minute-old one.
 */
const FIRE_ACTIVITY_MAX_AGE_MS = 120000;

/**
 * The demo choreography. Three constants, together at the top so a recording
 * can be paced without reading the component.
 *
 * `AUTO_PASS_MS` runs a real slow-loop pass on an interval, so standby is a
 * platform doing background work rather than a list of statuses. Everything
 * that moves on screen moved because a pass wrote something.
 *
 * `AUTO_CALL_MS` is how long standby runs before a call arrives, and
 * `CALL_WARNING_MS` is how much of that is spent visibly counting down, so a
 * viewer sees the transition begin instead of the screen snapping.
 */
const AUTO_PASS_MS = 25000;

/**
 * How long after load the choreography runs its first slow-loop pass.
 *
 * See the lead-in in the choreography effect for why this is not zero and not
 * a full `AUTO_PASS_MS`.
 */
const FIRST_PASS_MS = 3000;
/**
 * The same passes, slower, while an incident is open.
 *
 * The slow loop does not stop when a fire starts -- that is stated in the fleet
 * component and in the architecture notes, and it is the reason the right-hand
 * column used to carry it. It is off screen during an incident now, so the only
 * thing that makes "still running" true is that it is still running.
 */
const AUTO_PASS_INCIDENT_MS = 60000;
const AUTO_CALL_MS = 50000;
const CALL_WARNING_MS = 6000;

/**
 * How often to look again when the call is due and the queue is not loaded.
 *
 * The call needs an address to dispatch against, and against a live backend the
 * district read can still be in flight when the countdown ends. Two seconds is
 * short enough that the call lands close to its appointed time and long enough
 * not to spin.
 */
const CALL_RETRY_MS = 2000;

/**
 * How long a standby read may take before the console gives up on it.
 *
 * The client default is four seconds, which is right for a console talking to
 * an idle backend and wrong for one talking to a backend part-way through a
 * live district pass. Twenty seconds is still a bound -- a read that has not
 * answered by then is a problem worth surfacing -- and it is past the point
 * where a busy Firestore stops being one.
 */
const STANDBY_READ_TIMEOUT_MS = 20_000;

/**
 * How many audit events and policy decisions the fleet panel reads.
 *
 * The panel derives every agent's state and its whole reasoning terminal from
 * this window, so an agent whose events fall outside it reads as idle no matter
 * what it did. At 60 that happened constantly: `records-watcher` writes a dozen
 * or more events per pass and `geometry-watcher` and `hazard-watcher` write one
 * each, so within two passes the quiet agents were pushed out entirely and the
 * console showed two working agents and three dead ones.
 *
 * 300 holds several passes of a busy district. It is a window, not a fix for
 * the shape of the metric -- an agent's own run records would be the right
 * source -- but it is the difference between a panel that is wrong every time
 * and one that is right.
 */
const AUDIT_WINDOW = 300;

/**
 * How many audit events and decisions the console keeps across polls.
 *
 * The window above is what one read returns; this is what is remembered from
 * all of them. Ten times the window, so a quiet agent's evidence survives a
 * busy stretch by an order of magnitude, and bounded so a long session cannot
 * grow this array without limit.
 */
const FLEET_MEMORY = 3000;

/**
 * Merge a freshly read page into what is already held, newest first.
 *
 * Deduplicated by the record's own id, so a poll that overlaps the last one --
 * which every poll does -- adds only what is new. The order the backend
 * returned is preserved: it sorts newest first and this keeps that, rather than
 * re-sorting on a timestamp string the console would have to parse.
 */
function mergeById<T>(current: T[], incoming: T[], idOf: (item: T) => string): T[] {
  // A body that is not a list is not an empty list.
  //
  // These are typed as arrays and the backend answers with arrays, but the
  // console reads them over a gateway and through a proxy, and anything in that
  // chain can answer with an object on a bad day. Writing one into this state
  // used to crash the fleet panel on `.filter`; keeping what is already held is
  // the honest response to a page that could not be read.
  if (!Array.isArray(incoming)) return current;
  const seen = new Set<string>();
  const merged: T[] = [];
  for (const item of [...incoming, ...current]) {
    const id = idOf(item);
    if (seen.has(id)) continue;
    seen.add(id);
    merged.push(item);
  }
  return merged.length > FLEET_MEMORY ? merged.slice(0, FLEET_MEMORY) : merged;
}

/** How long between walls on the drone sweep.
 *
 * Short, because it no longer has to create the separation. This was 3.5s so
 * that an officer saw each face arrive as its own event rather than four at
 * once -- but the activity stream now gives every face its own message with its
 * own timestamp, so the separation is in the record rather than in the pacing,
 * and the delay was just fourteen seconds of an incident's first minute spent
 * waiting. Against real services the vision call spaces the walls on its own.
 *
 * Not zero: the sweep is sequential by construction -- each request flies the
 * next unflown wall -- and a small gap keeps a fast backend from arriving as a
 * single indistinguishable burst. The backend decides when the sweep is
 * finished; this only decides how fast it is asked. */
const SWEEP_INTERVAL_MS = 600;

/**
 * How long to wait for one wall of the sweep.
 *
 * The longest single call in the incident loop: the backend points a camera,
 * renders the face, and puts it through a vision model. On the client's old
 * 4-second default that abort landed as `Drone sweep stopped: signal is aborted
 * without reason` after the first or second wall, which is why `sensor-fusion`
 * read as an agent that did one thing and stopped.
 */
const SWEEP_TIMEOUT_MS = 90_000;

/** A hard ceiling on sweep requests, so a backend that never reports `complete`
 *  cannot spin this loop for ever. Four walls, one spare. */
const SWEEP_FACES = 5;

/**
 * The notifications the demo asks for on dispatch, in order.
 *
 * **Choreography, and it is a simulated operator rather than a claim about the
 * agent.** Each of these is a kind `agency-notifier` may send autonomously --
 * informing an agency that stays free to act or not -- and each is a button an
 * officer can press in this console. The demo presses a few of them so the
 * fleet is visibly doing its work inside the first ninety seconds, which is
 * what an unattended screen cannot otherwise show.
 *
 * It does not decide *whether* a notification is warranted; the agent does that
 * when it composes each one, and refuses the ones it cannot support. Anything
 * needing a chief -- a gas shutoff, a road closure -- is deliberately absent:
 * a demo must never press an approval gate on nobody's authority.
 *
 * All seven autonomous kinds, not four. The catalog has seven and the console
 * pressed four of them, so three agencies a real fire would inform -- the
 * exposure address next door, the building department, the utility -- were
 * never told anything, and the notifier looked like an agent with four
 * behaviours. The other five kinds in the catalog are approval-gated and stay
 * out, on the rule above.
 */
const DEMO_NOTIFICATIONS = [
  'water-supply',
  'mutual-aid',
  'county-oem',
  'public-works',
  'exposure',
  'building-department',
  'utility-conditions',
] as const;

/**
 * The writes this console makes, one member per action that holds the screen.
 *
 * These all used to be a single `busy` boolean, and the one control that put
 * that flag into words said "Closing…" no matter which of them had set it --
 * so asking `agency-notifier` to tell the water department made the top of a
 * live incident announce that the incident was being closed. A union rather
 * than a loose string because `IN_FLIGHT_LABEL` below is keyed by it: an
 * action added later cannot reach the screen without being given a verb of its
 * own, which is exactly the check that was missing when they all shared one.
 */
type InFlightAction =
  | 'dispatch'
  | 'draft-referral'
  | 'file-referral'
  | 'resolve'
  | 'notify'
  | 'approve'
  | 'thermal'
  | 'close';

/**
 * What each write is doing while it is still doing it.
 *
 * Present tense and nothing further: every one of these describes an open
 * request, so none of them may claim the thing came back. "Notifying…" while
 * the notifier is talking to the agency; whether the agency was reached, and
 * under what reference, stays with the outcome cards in `ResourcePanel`, which
 * are written from what the backend actually returned.
 */
const IN_FLIGHT_LABEL: Record<InFlightAction, string> = {
  dispatch: 'Dispatching…',
  'draft-referral': 'Drafting…',
  'file-referral': 'Filing…',
  resolve: 'Resolving…',
  notify: 'Notifying…',
  approve: 'Approving…',
  thermal: 'Recording…',
  close: 'Closing…',
};

/**
 * How long to let a slow-loop pass run before giving up on it.
 *
 * Ten minutes. Generous because the loop is serial end to end -- records
 * extraction, geometry, the hazard graph, then a profile materialised per
 * address -- and a live district measured 318 seconds. This is a ceiling on a
 * job that is genuinely long, not a target: it exists so an honest wait does
 * not read as a failure.
 */
const SLOW_LOOP_TIMEOUT_MS = 600_000;

/**
 * How long the console waits for an incident to open.
 *
 * The open itself is quick by construction -- the instant brief is budgeted at
 * `instant_brief_budget_ms` and is model-free -- but it is still a Firestore
 * write, a grant mint and a Pub/Sub publish against real services. The client's
 * 4-second default aborted it, and because the abort arrives as
 * `signal is aborted without reason`, the console reported a failure for an
 * incident the backend had already opened.
 */
const OPEN_INCIDENT_TIMEOUT_MS = 30_000;

/**
 * How long the console waits for one agency notification.
 *
 * Each is a policy decision, a write action and a delivery through the gateway.
 * Seven of them now go out together, so the slowest sets the wall clock rather
 * than the sum -- but each still needs more than the client's 4-second default
 * against real services.
 */
const NOTIFY_TIMEOUT_MS = 30_000;

/**
 * How long the console waits for a narrative to be read.
 *
 * This one is a model call: Model Armor screens the transcript and Gemini reads
 * it into a closed key set with every value bound to a span. Against Vertex that
 * is seconds, not milliseconds, and it is the reason this no longer rides along
 * with the open -- see `dispatch`.
 */
const INTAKE_TIMEOUT_MS = 60_000;

/**
 * Run the demo choreography against a live backend.
 *
 * Off unless `NEXT_PUBLIC_DEMO_DISPATCH=true` was set at launch, which only
 * `make live-demo` does. It exists so the fleet can be shown working against
 * real Vertex, real Firestore and real municipal feeds without somebody having
 * to hand-drive a dispatch mid-sentence -- and it is opt-in rather than
 * inferred precisely because a console that decided on its own to simulate a
 * 911 call would be indefensible.
 *
 * The call it places is still labelled synthetic wherever it appears.
 */
function demoDispatchRequested(): boolean {
  // The launch flag, inlined at build time by Next.
  if (process.env.NEXT_PUBLIC_DEMO_DISPATCH === 'true') return true;
  // And `?demo=1` on the URL, which does not depend on the console having been
  // started with the right environment. That matters more than it sounds: a
  // `NEXT_PUBLIC_*` value is baked in when the bundle is built, so a console
  // already running cannot be talked into the choreography, and an operator
  // about to present has no way to tell whether the flag took. The query
  // parameter is checked in the browser, at the moment the page loads, and is
  // visible in the address bar -- which is the property that makes it usable
  // under pressure.
  if (typeof window === 'undefined') return false;
  try {
    return new URLSearchParams(window.location.search).get('demo') === '1';
  } catch {
    return false;
  }
}

/** What one step of the sweep reports back. A refusal is a value: `flown` false
 *  with a reason, never an exception. */
interface DroneSweepResult {
  flown: boolean;
  complete: boolean;
  reason?: string;
  face?: string;
}

/**
 * What one slow-loop pass reported, in one line.
 *
 * Every clause is read off the pass's own report and omitted when the report
 * does not carry it. A pass that wrote nothing says "0 facts written", which is
 * a result; a pass whose report has no such field says nothing at all, which is
 * an absence. The two must not be summarised into the same sentence.
 */
function summarisePass(report: unknown): string {
  const parts: string[] = [];
  if (typeof report === 'object' && report !== null) {
    const row = report as Record<string, unknown>;
    if (typeof row.facts_written === 'number') parts.push(`${row.facts_written} facts written`);
    if (Array.isArray(row.conflicts)) parts.push(`${row.conflicts.length} conflicts detected`);
    if (typeof row.queue_size === 'number') parts.push(`queue re-ranked to ${row.queue_size}`);
    if (Array.isArray(row.unavailable_sources) && row.unavailable_sources.length > 0) {
      parts.push(`${row.unavailable_sources.length} sources UNAVAILABLE`);
    }
  }
  return parts.length > 0 ? `Slow-loop pass complete: ${parts.join(', ')}.` : 'Slow-loop pass complete.';
}

type BackendState = 'checking' | 'ready' | 'degraded' | 'unreachable';

/**
 * The only status chrome left in the header: is the backend there at all.
 *
 * The row of chips this replaces -- mode, storage backend, event backend,
 * municipality, version -- told an operator things that never change while
 * they watch, and cost the screen a band of pixels to do it. One thing does
 * change and does matter: a district with no work queued and a district whose
 * backend is dead render identically otherwise, and an officer must never read
 * the second as the first.
 *
 * So: a dot. Silent when the backend answers, a word when it does not.
 */
function BackendSignal({
  initial,
  statusMissing,
}: {
  initial: Readiness | null;
  statusMissing: boolean;
}) {
  const [state, setState] = useState<BackendState>(() => {
    // The server render already tried and failed; say so on the first paint
    // rather than showing "checking" until a poll confirms what is known.
    if (statusMissing) return 'unreachable';
    if (initial) return initial.ready ? 'ready' : 'degraded';
    return 'checking';
  });
  const [detail, setDetail] = useState<string | null>(initial?.status ?? null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function poll() {
      // Through the console's own gateway, not straight at the backend: this
      // runs in the browser and the credential lives on the server.
      const result = await browserGet<Readiness>('/readyz', { signal: controller.signal });
      if (cancelled) return;
      if (result.ok) {
        setState(result.data.ready ? 'ready' : 'degraded');
        setDetail(result.data.status);
      } else {
        setState('unreachable');
        setDetail(result.error.message);
      }
    }

    void poll();
    const timer = setInterval(() => void poll(), READINESS_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  const label =
    state === 'ready'
      ? 'Backend reachable'
      : state === 'degraded'
        ? 'Backend degraded'
        : state === 'unreachable'
          ? 'Backend unreachable'
          : 'Checking backend';

  const tone =
    state === 'ready'
      ? 'text-confirmed'
      : state === 'degraded'
        ? 'text-disputed'
        : state === 'unreachable'
          ? 'text-alarm'
          : 'text-muted';

  return (
    <span
      className="flex items-center gap-1.5 text-micro uppercase tracking-wide"
      aria-live="polite"
      aria-atomic="true"
      title={detail ? `${label}: ${detail}` : label}
      data-testid="backend-signal"
    >
      <span aria-hidden="true" className={tone}>
        {state === 'ready' ? '●' : state === 'unreachable' ? '■' : '▲'}
      </span>
      {/* Healthy is the boring case: it stays available to a screen reader and
          out of a commander's way. Anything else is said in words. */}
      <span className={state === 'ready' ? 'sr-only' : tone}>{label}</span>
    </span>
  );
}

export interface CommandCenterProps {
  status: SystemStatus | null;
  readiness: Readiness | null;
  error: string | null;
  /** Injected by tests and by the server render; fetched otherwise. */
  initialStats?: DistrictStatsView | null;
  initialQueue?: QueueView | null;
  initialAgents?: AgentDescriptorView[];
  initialSubscriptions?: SubscriptionView[];
  initialEvents?: AuditEventView[];
  initialDecisions?: PolicyDecisionView[];
  /** Set by the WebGL-disabled test path. */
  forceSvgGeometry?: boolean;
}

export function CommandCenter({
  status,
  readiness,
  error,
  initialStats = null,
  initialQueue = null,
  initialAgents = [],
  initialSubscriptions = [],
  initialEvents = [],
  initialDecisions = [],
  forceSvgGeometry = false,
}: CommandCenterProps) {
  const districtId = status?.districts[0] ?? 'sffd-district-03';

  const [stats, setStats] = useState<DistrictStatsView | null>(initialStats);
  const [queue, setQueue] = useState<QueueView | null>(initialQueue);
  /** Which way the incident building is being looked at. Street on arrival. */
  /** Street and aerial are photographs the backend fetches; `3d` is the tile
      renderer, which streams in the browser. One control, three viewpoints --
      the type is wider than `ImageryView` because only two of them are a
      request the imagery port knows how to serve.
   *
   * Opens on `3d`: it is the view that shows the building *and* what is packed
   * around it, which is the first thing asked on arrival, and unlike the
   * photographs it costs no metered request per address. */
  const [imageryView, setImageryView] = useState<ImageryView | '3d'>('3d');
  const [agents] = useState<AgentDescriptorView[]>(initialAgents);
  const [subscriptions, setSubscriptions] = useState<SubscriptionView[]>(initialSubscriptions);
  const [events, setEvents] = useState<AuditEventView[]>(initialEvents);
  const [decisions, setDecisions] = useState<PolicyDecisionView[]>(initialDecisions);

  /**
   * The instant this console session started watching the fleet, in the
   * backend's clock. Everything at or before it belongs to a previous run.
   *
   * There is one of these for the whole console and it is set once, because
   * `AgentRail` is mounted twice in either mode and the layout swaps those
   * mounts when a fire starts -- a floor each panel captured for itself would
   * re-anchor on that swap, mid-pass, and blank a fleet that was working.
   *
   * It is not `Date.now()`. The timestamps it is compared against are stamped
   * by the backend and this browser's clock is a different clock, so a floor
   * from here would hide live work or admit stale work by however far the two
   * have drifted -- and would not even be the same string format. It is instead
   * the newest instant in the *first audit read that answers*, which is a value
   * the backend wrote, in the backend's format, naming the last thing that had
   * already happened when we arrived. `null` while no read has answered and
   * after one that came back empty; an empty log needs no floor.
   *
   * `initialEvents` is seeded into the floor for the same reason: a caller that
   * hands the console a log has handed it a log from before it mounted.
   */
  const floorAnchored = useRef(initialEvents.length > 0 || initialDecisions.length > 0);
  /**
   * Whether this console has set the fleet working yet.
   *
   * The floor is only honest while it is a statement about a log this session
   * did not write. The first fleet read is issued on mount and the choreography
   * starts a pass three seconds later, which is fine until the read is slow --
   * and against a live backend it is the slowest read on the screen, because
   * `list_events` reads the whole audit collection and decodes it. When that
   * read times out, the next one to answer arrives *after* the pass has been
   * writing, `sessionFloor` anchors on the newest instant in it, and the
   * console floors out the work it just commissioned: every agent `0 recorded`,
   * every agent idle, for a pass that ran in full.
   *
   * So anchoring stops the moment a pass starts. What is lost by refusing to
   * anchor is that the column counts from the whole session rather than from
   * arrival -- and the slow loop's own `currentPass` scoping narrows that back
   * to the pass in flight anyway, while the incident column scopes to the fire.
   * Under-reporting a previous run is the failure this floor exists to prevent;
   * showing a working fleet as idle is worse than either.
   */
  const passStarted = useRef(false);
  const [since, setSince] = useState<string | null>(() =>
    sessionFloor(initialEvents, initialDecisions),
  );
  const [agentList, setAgentList] = useState<AgentDescriptorView[]>(initialAgents);

  const [selected, setSelected] = useState<string | null>(null);
  /** The transcript this incident was dispatched with, if any. */
  const [narrative, setNarrative] = useState('');
  /** Referrals staged in this session.
   *
   * The profile carries `open_referrals` only once a referral has been
   * *filed* -- the backend writes it back with the case number the building
   * department returned. A referral that is staged and awaiting a captain
   * exists in the referral store and on no profile, so the console would have
   * nothing to offer a captain to approve. Holding it here closes that gap
   * without changing what a profile means. A reload loses it; the filed ones
   * come back from the profile, which is the half that has to survive.
   */
  const [staged, setStaged] = useState<ReferralSummary[]>([]);
  const [profile, setProfile] = useState<BuildingProfileView | null>(null);
  /**
   * The structure panel, so opening one can be seen to have happened.
   *
   * The middle column is the region above and the structure below, and the map
   * is deliberately tall -- it is the subject of the standby screen and takes
   * the column. So a profile opened from a conflict card lands some six hundred
   * pixels down a pane that scrolls on its own: it rendered, it was correct,
   * and nothing on screen moved. Clicking the disagreement an officer was told
   * to look at and seeing nothing change reads as a broken console.
   */
  const profileRef = useRef<HTMLDivElement | null>(null);
  const [timeline, setTimeline] = useState<TimelineEventView[]>([]);
  const [geometry, setGeometry] = useState<GeometryView | null>(null);
  /** Whether geometry is on its way, here, or not coming. `geometry === null`
      cannot say which, and a panel that guessed reported a backend fault
      while the request was still open. */
  const [geometryState, setGeometryState] = useState<GeometryState>('idle');
  /** Why the sweep stopped early, when it did. Null while it is flying or done. */
  const [sweepNotice, setSweepNotice] = useState<string | null>(null);
  const [view, setView] = useState<ViewAngle>('ISO');


  const [incident, setIncident] = useState<OpenIncidentResponse | null>(null);
  const [outcomes, setOutcomes] = useState<ResourceOutcomeView[]>([]);
  /** Which write is holding the console, or null when none is. Named rather
      than counted, because the screen has to say which one -- see
      `InFlightAction`. */
  const [inFlight, setInFlight] = useState<InFlightAction | null>(null);
  //: Everything that only needs "is a write running" still reads a boolean, so
  //: naming the action changed which controls disable, and nothing else.
  const busy = inFlight !== null;
  const [notice, setNotice] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');

  /** Regional fire activity. `null` until the first read answers. */
  const [fireActivity, setFireActivity] = useState<FireActivity | null>(null);
  /** A failed *request*, kept apart from an answered request carrying a
      refusal: the endpoint always returns 200, so a transport failure and an
      unconfigured key are different facts and must not render alike. */
  const [fireActivityError, setFireActivityError] = useState<string | null>(null);
  /** When the last fire-activity read went out, for the staleness check. */
  const fireActivityAtRef = useRef(0);

  /**
   * The ground plane under the regional heat map.
   *
   * Fetched once per district and deliberately *not* on the standby heartbeat:
   * it is a half-megabyte image inline in a JSON body, it is cached for a week
   * at the backend, and it changes only when somebody reconfigures the region.
   * Putting it on a four-second poll would ship it fifteen times a minute to
   * redraw an identical picture.
   */
  const [basemap, setBasemap] = useState<RegionBasemapView | null>(null);

  /** The hand-run slow-loop pass: idle, in flight, or finished with a word. */
  const [passRunning, setPassRunning] = useState(false);
  /**
   * Seconds the current pass has been running, or null between passes.
   *
   * A live district pass is minutes, not seconds: the loop is serial end to
   * end, and against real feeds one measured **318s** -- records-watcher,
   * geometry, the hazard graph and then a profile materialised per address,
   * one at a time. A status line with no clock on it is indistinguishable from
   * a hung request, and the honest fix while the loop is still serial is to
   * say how long it has been rather than to imply it is nearly done.
   */
  const [passElapsed, setPassElapsed] = useState<number | null>(null);
  //: When the last pass finished, for the off-screen slow-loop line. Null until
  //: one has, because "no pass yet" and "a pass just now" are different claims.
  const [passAt, setPassAt] = useState<number | null>(null);
  //: Seconds until the demo dispatches, or null when nothing is scheduled.
  const [callIn, setCallIn] = useState<number | null>(null);
  /**
   * The sample the incident was opened from, when it was opened from one.
   *
   * Only the recording depends on this. The transcript already travelled to the
   * backend and is what the model read; this is how the console finds the audio
   * that goes with it, and `null` simply means no recording to play.
   */
  const [callAudioSrc, setCallAudioSrc] = useState<string | null>(null);
  /**
   * Whether the arriving-call overlay is up.
   *
   * Presentational only. The dispatch has already fired when this goes true --
   * the instant brief is rendering underneath and the fleet is already awake --
   * so dismissing it changes what is on screen and nothing else.
   */
  const [callOnScreen, setCallOnScreen] = useState(false);
  //: Set once a call has been placed, by anyone, so the demo never fires a
  //: second one on top of a live incident.
  const dispatchedRef = useRef(false);
  //: The viewer stopped the clock. Sticky: a demo you had to keep re-cancelling
  //: mid-sentence would be worse than no demo.
  const [autoCallOff, setAutoCallOff] = useState(false);
  //: What the demo timers read. A ref, so a refresh that changes any of it does
  //: not tear the timers down -- see the comment on the choreography effect.
  const demoRef = useRef({
    queue: null as QueueView | null,
    passRunning: false,
    busy: false,
    runPass: async () => {},
    // Four parameters, and the fourth is why: the recording travels with the
    // transcript. A three-argument type here silently dropped it and the
    // arriving-call panel came up with no player on it.
    dispatch: async (
      _a: string,
      _n: string,
      _c: IntakeChannel,
      _audioSrc?: string,
    ) => {},
  });
  const [passNotice, setPassNotice] = useState<string | null>(null);

  const stream = useBriefStream(incident?.incident_id ?? null);
  // Opening this stream is what *asks* for the prose -- it replaces the
  // blocking POST the console used to fire and then wait on in silence. The
  // persisted emission still arrives on the brief stream above; this carries
  // only the provisional text, as it is written.
  // Named `prose` rather than `narrative`: `narrative` above is the 911
  // transcript this incident arrived with, which is a different thing entirely
  // -- one is what a caller said, the other is what the model composed.
  const prose = useNarrativeStream(incident?.incident_id ?? null);
  /**
   * The incident log as it is written, for the agent cards above the brief.
   *
   * Its own stream rather than a field on the brief stream: the two answer
   * different questions and move at different rates, and a log entry that had
   * to wait for a brief version would arrive late for no reason.
   */
  const incidentLog = useIncidentLogStream(incident?.incident_id ?? null);
  const announcedRef = useRef<number>(0);

  /**
   * Entry packages: off the log stream first, off the endpoint regardless.
   *
   * No second connection -- every package state change appends an
   * `ENTRY_PACKAGE` entry carrying the whole document, so a package composed by
   * the loop, a half signed by an officer and a send normally arrive on the
   * same feed the agent cards are drawn from, and that stays the fast path.
   *
   * It is not the only path any more, and the reason is in
   * `lib/api/entry-packages.ts`: the log stream is snapshot-and-close, so
   * everything after the first frame really arrives on the browser's reconnect,
   * and one non-SSE answer on that URL closes it permanently and silently. The
   * hook polls the packages endpoint underneath for as long as the incident is
   * open. Both sources fold into the same `package_id`-keyed state, so the card
   * below is still raised exactly once however the package got here.
   */
  const entryPackages = useEntryPackages(incident?.incident_id ?? null, incidentLog.entries);
  /** The package the modal is showing. `null` when it is closed. */
  const [reviewing, setReviewing] = useState<EntryPackageView | null>(null);
  /**
   * Packages the modal has already come up for, so dismissing one is final.
   *
   * A modal that reopened on the next log frame would be a modal an officer
   * cannot get out of -- and every approval writes a frame, so it would reopen
   * on the officer's own tap.
   */
  const raisedFor = useRef<Set<string>>(new Set());
  /** Which leg the leg list has under cursor or selection, for the model. */
  const [legSelection, setLegSelection] = useState<LegSelection | null>(null);
  /**
   * The sheet that covers the console while a released incident resolves.
   *
   * Set only after a dispatch came back ok. Nothing here decides that an
   * incident is over: the send returning is what does, and the close call
   * behind this sheet is the same one the banner's close control makes.
   */
  const [resolving, setResolving] = useState<string | null>(null);

  // The brief the officer is looking at: whatever arrived last on the stream,
  // falling back to the instant brief the open call already returned.
  const emissions: BriefEmissionView[] = useMemo(() => {
    if (stream.emissions.length > 0) return stream.emissions;
    return incident ? [incident.brief] : [];
  }, [stream.emissions, incident]);
  const latest = emissions[emissions.length - 1] ?? null;

  // Announce each new version once, politely. An officer who cannot see the
  // version tick still hears that the brief changed.
  useEffect(() => {
    if (!latest || latest.version === announcedRef.current) return;
    announcedRef.current = latest.version;
    setAnnouncement(announcementFor(latest));
  }, [latest]);

  /**
   * Read the region's satellite fire activity.
   *
   * Not a loop of its own: this is called once when standby opens and then only
   * from the standby poll, which is the console's single heartbeat.
   */
  /**
   * Read the ground plane once, when the district's region becomes known.
   *
   * Keyed on the district rather than the region box: the box comes from the
   * fire-activity answer, and the backend derives the basemap from that same
   * answer, so there is nothing here to pass and nothing that could disagree.
   *
   * A failure is silent on purpose. The heat map draws its rings, its bins and
   * its key without a ground plane; what it loses is the coastline under them,
   * and an error banner over a working map would misrepresent that.
   */
  useEffect(() => {
    let live = true;
    void browserGet<RegionBasemapView>(
      `/api/v1/districts/${districtId}/fire-activity/basemap`,
    ).then((result) => {
      if (live && result.ok) setBasemap(result.data);
    });
    return () => {
      live = false;
    };
  }, [districtId]);

  const refreshFireActivity = useCallback(
    async (signal?: AbortSignal) => {
      fireActivityAtRef.current = Date.now();
      const result = await browserGet<unknown>(
        `/api/v1/districts/${districtId}/fire-activity`,
        { signal },
      );
      // A torn-down poll aborts, which comes back `ok: false`. That is not a
      // failure an officer should be told about.
      if (signal?.aborted) return;
      if (result.ok) {
        setFireActivity(normalizeFireActivity(result.data));
        setFireActivityError(null);
        return;
      }
      setFireActivityError(result.error.message);
    },
    [districtId],
  );

  /**
   * The fleet's own evidence: the audit log and the gateway's decisions.
   *
   * **Its own function, and its own timer, because it is the one read that must
   * not stop when an incident opens.** It used to ride the standby poll, and
   * that poll is torn down by the state change that opens the incident stream
   * -- so for the whole ninety seconds the incident agents were actually
   * working, the console was still holding the audit log as it stood at
   * dispatch. `incident-interceptor`, `incident-recorder` and `sensor-fusion`
   * showed idle through an entire incident not because they recorded nothing,
   * but because nobody asked again.
   *
   * **It accumulates rather than replaces.** The endpoint answers with the
   * newest N events across the whole fleet, and during an incident a handful of
   * agents write fast enough to push everyone else out of that window -- so an
   * agent that did real work a minute ago would silently revert to idle. The
   * panel's own words are that active means "it has recorded work this session",
   * and a session is exactly what merging by id keeps. Nothing is invented: an
   * event is only ever added by having been read from the audit log.
   */
  const refreshFleet = useCallback(
    async (signal?: AbortSignal) => {
      const [eventsResult, decisionsResult] = await Promise.all([
        browserGet<AuditEventView[]>(`/api/v1/internal/audit/events?limit=${AUDIT_WINDOW}`, {
          signal,
          timeoutMs: STANDBY_READ_TIMEOUT_MS,
        }),
        browserGet<PolicyDecisionView[]>(`/api/v1/internal/audit/decisions?limit=${AUDIT_WINDOW}`, {
          signal,
          timeoutMs: STANDBY_READ_TIMEOUT_MS,
        }),
      ]);
      // The floor, from the first read that answers and never again.
      //
      // Before the merge, and over the raw page rather than over the merged
      // state, because the point of the floor is the log *as it stood when we
      // arrived* -- a value taken after merging would be indistinguishable from
      // a value taken after the fleet had written something.
      //
      // Anchored on whichever of the two reads answered. Requiring both would
      // leave the console unfloored, and therefore counting a previous run's
      // work, for as long as one endpoint stayed down.
      if (!floorAnchored.current && passStarted.current) {
        // Settled rather than deferred: once a pass has written, no later read
        // can tell us what the log held before it. See `passStarted`.
        floorAnchored.current = true;
      }
      if (!floorAnchored.current && (eventsResult.ok || decisionsResult.ok)) {
        floorAnchored.current = true;
        // Same guard `mergeById` carries, for the same reason: a gateway or a
        // proxy can answer with something that is not a list, and an unfloored
        // console is the failure this whole mechanism exists to prevent.
        const floor = sessionFloor(
          eventsResult.ok && Array.isArray(eventsResult.data) ? eventsResult.data : [],
          decisionsResult.ok && Array.isArray(decisionsResult.data) ? decisionsResult.data : [],
        );
        if (floor !== null) setSince(floor);
      }
      // An aborted request comes back `ok: false`, so a torn-down poll writes
      // no state: there is no unmount guard to forget.
      if (eventsResult.ok) {
        setEvents((current) => mergeById(current, eventsResult.data, (e) => e.audit_id));
      }
      if (decisionsResult.ok) {
        setDecisions((current) =>
          mergeById(current, decisionsResult.data, (d) => d.decision_id),
        );
      }
    },
    [],
  );

  const refreshStandby = useCallback(
    async (signal?: AbortSignal, options: { forceFireActivity?: boolean } = {}) => {
      // The audit event and decision streams are no longer rendered as a console
      // of their own. They are still fetched: the fleet panel builds each agent's
      // reasoning box out of the events that agent is the `actor` of, so this is
      // the fleet's data, not the audit console's leftovers.
      //
      // The queue is still fetched too, though the ranked-queue panel is gone:
      // its rows are the only list of addresses the console has, and selecting
      // a structure is what opens a profile and arms a dispatch.
      //
      // Fire activity rides this same pass rather than getting a timer of its
      // own, and is skipped while the last read is still fresh -- see
      // `FIRE_ACTIVITY_MAX_AGE_MS`.
      const fireIsStale =
        options.forceFireActivity === true ||
        Date.now() - fireActivityAtRef.current >= FIRE_ACTIVITY_MAX_AGE_MS;

      // `STANDBY_READ_TIMEOUT_MS`, not the client's 4-second default.
      //
      // These are quick reads against an idle backend and slow ones against a
      // busy one: a live slow-loop pass holds Firestore for minutes, and the
      // district read queues behind it. At 4s they aborted, the queue stayed
      // null, and the demo's 911 call had no address to dispatch against --
      // which is how a console that looked fine never placed a call.
      const [statsResult, queueResult] = await Promise.all([
        browserGet<DistrictStatsView>(`/api/v1/districts/${districtId}/stats`, {
          signal,
          timeoutMs: STANDBY_READ_TIMEOUT_MS,
        }),
        browserGet<QueueView>(`/api/v1/districts/${districtId}/queue`, {
          signal,
          timeoutMs: STANDBY_READ_TIMEOUT_MS,
        }),
        // The fleet's evidence rides along here so a standby tick is one round
        // trip, but it has its own timer as well -- see `refreshFleet`.
        refreshFleet(signal),
        fireIsStale ? refreshFireActivity(signal) : Promise.resolve(),
      ]);
      // An aborted request comes back `ok: false`, so a torn-down poll writes
      // no state: there is no unmount guard to forget.
      if (statsResult.ok) setStats(statsResult.data);
      if (queueResult.ok) setQueue(queueResult.data);
    },
    [districtId, refreshFireActivity, refreshFleet],
  );

  useEffect(() => {
    if (initialAgents.length > 0) return;
    void (async () => {
      const [agentsResult, subsResult] = await Promise.all([
        browserGet<AgentListResponse>('/api/v1/registry/agents'),
        browserGet<SubscriptionListResponse>(
          '/api/v1/registry/subscriptions?subscriber_department=fire',
        ),
      ]);
      if (agentsResult.ok) setAgentList(agentsResult.data.agents);
      if (subsResult.ok) setSubscriptions(subsResult.data.subscriptions);
      await refreshStandby();
    })();
  }, [initialAgents.length, refreshStandby]);

  /**
   * The standby heartbeat.
   *
   * One interval, no second loop: it exists only while `incident` is null, so
   * it is torn down by the same state change that opens the SSE stream and
   * re-armed by the one that closes it. `inFlight` keeps a slow round trip from
   * stacking requests behind itself on a bad connection, and a hidden tab does
   * not poll at all.
   */
  useEffect(() => {
    if (incident) return;
    const controller = new AbortController();
    let inFlight = false;

    const timer = setInterval(() => {
      if (inFlight) return;
      if (typeof document !== 'undefined' && document.hidden) return;
      inFlight = true;
      void refreshStandby(controller.signal).finally(() => {
        inFlight = false;
      });
    }, STANDBY_POLL_MS);

    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [incident, refreshStandby]);

  /**
   * The fleet poll, which runs whether or not an incident is open.
   *
   * Deliberately not folded into the effect above. That one is torn down when
   * an incident opens, because the district's standby numbers stop being the
   * thing on screen -- but the fleet panel is on screen the whole time, and it
   * is *during* an incident that its agents are busiest. Sharing that teardown
   * is what left three incident agents reading idle through the ninety seconds
   * they were doing all their work.
   *
   * Faster than the standby tick: two small reads, and they are what turns an
   * agent from idle to active on screen.
   */
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;

    const tick = () => {
      if (inFlight) return;
      if (typeof document !== 'undefined' && document.hidden) return;
      inFlight = true;
      void refreshFleet(controller.signal).finally(() => {
        inFlight = false;
      });
    };
    // Immediately, not one interval from now: on a fresh load the fleet is
    // drawn from an empty list, and waiting to fill it shows every agent in the
    // catalog as idle for the first few seconds of the demo.
    tick();
    const timer = setInterval(tick, FLEET_POLL_MS);

    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [refreshFleet]);

  /**
   * The first fire-activity read, and the one after an incident closes.
   *
   * One request, not a loop: the panel would otherwise sit empty until the
   * standby poll's first tick, and a map that has not answered yet looks
   * exactly like a region with nothing burning in it.
   */
  useEffect(() => {
    if (incident) return;
    const controller = new AbortController();
    void refreshFireActivity(controller.signal);
    return () => controller.abort();
  }, [incident, refreshFireActivity]);

  /**
   * Run one complete slow-loop pass, by hand.
   *
   * A scheduler drives this in production. Exposing it here is not a refresh
   * button: the request polls every source, writes the facts it finds, detects
   * conflicts and re-ranks the queue, so the district bar, the ranked strip and
   * every agent's reasoning box tick because work actually happened. Calling it
   * "refresh" would describe the screen instead of the system.
   */
  // The clock behind the status line. Runs only while a pass is in flight, so
  // an idle console has no timer in it.
  useEffect(() => {
    if (!passRunning) return;
    const started = Date.now();
    const tick = setInterval(() => {
      setPassElapsed(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => clearInterval(tick);
  }, [passRunning]);

  const runSlowLoopPass = useCallback(async () => {
    // Before the request goes out, not after it answers: the pass is writing
    // from the moment the backend picks it up, and a floor anchored on a read
    // that overlaps it hides exactly this work. See `passStarted`.
    passStarted.current = true;
    setPassRunning(true);
    setPassElapsed(0);
    setPassNotice(null);
    const result = await browserPost<Record<string, unknown>>(
      `/api/v1/districts/${districtId}/poll`,
      {},
      // A slow-loop pass is not a request, it is a job.
      //
      // The client's 4-second default is right for every read on this console
      // and wrong for this one: a live district pass runs the whole fleet
      // serially and one measured 318 seconds. At 4s the browser aborted every
      // pass and reported "Slow-loop pass failed: signal is aborted without
      // reason" while the backend went on and finished the work -- a failure
      // message about a pass that succeeded, which is the worst of both.
      { timeoutMs: SLOW_LOOP_TIMEOUT_MS },
    );
    if (!result.ok) {
      setPassRunning(false);
      setPassElapsed(null);
      setPassNotice(`Slow-loop pass failed: ${result.error.message}`);
      return;
    }
    // Read the district back before saying the pass is done, so the counts on
    // screen are the ones the pass produced rather than the ones before it.
    await refreshStandby(undefined, { forceFireActivity: true });
    setPassRunning(false);
    setPassElapsed(null);
    setPassAt(Date.now());
    setPassNotice(summarisePass(result.data));
  }, [districtId, refreshStandby]);

  const openProfile = useCallback(async (addressId: string) => {
    setSelected(addressId);
    setGeometryState('loading');
    const [profileResult, timelineResult, geometryResult] = await Promise.all([
      browserGet<BuildingProfileView>(`/api/v1/buildings/${addressId}`),
      browserGet<TimelineEventView[]>(`/api/v1/buildings/${addressId}/timeline`),
      browserGet<GeometryView>(`/api/v1/buildings/${addressId}/geometry`),
    ]);
    setProfile(profileResult.ok ? profileResult.data : null);
    setTimeline(timelineResult.ok ? timelineResult.data : []);
    setGeometry(geometryResult.ok ? geometryResult.data : null);
    setGeometryState(geometryResult.ok ? 'ready' : 'unavailable');
  }, []);

  /**
   * Bring an opened structure to the top of its column.
   *
   * Keyed on the address rather than the object, so re-reading the same
   * building after a sweep does not yank the column back while somebody is
   * reading further down it. Motion is dropped for anyone who has asked for
   * less of it -- the point is arriving, not the travelling.
   */
  const openedAddress = profile?.address_id ?? null;
  useEffect(() => {
    if (!openedAddress) return;
    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
    // Called optionally: not every environment this renders in implements it,
    // and a console that throws while opening a building is far worse than one
    // that opens it without scrolling.
    profileRef.current?.scrollIntoView?.({
      behavior: reduced ? 'auto' : 'smooth',
      block: 'start',
    });
  }, [openedAddress]);

  /**
   * Fly the drone sweep, one wall per tick.
   *
   * The console decides *when*, and nothing else. Each call advances the sweep
   * by one face inside the backend, where **Sensor Fusion** reads the frame,
   * registers the thermal observation and amends the brief; the profile is
   * re-read afterwards so the massing model paints what the agent recorded
   * rather than what this component predicted.
   *
   * Stopping conditions are the backend's, not a counter here: it reports
   * `complete` when every face the footprint has is covered, and a `reason`
   * when it refuses -- a live vision model, or an address the slow loop never
   * profiled. Either way the sweep stops and says so instead of retrying into
   * a wall that will never resolve.
   */
  const sweepRef = useRef<{ running: boolean; stop: boolean }>({ running: false, stop: false });

  /**
   * Ask for the autonomous notifications, one at a time.
   *
   * Sequential and spaced, so each arrives as its own card rather than four at
   * once -- the point of the panel is watching the fleet work, and a burst is
   * indistinguishable from a single step. A refusal is left to the notice the
   * request path already sets: the agent declining a notification it cannot
   * support is a correct outcome, not an error to interrupt the demo with.
   */
  /**
   * Read the transcript, after the banner is already up.
   *
   * A failure here is not a failed incident. The instant brief is persisted and
   * on screen before this is called, so an intake that times out, is refused by
   * the screener, or comes back from a model that is unreachable leaves the
   * incident exactly as it was -- which is the same guarantee `_read_intake`
   * makes on the backend. It says so in the notice rather than silently, since
   * a console that dropped a caller's words without a word would be the failure
   * mode nobody catches until afterwards.
   */
  const readIntake = useCallback(
    async (incidentId: string, narrative: string, channel: IntakeChannel) => {
      if (!narrative) return;
      const result = await browserPost<IntakeResponse>(
        `/api/v1/incidents/${incidentId}/intake`,
        { narrative, channel },
        { timeoutMs: INTAKE_TIMEOUT_MS },
      );
      if (!result.ok) {
        setNotice(`The call was not read: ${result.error.message}`);
        return;
      }
      // Guarded on the id: by the time a slow read returns, the console may be
      // on a different incident, and writing this one's transcript onto that
      // one would attribute a caller's words to the wrong fire.
      setIncident((current) =>
        current && current.incident_id === incidentId
          ? { ...current, intake: result.data }
          : current,
      );
    },
    [],
  );

  /**
   * Tell every agency at once, not one every couple of seconds.
   *
   * These were sequential with a 2.2s pause between them, which is nine seconds
   * of an incident's first minute spent waiting on nothing -- and against real
   * services the request latency was on top of that, so a demo could end with
   * two of the four sent. They are independent notifications to different
   * departments and nothing orders them, so they go together and land as a
   * burst, which is also what a real dispatch looks like.
   *
   * Concurrency here is only safe because the outcome each one produces is now
   * filed under its own correlation id; it used to be filed under the incident,
   * where four at once overwrote each other and a notification to one agency
   * could be reported under another's name.
   */
  const notifyAgencies = useCallback(
    async (incidentId: string) => {
      await Promise.all(
        DEMO_NOTIFICATIONS.map(async (kindId) => {
          const result = await browserPost<ResourceOutcomeView>(
            `/api/v1/incidents/${incidentId}/resources`,
            { kind_id: kindId },
            { timeoutMs: NOTIFY_TIMEOUT_MS },
          );
          if (!result.ok) return;
          setOutcomes((current) => [
            ...current.filter((o) => o.kind_id !== kindId),
            result.data,
          ]);
        }),
      );
    },
    [],
  );

  const flyDroneSweep = useCallback(
    async (incidentId: string, addressId: string) => {
      if (sweepRef.current.running) return;
      sweepRef.current = { running: true, stop: false };
      setSweepNotice(null);
      try {
        for (let face = 0; face < SWEEP_FACES; face += 1) {
          if (sweepRef.current.stop) return;
          const result = await browserPost<DroneSweepResult>(
            `/api/v1/incidents/${incidentId}/drone-sweep`,
            {},
            { timeoutMs: SWEEP_TIMEOUT_MS },
          );
          if (!result.ok) {
            setSweepNotice(`Drone sweep stopped: ${result.error.message}`);
            return;
          }
          if (!result.data.flown) {
            // `complete` is the ordinary end of a sweep, not a fault.
            if (!result.data.complete) setSweepNotice(result.data.reason ?? 'Drone sweep refused.');
            return;
          }
          // Re-read the structure so the thermal on screen is the agent's.
          await openProfile(addressId);
          if (result.data.complete) return;
          await new Promise((resolve) => setTimeout(resolve, SWEEP_INTERVAL_MS));
        }
      } finally {
        sweepRef.current.running = false;
      }
    },
    [openProfile],
  );


  const dispatch = useCallback(
    async (
      addressId: string,
      narrative = '',
      channel: IntakeChannel = 'CALL_911',
      audioSrc?: string,
    ) => {
      dispatchedRef.current = true;
      setCallIn(null);
      setCallAudioSrc(audioSrc ?? null);
      if (audioSrc) setCallOnScreen(true);
      setInFlight('dispatch');
      setNotice(null);
      // The narrative is kept so the intake panel can check a quote against
      // the offsets it claims. Without the source text, a span is unverifiable.
      setNarrative(narrative);
      // The narrative is deliberately *not* sent with the open.
      //
      // `POST /incidents` reads a dispatch narrative inline, after it persists
      // the instant brief, so one request carried both a sub-second write and a
      // Gemini extraction. Against real services that request routinely ran past
      // the client's default timeout, and the abort surfaced as
      // `Could not open an incident: signal is aborted without reason` -- for an
      // incident that had in fact opened.
      //
      // Splitting them restores what the instant brief is *for*: version 1 is
      // model-free and on screen in well under a second, and the transcript
      // amends it when the model comes back. `POST /incidents/{id}/intake` runs
      // the same `_read_intake` the inline path does, so nothing about how the
      // narrative is screened, bound to spans, or routed changes.
      const result = await browserPost<OpenIncidentResponse>(
        '/api/v1/incidents',
        {
          address: addressId,
          cad_ref: `CAD-${Date.now().toString().slice(-6)}`,
          alarm_level: 2,
        },
        { timeoutMs: OPEN_INCIDENT_TIMEOUT_MS },
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Could not open an incident: ${result.error.message}`);
        return;
      }
      setIncident(result.data);
      setOutcomes([]);
      announcedRef.current = 0;
      await openProfile(result.data.address_id);
      // Prose is no longer requested here. `useNarrativeStream` opens
      // `/brief/stream-enriched` for this incident, which both asks for the
      // composition and delivers it token by token -- the instant brief is
      // already on screen before the first chunk lands, exactly as before.
      // And the drone goes up. Not awaited: the brief is what the first ninety
      // seconds are for, and the sweep paints onto it as each wall lands.
      void flyDroneSweep(result.data.incident_id, result.data.address_id);
      // And the agencies. Runs beside the sweep rather than after it: both are
      // things the fleet does in the first minute, and serialising them would
      // put the notifier's work after the window it belongs in.
      void notifyAgencies(result.data.incident_id);
      // And the transcript, beside the sweep rather than ahead of the banner.
      void readIntake(result.data.incident_id, narrative, channel);
    },
    [openProfile, flyDroneSweep, notifyAgencies, readIntake],
  );

  demoRef.current = {
    queue,
    passRunning,
    busy,
    runPass: runSlowLoopPass,
    dispatch,
  };

  /**
   * Standby runs itself: real passes on an interval, then a call.
   *
   * **Auto-dispatch is gated on the backend calling itself fake, or on somebody
   * explicitly asking for it.** `status.mode` comes from
   * `/api/v1/system/status`; anything other than the string `fake` -- including
   * a status this console has not managed to read yet -- means no call is
   * placed. Software that invented a 911 call on a real deployment would be the
   * worst thing in this repository, so the gate is a positive check on a known
   * value rather than an absence of a live flag. **Do not relax it into
   * `!== 'live'`.**
   *
   * The one way past it is `NEXT_PUBLIC_DEMO_DISPATCH=true`, which `make
   * live-demo` sets and nothing else does. That is a different statement from
   * inferring permission: a live console still never decides on its own to
   * simulate a call, and an operator who wants the choreography against real
   * services has to say so at launch. The banner it produces says the call is
   * simulated either way, so nobody watching can mistake it for a real one.
   *
   * Both timers live here, in the effect the incident tears down, so an open
   * incident silences the demo without a second piece of state deciding that.
   */
  useEffect(() => {
    // Fake mode runs the choreography by default; a live console runs it only
    // when it was launched with the flag. Read once, at module scope, so a
    // runtime value can never turn it on.
    const choreographed = status?.mode === 'fake' || demoDispatchRequested();
    const demo = !incident && choreographed && !autoCallOff && !dispatchedRef.current;

    // Runs in both modes, slower during an incident. A pass is one HTTP request
    // that returns before it resolves; the brief arrives on its own SSE stream
    // and neither waits on the other.
    const runPassIfIdle = () => {
      // Skipped rather than queued: a pass still running means the work this
      // tick would have done is already happening.
      const now = demoRef.current;
      if (!now.passRunning && !now.busy) void now.runPass();
    };
    const passes = setInterval(
      runPassIfIdle,
      incident ? AUTO_PASS_INCIDENT_MS : AUTO_PASS_MS,
    );

    /**
     * The choreography's first pass, shortly after load rather than a full
     * interval later.
     *
     * `setInterval` alone left the console sitting still for the first
     * twenty-five seconds with every slow-loop agent reading idle -- which is
     * when somebody is looking at it hardest, and is the one moment where
     * "nothing is happening yet" is indistinguishable from "this is broken".
     *
     * A lead-in rather than an immediate call, and only under the
     * choreography. Firing on mount seizes the operator's own "run a pass"
     * button before the page has finished arriving, and an unattended live
     * console should not start a multi-minute job merely because someone opened
     * it. Three seconds is long enough for the fleet's first audit read to land
     * and the page to settle, and short enough that the loop is visibly running
     * before anyone wonders whether it is.
     */
    const lead = choreographed && !incident ? setTimeout(runPassIfIdle, FIRST_PASS_MS) : undefined;

    const stopTimers = () => {
      clearInterval(passes);
      if (lead !== undefined) clearTimeout(lead);
    };

    if (incident) {
      setCallIn(null);
      return stopTimers;
    }
    if (!demo) {
      return stopTimers;
    }

    const warn = setTimeout(
      () => setCallIn(Math.round(CALL_WARNING_MS / 1000)),
      Math.max(0, AUTO_CALL_MS - CALL_WARNING_MS),
    );
    const tick = setInterval(
      () => setCallIn((left) => (left === null || left <= 0 ? left : left - 1)),
      1000,
    );
    // Retried, not fired once.
    //
    // This used to be a single `setTimeout`: at the appointed second it read
    // the top of the survey queue and, if the queue had not loaded yet, simply
    // returned. Nothing rescheduled it, so the demo never dispatched and
    // nothing on screen said why. In fake mode the queue is always there by
    // then and the bug was invisible; against a live backend -- where the
    // district read competes with a slow-loop pass -- it is the ordinary case.
    //
    // So it waits for the queue instead of assuming it. The countdown still
    // starts on time; the call is placed on the first tick that has an address
    // to place it against.
    let callTimer: ReturnType<typeof setTimeout>;
    const placeCall = () => {
      const top = demoRef.current.queue?.entries?.[0]?.address_id;
      if (!top) {
        // No queue yet. Look again shortly rather than giving up silently.
        callTimer = setTimeout(placeCall, CALL_RETRY_MS);
        return;
      }
      dispatchedRef.current = true;
      setCallIn(null);
      const sample = SAMPLE_CALLS[0];
      if (!sample) return;
      // The recording travels *through* `dispatch`, not around it.
      //
      // Setting it here as well and letting the call below run without it is
      // how the overlay came up silent: `dispatch` owns this state and cleared
      // it back to null a line later, so the panel rendered with no player at
      // all and there was nothing on screen to press.
      //
      // Not awaited, deliberately: the overlay and the dispatch start together,
      // and the brief lands while the recording plays.
      void demoRef.current.dispatch(top, sample.text, sample.channel, sample.audioSrc);
    };
    callTimer = setTimeout(placeCall, AUTO_CALL_MS);

    return () => {
      stopTimers();
      clearInterval(tick);
      clearTimeout(warn);
      clearTimeout(callTimer);
    };
    // Deliberately narrow. Everything the timers *read* -- the queue, whether a
    // pass is in flight, the callbacks -- goes through a ref, because this
    // effect must not re-subscribe when those change. It did once: with
    // `passRunning` and `queue` in the list, the seven-second refresh tore the
    // timers down and rebuilt them every tick, so the countdown to the call
    // restarted before it could ever finish and the demo never dispatched.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incident, status?.mode, autoCallOff]);


  /** Draft a referral from a conflict. The agent stops here, by design. */
  const stageReferral = useCallback(
    async (conflictId: string) => {
      setInFlight('draft-referral');
      setNotice(null);
      const result = await browserPost<{ referral_id: string; status: string }>(
        `/api/v1/conflicts/${conflictId}/referral`,
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Could not draft a referral: ${result.error.message}`);
        return;
      }
      setStaged((current) => [
        ...current.filter((r) => r.referral_id !== result.data.referral_id),
        {
          referral_id: result.data.referral_id,
          status: result.data.status,
          case_number: null,
          conflict_id: conflictId,
        },
      ]);
      setNotice('Referral drafted and staged. A captain files it.');
    },
    [],
  );

  /** The one human tap. This is the step an agent is not allowed to take. */
  const approveReferral = useCallback(
    async (referralId: string) => {
      setInFlight('file-referral');
      setNotice(null);
      const result = await browserPost<{ case_number?: string }>(
        `/api/v1/referrals/${referralId}/approve`,
        { approved_by: 'captain' },
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Could not file the referral: ${result.error.message}`);
        return;
      }
      const caseNumber = result.data?.case_number;
      // Filed referrals come back on the profile, so drop the staged copy.
      setStaged((current) => current.filter((r) => r.referral_id !== referralId));
      setNotice(
        caseNumber
          ? `Referral filed. The building department returned case ${caseNumber}.`
          : 'Referral filed.',
      );
      if (selected) await openProfile(selected);
    },
    [openProfile, selected],
  );

  const resolve = useCallback(
    async (submission: ResolutionSubmission) => {
      if (!incident) return;
      setInFlight('resolve');
      const result = await browserPost<ResolutionResponse>(
        `/api/v1/incidents/${incident.incident_id}/resolutions`,
        {
          conflict_id: submission.conflictId,
          observed_value: submission.observedValue,
          resolved_by: submission.resolvedBy,
          note: submission.note,
        },
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Could not record the observation: ${result.error.message}`);
        return;
      }
      setNotice(
        `Recorded. Profile is now version ${result.data.profile_version}; brief amended to version ${result.data.brief_version}.`,
      );
      await openProfile(incident.address_id);
    },
    [incident, openProfile],
  );

  const requestResource = useCallback(
    async (kindId: string) => {
      if (!incident) return;
      setInFlight('notify');
      const result = await browserPost<ResourceOutcomeView>(
        `/api/v1/incidents/${incident.incident_id}/resources`,
        { kind_id: kindId },
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Request refused: ${result.error.message}`);
        return;
      }
      setOutcomes((current) => [...current.filter((o) => o.kind_id !== kindId), result.data]);
    },
    [incident],
  );

  const approve = useCallback(
    async (approvalId: string) => {
      if (!incident) return;
      setInFlight('approve');
      const result = await browserPost<Record<string, unknown>>(
        `/api/v1/incidents/${incident.incident_id}/approvals/${approvalId}`,
      );
      setInFlight(null);
      if (!result.ok) {
        setNotice(`Approval failed: ${result.error.message}`);
        return;
      }
      setOutcomes((current) =>
        current.map((outcome) =>
          outcome.approval_id === approvalId
            ? {
                ...outcome,
                action: 'ALLOW',
                external_ref: String(result.data.external_ref ?? ''),
              }
            : outcome,
        ),
      );
    },
    [incident],
  );

  const registerThermal = useCallback(
    async (face: string) => {
      if (!incident) return;
      setInFlight('thermal');
      await browserPost(`/api/v1/incidents/${incident.incident_id}/thermal`, {
        face,
        // Recorded footage, never presented as a live flight.
        region_temps_c: [21, 24, 96],
        coverage: 0.8,
        source: 'recorded',
      });
      setInFlight(null);
      await openProfile(incident.address_id);
    },
    [incident, openProfile],
  );

  const closeIncident = useCallback(async () => {
    if (!incident) return;
    setInFlight('close');
    const result = await browserPost<CloseIncidentResponse>(
      `/api/v1/incidents/${incident.incident_id}/close`,
      { closed_by: 'bc-09' },
    );
    setInFlight(null);
    if (!result.ok) {
      setNotice(`Could not close the incident: ${result.error.message}`);
      return;
    }
    setNotice(
      `Incident closed. Grant revoked, log sealed with ${result.data.log_entries} entries.`,
    );
    sweepRef.current.stop = true;
    setIncident(null);
    setCallOnScreen(false);
    // Back to standby, updated: the resolution and the survey both landed.
    await refreshStandby();
    await openProfile(incident.address_id);
  }, [incident, openProfile, refreshStandby]);

  // ------------------------------------------------------- entry packages --

  // A new incident is a new set of packages, and a modal an officer dismissed
  // on the last fire must not stay dismissed for this one.
  useEffect(() => {
    raisedFor.current = new Set();
    setReviewing(null);
    setLegSelection(null);
  }, [incident?.incident_id]);

  /** Identity only: the object behind it is replaced on every log frame, and
      depending on that would restart the draw below on an unrelated append. */
  const awaitingId = entryPackages.awaiting?.package_id ?? null;

  /**
   * How long this package's route takes to draw itself over the model.
   *
   * Read from the same pure schedule the renderer obeys, off the *awaiting*
   * package rather than off the overlay on screen: while an older package is
   * still open the overlay is showing that one, and timing a new package's
   * card against the old package's leg count would be gating one thing on the
   * measurements of another. A refusal has no route and therefore no wait.
   *
   * Zero here means "there is nothing to watch", and the card goes up at once.
   */
  const routeDrawMs = useMemo(() => {
    const waiting = entryPackages.awaiting;
    if (!waiting || waiting.path.refused) return 0;
    return routeDrawSchedule(
      {
        entry: waiting.path.entry,
        egress: waiting.path.egress,
        highlight: null,
        drawKey: waiting.package_id,
      },
      { reducedMotion: prefersReducedMotion() },
    ).totalMs;
  }, [entryPackages.awaiting]);

  /**
   * Draw the route first, then ask.
   *
   * The package is never announced by a side channel: whether it arrived on an
   * `ENTRY_PACKAGE` entry or on the endpoint poll, it arrived as the whole
   * document, so "is one awaiting approval" is read from the package's own
   * computed `status` and the console never re-derives it. Once per package id
   * -- a dismissal is a decision, and the id is marked the moment the draw
   * *starts*, so a card can never be raised twice by a frame landing mid-walk,
   * nor by the poll and the stream both finding the same package.
   *
   * **The wait is a clock, not a callback from the renderer.** The obvious wire
   * -- have the model report that it finished drawing -- fails closed on every
   * device that has no WebGL, no measured geometry, or a GL context that died:
   * the route would never finish, and the ask would be lost with it. An
   * approval request that can be silently dropped by a graphics driver is not
   * an approval request. So both ends compute the same duration from the same
   * `routeDrawSchedule`, and the card arrives whether or not anything drew.
   */
  useEffect(() => {
    const waiting = entryPackages.awaiting;
    if (!waiting || raisedFor.current.has(waiting.package_id)) return;
    raisedFor.current.add(waiting.package_id);
    if (routeDrawMs <= 0) {
      setReviewing(waiting);
      return;
    }
    const timer = setTimeout(() => setReviewing(waiting), routeDrawMs);
    // Cancelled by anything that ends the draw early -- the incident closing,
    // a newer package superseding this one, the console unmounting. The card
    // is not raised late for a fire that is already out.
    return () => clearTimeout(timer);
    // `entryPackages.awaiting` is deliberately absent: its identity changes on
    // every log frame, and re-running would clear the timer of a draw that is
    // still going and leave the card unraised.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [awaitingId, routeDrawMs]);

  /** The package the modal is on, kept current as approvals land. */
  const reviewingLive = reviewing
    ? (entryPackages.packages.find((held) => held.package_id === reviewing.package_id) ??
      reviewing)
    : null;

  /**
   * The route drawn over the massing model, or nothing.
   *
   * **Gated on a package existing, never on an incident being open.** A route
   * on screen from the moment the call lands says nothing; this drawing is the
   * *outcome* of the interceptor deciding the record was good enough to compute
   * one, and it has to arrive at that moment to mean that. A refused plan draws
   * nothing either -- there is no fallback route in the backend and inventing
   * one here would be drawing the thing the refusal withheld.
   */
  const routeOverlay = useMemo<RouteOverlay | null>(() => {
    const shown = reviewingLive ?? entryPackages.packages[entryPackages.packages.length - 1];
    if (!shown || shown.path.refused) return null;
    if (!shown.path.entry && !shown.path.egress) return null;
    return {
      entry: shown.path.entry,
      egress: shown.path.egress,
      highlight: legSelection,
      // The package id is the route's identity, so the model restarts the walk
      // when a different package's route arrives and keeps walking when this
      // memo simply recomputes under a hover.
      drawKey: shown.package_id,
    };
  }, [reviewingLive, entryPackages.packages, legSelection]);

  /**
   * The send landed: resolve the incident and hand the screen back to standby.
   *
   * The teardown is `closeIncident`, unchanged -- the same call the banner's
   * close control makes, revoking the grant and sealing the log. This adds the
   * sheet over the top of it and a floor under how long the sheet is up, so the
   * transition is visible even when the close returns in 40ms. It is not a
   * separate notion of "resolved": nothing is claimed here that the close
   * response did not.
   */
  const resolveAfterDispatch = useCallback(
    async (sent: EntryPackageView) => {
      setReviewing(null);
      setLegSelection(null);
      setResolving(sent.package_id);
      const floor = new Promise<void>((done) => {
        setTimeout(done, RESOLVE_TRANSITION_MS);
      });
      await closeIncident();
      await floor;
      setResolving(null);
    },
    [closeIncident],
  );

  const railAgents = agents.length > 0 ? agents : agentList;

  /**
   * One flank per loop, whole.
   *
   * These used to be four halves: the slow loop cut down the middle across both
   * standby flanks, the incident loop cut the same way during an incident. That
   * was a layout answer to a panel that drew nine full cards, and it stopped
   * being one when the panel became rows and a single pane -- two panels would
   * mean two panes, each with its own selection, for one fleet.
   *
   * It also closes a gap the split left open. `FleetPanel` builds its
   * attribution set from the agents it is handed, so a column holding half the
   * fleet knew half the fleet, and one agent's work on a shared write target
   * could surface inside another's reasoning box. A whole loop cannot.
   */
  const slowFleet = useMemo(
    () => railAgents.filter((agent) => agent.loop === 'SLOW'),
    [railAgents],
  );
  const incidentFleet = useMemo(
    () => railAgents.filter((agent) => agent.loop === 'INCIDENT'),
    [railAgents],
  );

  /**
   * How many of each loop are actually scheduled.
   *
   * Superseded agents are listed -- a brief recorded two years ago names the
   * version that produced it -- but they are not running, and counting them
   * puts a number on the heading that the rows below it contradict.
   */
  const slowRunning = slowFleet.filter((agent) => !agent.deprecated_at).length;
  const incidentRunning = incidentFleet.filter((agent) => !agent.deprecated_at).length;

  /**
   * The fleet panel's contract.
   *
   * `events` and `decisions` are the audit streams the deleted audit console
   * used to render; the rail turns them into per-agent reasoning boxes,
   * filtered by `actor`. `incident` lets the rail decide its own incident
   * presentation -- the layout no longer compresses it from outside, because
   * the fleet is never squeezed into a strip: it is two flanking columns of
   * full cards in both modes.
   *
   * `agents` here is the whole catalog, and it is the default a column gets.
   * `loop` -- and, in standby, an `agents` subset -- is added per column at the
   * call site, never here.
   *
   * One consequence of the standby split is worth naming: `FleetPanel` builds
   * its `fleetIds` set from whatever `agents` it was handed, and that set is
   * what stops one agent's work on a shared write target from appearing in
   * another's reasoning box. Handed half the fleet, a column knows about half
   * the fleet. Closing that properly means `FleetPanel` taking the full roster
   * separately from the agents it draws, which is the fleet package's call to
   * make, not the layout's.
   *
   * `geometry` and `sources` have no other path into the panel: the console is
   * the only holder of the structure currently on screen and of the district's
   * source health, and the rail's massing glyph and coverage read come out of
   * them.
   *
   * Spread rather than written out attribute by attribute: the props are being
   * added to `AgentRail` right now, and a spread passes the extra ones through
   * without the layout having to land in the same commit as the rail.
   */
  const railProps = {
    agents: railAgents,
    subscriptions,
    events,
    decisions,
    // The session floor, shared by both columns. Every counter and every
    // terminal line in either loop is measured from it -- see `since` above and
    // `sessionFloor` for why it is the backend's instant and not this tablet's.
    since,
    incident,
    geometry,
    sources: stats?.sources ?? [],
  };

  /**
   * A fleet column. Two of them in either mode: during an incident the same
   * panel asked for a different loop each time, and in standby the same loop
   * handed a different half of it.
   *
   * The panel still decides which agents match a *loop* -- `loop` is passed,
   * never re-filtered here, because a filter written twice is how two answers
   * drift apart. `columnAgents` is the one thing the panel cannot decide for
   * itself: two columns of the same loop are indistinguishable from inside it.
   */
  const fleetRegion = ({
    id,
    heading,
    note,
    loop,
    className,
    columnAgents,
    control,
    emptyNote,
    srHeading,
    subheading,
  }: {
    id: string;
    heading: string;
    /**
     * What this loop does, in words nobody has to read the repository to
     * understand. "Slow loop" is the architecture and it keeps its name -- the
     * two-loop split is the thesis -- but an inspector reading it cold learns
     * nothing from it, and this screen has to work for both.
     */
    subheading?: string;
    note?: string;
    loop?: 'SLOW' | 'INCIDENT';
    className: string;
    /** An explicit subset, for a column that is half of one loop. */
    columnAgents?: AgentDescriptorView[];
    /**
     * A control that belongs to this loop, pinned under the heading.
     *
     * The only one so far is the hand-run slow-loop pass. It is here rather
     * than in the middle of the screen because what it acts on is the loop
     * this column lists: a button that reruns the fleet reads as part of the
     * fleet, and the middle is for what the fleet found.
     */
    control?: ReactNode;
    /**
     * What to say when this column's subset is empty.
     *
     * Without it the panel would fall through to its own empty state -- "the
     * registry reported an empty catalog" -- which is true of the catalog and
     * false of this column when the other one is holding every agent there is.
     */
    emptyNote?: string;
    /**
     * The heading a screen reader gets when `heading` is blank.
     *
     * The second column of a loop is a continuation, not a second thing, so
     * repeating "Fleet — slow loop, continued" over it made the *layout* the
     * subject of the largest label on that half of the page. Sighted readers
     * get one heading per loop; the region still has to be named for anyone
     * navigating by landmark, so the name moves to `sr-only` rather than
     * disappearing.
     */
    srHeading?: string;
  }) => (
    <section aria-labelledby={id} className={className}>
      <div className="flex shrink-0 flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 pb-2 pt-3">
        <h2
          id={id}
          className={heading ? 'text-label uppercase text-muted' : 'sr-only'}
        >
          {heading || srHeading}
        </h2>
        {note && <span className="text-label uppercase text-muted">{note}</span>}
        {subheading && (
          <p className="w-full text-micro normal-case text-muted">{subheading}</p>
        )}
      </div>
      {control}
      <div className="px-4 pb-4 pt-2 lg:min-h-0 lg:flex-1 lg:overflow-y-auto">
        {columnAgents && columnAgents.length === 0 && emptyNote ? (
          <p className="border border-dashed border-line p-4 text-body text-muted">
            {emptyNote}
          </p>
        ) : (
          <AgentRail
            {...railProps}
            {...(columnAgents
              ? // Half the roster to draw, the whole roster to attribute against.
                // A column that only knew its own half would claim the other
                // half's writes on any shared target.
                { agents: columnAgents, fleetRoster: railProps.agents }
              : {})}
            {...(loop ? { loop } : {})}
          />
        )}
      </div>
    </section>
  );

  /** The computed structure. Right of the fleet in either mode -- it has a
      place both ways, but only once a structure has been selected does it have
      anything to draw. */
  const structurePanel = (
    <section aria-labelledby="structure-heading" className="min-w-0 bg-ground p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 id="structure-heading" className="text-micro uppercase tracking-widest text-muted">
          Structure
        </h2>
        {/* One model now, not a pair of competing ones. The photorealistic
            view moved to the imagery panel, where the other Google-sourced
            pictures of this building already live -- it answers "what does it
            look like", which is that panel's question. This panel answers
            "what do the records say it is", and the camera angles are its
            own. */}
        <div className="flex flex-wrap gap-1" role="group" aria-label="Fixed camera views">
          {VIEWS.map((angle) => (
            <button
              key={angle}
              type="button"
              aria-pressed={view === angle}
              onClick={() => setView(angle)}
              className={`border px-2 py-0.5 text-micro uppercase tracking-wide focus-visible:outline focus-visible:outline-2 focus-visible:outline-live ${
                view === angle ? 'border-live text-live' : 'border-line text-muted'
              }`}
            >
              {angle}
            </button>
          ))}
        </div>
      </div>
      {/* Why the sweep stopped, when it stopped early. Nothing while it is
          flying: a progress line for something visibly progressing is noise,
          and the faces filling in on the model are the progress. */}
      {sweepNotice && <p className="mb-2 text-body text-muted">{sweepNotice}</p>}
      {/* `route` is null until the interceptor has composed a package. The
          path appearing over the model is the visible outcome of that
          decision, and a route drawn before one would be a picture of
          nothing the fleet had committed to. */}
      <StructureModel
        geometry={geometry}
        view={view}
        forceFallback={forceSvgGeometry}
        route={routeOverlay}
        geometryState={geometryState}
      />
    </section>
  );

  /** The real structure, beside the computed one. Incident only: outside one
      there is no address to photograph, and a panel explaining that in a
      paragraph was a paragraph explaining an empty box. */
  /**
   * The building, from the kerb and from above.
   *
   * Two views of one provider rather than two panels: the aerial is what a
   * commander wants on the way in -- roof shape, what is standing on it, how
   * close the exposure next door is -- and the street view is what they check
   * a storey count and a barred window against on arrival. Both open only
   * during an incident, because both cost a metered request per address and
   * standby opens buildings by the dozen.
   */
  const imageryPanel = (
    <section aria-labelledby="imagery-heading" className="min-w-0 bg-ground p-4">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="imagery-heading" className="text-micro uppercase tracking-widest text-muted">
          Building imagery
        </h2>
        {/* Three viewpoints from one provider. `3d` is Google's Photorealistic
            3D Tiles and it belongs here rather than beside the structure model:
            all three answer "what does this building look like", and the panel
            next door answers the different question of what the records say it
            is. Putting them in one place also stops the console implying that
            photogrammetry and a derived massing are two takes on one claim. */}
        <div className="flex gap-1" role="group" aria-label="Imagery viewpoint">
          {(['street', 'aerial', '3d'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={imageryView === option}
              onClick={() => setImageryView(option)}
              data-testid={`imagery-view-${option}`}
              className={`border px-2 py-0.5 text-micro uppercase tracking-wide focus-visible:outline focus-visible:outline-2 focus-visible:outline-live ${
                imageryView === option
                  ? 'border-live text-live'
                  : 'border-line text-muted hover:text-ink'
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>
      {imageryView === '3d' ? (
        <PhotorealisticModel
          latitude={geometry?.latitude ?? null}
          longitude={geometry?.longitude ?? null}
          label={incident?.address_id ?? selected ?? 'the selected structure'}
          geometryState={geometryState}
        />
      ) : (
        <BuildingImagery addressId={incident?.address_id ?? null} view={imageryView} />
      )}
    </section>
  );

  const profileSection = profile && (
    <section aria-labelledby="profile-heading" className="min-w-0 bg-ground p-4">
      <h2 id="profile-heading" className="font-mono text-ink">
        {profile.address_id}
        <span className="ml-2 text-micro text-muted">profile v{profile.profile_version}</span>
      </h2>

      <div className="mt-3 grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <AttributeGrid facts={profile.facts} unknownKeys={profile.unknown_keys} />
        </div>
        <div className="space-y-4">
          <ConflictPanel
            conflicts={profile.conflicts}
            // `?? []`, because a profile without the field is a crash here.
            // The spread threw `open_referrals is not iterable` the first time
            // a test rendered the incident view against a profile that had not
            // been written back with one -- and a console that dies on an
            // absent optional field would take the whole incident view with it
            // on a fireground, over a referral nobody had filed yet.
            referrals={[...(profile.open_referrals ?? []), ...staged]}
            onResolve={resolve}
            onStageReferral={stageReferral}
            onApproveReferral={approveReferral}
            busy={busy}
            disabledReason={
              incident
                ? undefined
                : 'An observation is recorded during an incident 360. Open an incident to settle this on scene.'
            }
          />
          <div>
            <h3 className="text-micro uppercase tracking-widest text-muted">Timeline</h3>
            <div className="mt-2">
              <Timeline events={timeline} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );

  /**
   * The slow loop, run by hand.
   *
   * This control used to travel with the survey ranking, on the grounds that
   * the queue was the thing a pass re-ranked. The ranking is off this screen
   * now and the pass is not: it is the one button that makes the district
   * move, so it sits at the head of the column that owns the loop it runs
   * rather than in a strip of its own in the middle.
   *
   * The pass still ranks -- `structure-watch` reads the district and scores it
   * whether or not anything draws the result -- so the completion notice keeps
   * reporting what the backend did rather than only the part that shows.
   */
  const slowLoopControl = (
    <div className="shrink-0 px-4 py-2">
      <button
        type="button"
        onClick={() => void runSlowLoopPass()}
        disabled={passRunning}
        aria-busy={passRunning}
        data-testid="run-slow-loop-pass"
        title="Polls every source, writes the facts it finds, and detects conflicts."
        className="w-full border border-line px-3 py-2 text-label uppercase tracking-wide text-ink hover:border-live disabled:cursor-progress disabled:border-line disabled:text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
      >
        {passRunning ? 'Running a slow-loop pass…' : 'Run a slow-loop pass'}
      </button>
      <p
        role="status"
        aria-atomic="true"
        data-testid="slow-loop-pass-status"
        className={`mt-1 text-micro ${passNotice?.startsWith('Slow-loop pass failed') ? 'text-alarm' : 'text-muted'}`}
      >
        {passRunning
          ? `Slow-loop pass running: sources, facts, conflicts, ranking.${
              passElapsed === null ? '' : ` ${passElapsed}s elapsed.`
            }${
              // Said once, past the point where a viewer starts wondering
              // whether it has hung. It has not: a live pass is minutes.
              (passElapsed ?? 0) >= 20 ? ' A live district takes minutes; nothing is stuck.' : ''
            }`
          : passNotice}
      </p>
    </div>
  );

  /**
   * A live deployment without delegated Workspace authority records the
   * calendar and mail writes and sends neither. That has to stay on screen
   * somewhere: the work order, the referral and the pre-plan really do execute,
   * and a crew notification sitting beside them looking identical is the
   * console asserting a notification nobody received. It lives in the
   * disclosure line now rather than in a header chip.
   */
  const simulatedWorkspaceWrites = status?.mode === 'live' && status.workspace_writes === 'fake';

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-ground text-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:border focus:border-live focus:bg-surface focus:px-3 focus:py-1"
      >
        Skip to main content
      </a>

      <header className="flex shrink-0 flex-wrap items-baseline justify-between gap-3 border-b border-line bg-surface px-4 py-2">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-widest text-ink">TERSAGE</h1>
          <span className="text-micro uppercase tracking-wide text-muted">Command Center</span>
        </div>
        {/* The one place a write says what it is. It has to be a lookup on the
            named action and not a fixed word: the shared flag used to be put
            into words only by the banner's close control, which meant every
            write on the screen -- a notification, an approval, a referral --
            was reported to the officer as the incident being closed. Rendered
            only while something is running, so it never leaves a stale verb
            sitting beside the backend signal. */}
        <div className="flex items-baseline gap-3">
          {inFlight && (
            <span
              role="status"
              aria-atomic="true"
              data-testid="in-flight-status"
              className="font-mono text-micro uppercase tracking-wide text-muted"
            >
              {IN_FLIGHT_LABEL[inFlight]}
            </span>
          )}
          <BackendSignal initial={readiness} statusMissing={status === null} />
        </div>
      </header>

      {incident && (
        <IncidentBanner
          incidentId={incident.incident_id}
          addressId={incident.address_id}
          addressDisplay={incident.address_display}
          alarmLevel={2}
          dispatchedAt={incident.dispatched_at}
          coldStart={incident.cold_start}
          onClose={closeIncident}
          // The close control, not a general busy light. `closing` is the only
          // input the banner has and it drives the word on the button, so
          // handing it the shared flag made the button read "Closing…" through
          // a resource request the officer had just fired -- a console
          // announcing an action nobody took. It costs the button its disabled
          // state during another write, which the backend refuses on its own
          // anyway; a wrong verb on the incident header does not get refused
          // by anything.
          closing={inFlight === 'close'}
          // Still disabled while any other write runs, which `closing` used to
          // do as a side effect of being the only flag. Splitting the word from
          // the disabled state fixed the wrong verb; passing this keeps the
          // guard the single flag was also providing.
          busy={busy}
        />
      )}

      {/* Stage and amendment announcements. Polite: it must not interrupt. */}
      <p aria-live="polite" aria-atomic="true" className="sr-only" data-testid="brief-announcer">
        {announcement}
      </p>
      {notice && (
        <p
          role="status"
          className="shrink-0 border-b border-line bg-raised px-4 py-2 text-micro text-ink"
        >
          {notice}
        </p>
      )}
      {error && (
        <p
          role="alert"
          className="shrink-0 border-b border-alarm bg-raised px-4 py-2 text-micro text-alarm"
        >
          {error}
        </p>
      )}
      {stream.state === 'reconnecting' && (
        <p
          role="status"
          className="shrink-0 border-b border-line bg-raised px-4 py-1 text-micro text-disputed"
        >
          Stream reconnecting. Versions already received stay on screen; missed
          ones replay from the log.
        </p>
      )}

      {/* The call, over everything, while the console keeps working behind it.
          Rendered here rather than inside a column so it covers the screen --
          and it covers a screen that is already showing the brief. */}
      {incident && (
        <IncomingCall
          open={callOnScreen}
          addressId={incident.address_id}
          transcript={narrative}
          audioSrc={callAudioSrc}
          channel={incident.intake?.channel === 'CAD_NARRATIVE' ? 'CAD_NARRATIVE' : 'CALL_911'}
          onDismiss={() => setCallOnScreen(false)}
        />
      )}

      {callIn !== null && !incident && (
        /* A demo affordance, not a claim about data -- worded so nobody reads
           it as a real dispatch already in progress. Polite, not assertive: it
           should not interrupt a screen reader mid-sentence. */
        <div
          role="status"
          aria-live="polite"
          className="shrink-0 border-b border-disputed bg-raised px-4 py-2 text-micro text-disputed"
        >
          <span className="font-mono">
            Simulated 911 call arriving in {callIn}s — the console will switch to the
            incident view.
          </span>
          <button
            type="button"
            onClick={() => {
              setAutoCallOff(true);
              setCallIn(null);
            }}
            className="ml-3 underline underline-offset-4 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
          >
            Stay in standby
          </button>
        </div>
      )}

      <main id="main" className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:overflow-hidden">
        {/* The district bar, above the mode switch because it is true in both
            modes. It does not scroll with whatever is under it. */}
        <div className="shrink-0 border-b border-line bg-surface px-3 py-2">
          <DistrictStrip stats={stats} />
        </div>

        {!incident && (
          /* Standby: the same three columns an incident uses, so the screen
             does not change shape under an officer at the moment a fire
             starts. The slow loop holds the left. The middle is the subject --
             in standby that is the region, drawn; on dispatch it becomes the
             building. The right holds the findings: what is burning out there,
             and which structures' records disagree, one card each.

             Stacks below `lg`, where three columns is one unreadable column,
             and the map keeps a floor height so it does not collapse to a
             sliver above the cards. */
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-px bg-line lg:gap-x-4 lg:bg-ground lg:p-2 lg:grid-cols-[clamp(300px,21vw,380px)_minmax(0,1fr)_clamp(300px,23vw,400px)] lg:overflow-hidden">
            {fleetRegion({
              id: 'standby-fleet-heading',
              heading: 'Slow loop',
              subheading: 'Watches records between fires.',
              note: `${slowRunning} agents`,
              loop: 'SLOW',
              columnAgents: slowFleet,
              control: slowLoopControl,
              className:
                'flex min-w-0 flex-col bg-surface lg:min-h-0 lg:rounded-lg lg:border lg:border-line lg:overflow-hidden',
            })}

            {/* The middle: the region as a picture. The heat map holds the top
                whether or not a structure is selected, because "what is
                burning around us" does not stop being true when somebody opens
                a building -- and the panel below it is the structure they
                opened, which is what the middle column becomes on dispatch. */}
            <div className="flex min-w-0 flex-col gap-px bg-line lg:min-h-0 lg:gap-3 lg:bg-transparent lg:overflow-y-auto">
              {/* Tall on purpose. The region is close to square and the column
                  is wide, so a short frame makes `fitBounds` fit by height and
                  leaves the map a stamp in the middle of empty space. This is
                  the subject of the standby screen and it is sized like it. */}
              <div className="flex min-h-[420px] flex-col bg-ground lg:min-h-[620px] lg:flex-1 lg:rounded-lg lg:border lg:border-line lg:overflow-hidden">
                <RegionalHeatMap
                  activity={fireActivity}
                  error={fireActivityError}
                  basemap={basemap}
                />
              </div>

              {profile && (
                /* `shrink-0`, or the map eats it.
                   The heat map above is `flex-1` and grows to fill the column.
                   Without a shrink guard flexbox squeezed this panel down to
                   its borders -- two pixels -- so opening a structure rendered
                   the whole profile, correctly, at no height at all. The click
                   did everything except produce anything to look at, which is
                   indistinguishable from a dead button. */
                <div
                  ref={profileRef}
                  className="flex shrink-0 scroll-mt-2 flex-col gap-px bg-line lg:rounded-lg lg:border lg:border-line lg:overflow-hidden"
                >
                  <div className="grid shrink-0 grid-cols-1 gap-px bg-line lg:grid-cols-[7fr_3fr]">
                    {structurePanel}
                    <div className="min-w-0 bg-ground p-4">
                      <DispatchPanel
                        addressId={profile.address_id}
                        busy={busy}
                        onDispatch={dispatch}
                      />
                    </div>
                  </div>
                  {profileSection}
                </div>
              )}
            </div>

            {/* The right: what the fleet found, one card per question. */}
            <div className="flex min-w-0 flex-col gap-px bg-line lg:min-h-0 lg:gap-3 lg:bg-transparent lg:overflow-y-auto">
              <PanelCard
                id="standby-fire-activity-heading"
                heading="Regional fire activity"
                subheading="Satellite thermal detections and recent fire weather."
                note={
                  fireActivity?.regionalCount === null || fireActivity?.regionalCount === undefined
                    ? undefined
                    : `${fireActivity.regionalCount} detections`
                }
                className="shrink-0"
              >
                <FireActivityMap activity={fireActivity} error={fireActivityError} headless />
              </PanelCard>

              <PanelCard
                id="standby-records-disagree-heading"
                heading="Records disagree"
                subheading="Structures whose paperwork and measurement do not match."
                note={
                  stats?.open_conflicts === null || stats?.open_conflicts === undefined
                    ? undefined
                    : `${stats.open_conflicts} open`
                }
                className="lg:min-h-0"
                bodyClassName="lg:overflow-y-auto"
              >
                <RecordsDisagree
                  entries={queue?.entries ?? []}
                  openConflicts={stats?.open_conflicts ?? null}
                  selectedAddressId={selected}
                  onSelect={openProfile}
                  headless
                />
              </PanelCard>
            </div>

          </div>
        )}

        {incident && (
          /* Incident: three columns. The agents acting right now on the left,
             the building in the middle -- the computed structure beside the
             photograph of it -- and the brief down the right, where it is read
             top to bottom without competing with the model for the width. The
             slow loop leaves the screen and says so in a line of its own below
             -- it did not stop because a fire started, and an officer should
             not have to assume either way.

             The brief comes second in the source and third on screen. Stacked
             on a narrow tablet the source order is the reading order, and the
             brief is what the first ninety seconds are for: it belongs above
             the profile timeline and the resource panel, not under them. The
             two explicit column starts put it back on the right where there is
             room for three. */
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-px bg-line lg:gap-x-4 lg:bg-ground lg:p-2 lg:grid-cols-[clamp(288px,19vw,380px)_minmax(0,1fr)_clamp(288px,19vw,380px)] lg:overflow-hidden">
            {fleetRegion({
              id: 'incident-fleet-heading',
              heading: 'Incident loop',
              note: `${incidentRunning} acting now`,
              loop: 'INCIDENT',
              columnAgents: incidentFleet,
              className:
                'flex min-w-0 flex-col bg-surface lg:min-h-0 lg:rounded-lg lg:border lg:border-line lg:overflow-hidden',
            })}

            {/* The brief, in a column of its own that scrolls on its own. It
                used to run the full width of the middle under the model, which
                meant a three-stage brief pushed the building off the top of the
                screen as it filled in. */}
            <div className="flex min-w-0 flex-col gap-px bg-line lg:col-start-3 lg:row-start-1 lg:min-h-0 lg:rounded-lg lg:border lg:border-line lg:bg-surface lg:overflow-y-auto">
              <section
                aria-labelledby="brief-heading"
                className="min-w-0 flex-1 bg-surface p-4"
              >
                <h2 id="brief-heading" className="sr-only">
                  Incident brief
                </h2>
                {/* What the loop is doing about the entry package, while there
                    is not one yet. Until this existed, a loop that was working,
                    a loop whose composition had been cancelled mid-run, and a
                    loop with autonomy off were all the same empty screen -- and
                    that ambiguity is exactly what made the same live failure
                    take several rounds to find. */}
                <EntryPackageWatch
                  incidentId={incident.incident_id}
                  hasPackage={entryPackages.packages.length > 0}
                />
                {/* The call sits above the brief, not under it.
                    It was below the whole three-stage brief and rendered some
                    24,000px down a scrolling column -- playing to nobody, which
                    is the same as not playing. The recording is the first thing
                    that happens in an incident and it belongs where an officer
                    is already looking.

                    **It does not autoplay, and must not start doing so.** The
                    overlay above it plays the call as it arrives -- `dispatch`
                    opens that overlay for every call that carries a recording,
                    so this player is mounted at the same moment and with the
                    same file. Both asked to play, and the same twenty-three
                    seconds ran twice over itself a fraction of a second apart:
                    two voices disagreeing about which floor the fire is on.
                    The overlay owns the first playback because it is the thing
                    the dispatch opens. This one keeps its controls and waits to
                    be pressed, which is what it is for -- hearing the call
                    again, after. */}
                {incident.intake && (
                  <div className="mb-4">
                    <CallAudio
                      src={callAudioSrc}
                      label={
                        incident.intake.channel === 'CALL_911' ? '911 call' : 'CAD narrative'
                      }
                    />
                  </div>
                )}
                {/* The fleet at work, above the brief it is producing. Every
                    action arrives as its own message as the log is written,
                    newest at the top, and the stream scrolls inside its own
                    bounded height -- so the column answers both "what is
                    happening now" and "what has happened" without pushing the
                    brief off the screen. */}
                <div className="mb-4">
                  <AgentActivity entries={incidentLog.entries} />
                </div>
                <BriefPanel
                  emission={latest}
                  emissions={stream.emissions}
                  // Provisional prose is shown only while it belongs to a
                  // version the panel has not yet received persisted. Once the
                  // record has it, the record's copy is what is on screen.
                  draftNarrative={
                    prose.forVersion > (latest?.version ?? 0) || prose.writing
                      ? prose.text
                      : ''
                  }
                  writing={prose.writing}
                />
                {incident.intake && (
                  <div className="mt-4">
                    {/* What was read out of the call, under the brief it
                        amended. The audio is never an input to either. */}
                    <IntakePanel intake={incident.intake} narrative={narrative} />
                  </div>
                )}
              </section>
            </div>

            <div className="flex min-w-0 flex-col gap-px bg-line lg:col-start-2 lg:row-start-1 lg:min-h-0 lg:overflow-y-auto">
              {/* The computed structure and the real one, side by side, in a
                  column narrower than the screen. They stack below `xl`: two
                  panels in a third of a 1280 display is neither a model nor a
                  photograph. */}
              <div className="grid shrink-0 grid-cols-1 gap-px bg-line xl:grid-cols-[3fr_2fr]">
                {structurePanel}
                {imageryPanel}
              </div>

              <section
                aria-labelledby="conditions-heading"
                className="min-w-0 bg-ground p-4"
              >
                <h2
                  id="conditions-heading"
                  className="mb-3 text-micro uppercase tracking-widest text-muted"
                >
                  Resources and conditions
                </h2>
                {/* Two panels abreast only where the middle column is wide
                    enough to hold two. It is a third of the screen now, not
                    all of it. */}
                <div className="grid gap-4 2xl:grid-cols-2">
                  <ResourcePanel
                    outcomes={outcomes}
                    onRequest={requestResource}
                    onApprove={approve}
                    busy={busy}
                  />
                  <ThermalPanel
                    faces={geometry?.spec.faces ?? []}
                    onRegister={registerThermal}
                    busy={busy}
                  />
                </div>
                {/* Every package this incident produced, and the sheet each
                    one prints to. Beside the resources rather than inside the
                    modal: a package that was declined is still a thing that
                    happened, and it has to be reachable once the card that
                    raised it is gone. */}
                <div className="mt-4">
                  <EntryPackageList
                    incidentId={incident.incident_id}
                    packages={entryPackages.packages}
                    recoveredFromList={entryPackages.recoveredFromList}
                    onReview={(held) => setReviewing(held)}
                  />
                </div>
              </section>

              {profileSection}

              {/* The slow loop left the screen; it did not stop. Derived from
                  the real descriptor count and the real timestamp of the last
                  completed pass, so the claim is checkable. "No pass yet" is
                  said rather than rounded to zero seconds. */}
              <p
                className="shrink-0 border-t border-line bg-surface px-4 py-1.5 font-mono text-micro text-muted"
                data-testid="slow-loop-offscreen"
              >
                slow loop · {slowRunning} agents · off screen, still running ·{' '}
                {passRunning
                  ? 'pass in progress'
                  : passAt === null
                    ? 'no pass completed yet this session'
                    : `last pass ${Math.max(0, Math.round((Date.now() - passAt) / 1000))}s ago`}
              </p>
            </div>

          </div>
        )}
      </main>

      {/* The interceptor's approval card, over everything. Outside `main` so
          it is not a second region inside a landmark whose other regions are
          still tabbable behind it, and it manages its own focus. */}
      {incident && reviewingLive && (
        <EntryPackageModal
          incidentId={incident.incident_id}
          entryPackage={reviewingLive}
          autonomyTrigger={entryPackages.triggers[reviewingLive.package_id] ?? ''}
          onUpdated={(updated) => {
            entryPackages.apply(updated);
            setReviewing(updated);
          }}
          onClose={() => {
            setReviewing(null);
            setLegSelection(null);
          }}
          onDispatched={(held) => void resolveAfterDispatch(held)}
          onSelectLeg={setLegSelection}
        />
      )}

      {/* The sheet between the fireground and standby. `role="status"` rather
          than `alert`: it reports a conclusion, and it is polite because the
          officer who tapped release is the one reading it. */}
      {resolving && (
        <div
          role="status"
          data-testid="resolve-sheet"
          className="resolve-sheet fixed inset-0 z-50 flex flex-col items-center justify-center gap-2 bg-ground px-6 text-center"
        >
          <p className="font-mono text-title uppercase tracking-widest text-live">
            Package released to live dispatch units
          </p>
          <p className="max-w-xl text-body leading-6 text-muted">
            {resolving} was sent to the crew. The incident is resolving: the grant is being
            revoked and the log sealed, and the console is returning to the slow loop.
          </p>
        </div>
      )}

      <footer className="shrink-0 border-t border-line bg-surface px-4 py-1.5 text-micro leading-5 text-muted">
        {status?.disclosure ??
          'Decision-support prototype, not a certified public-safety system.'}
        {simulatedWorkspaceWrites && (
          <span className="ml-2 text-disputed">
            calendar + mail: simulated — recorded and audited, neither is sent.
          </span>
        )}
      </footer>
    </div>
  );
}
