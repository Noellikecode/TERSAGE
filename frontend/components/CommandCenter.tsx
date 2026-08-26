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

import { StructureModel, type ViewAngle } from '@/components/StructureModel';
import { PhotorealisticModel, type GeometryState } from '@/components/PhotorealisticModel';
import { BriefPanel, announcementFor } from '@/components/incident/BriefPanel';
import { BuildingImagery, type ImageryView } from '@/components/incident/BuildingImagery';
import { IntakePanel } from '@/components/incident/IntakePanel';
import { IncidentBanner } from '@/components/incident/IncidentBanner';
import { ResourcePanel } from '@/components/incident/ResourcePanel';
import { ThermalPanel } from '@/components/incident/ThermalPanel';
import { AttributeGrid } from '@/components/profile/AttributeGrid';
import { ConflictPanel, type ResolutionSubmission } from '@/components/profile/ConflictPanel';
import { Timeline } from '@/components/profile/Timeline';
import { AgentRail } from '@/components/standby/AgentRail';
import { RecordsDisagree } from '@/components/standby/RecordsDisagree';
import { DispatchPanel, SAMPLE_CALLS } from '@/components/standby/DispatchPanel';
import { DistrictStrip } from '@/components/standby/DistrictStrip';
import {
  FireActivityMap,
  normalizeFireActivity,
  type FireActivity,
} from '@/components/standby/FireActivityMap';
import { browserGet, browserPost } from '@/lib/api/client';
import { useBriefStream } from '@/lib/api/stream';
import type {
  AgentDescriptorView,
  AgentListResponse,
  AuditEventView,
  BriefEmissionView,
  BuildingProfileView,
  CloseIncidentResponse,
  DistrictStatsView,
  GeometryView,
  IntakeChannel,
  OpenIncidentResponse,
  PolicyDecisionView,
  QueueView,
  ReferralSummary,
  Readiness,
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

/** How long between walls on the drone sweep.
 *
 * Long enough that an officer sees each face arrive as its own event rather
 * than four appearing together, short enough that the building is covered
 * inside the first brief. The backend decides when the sweep is finished; this
 * only decides how fast it is asked. */
const SWEEP_INTERVAL_MS = 3500;

/** A hard ceiling on sweep requests, so a backend that never reports `complete`
 *  cannot spin this loop for ever. Four walls, one spare. */
const SWEEP_FACES = 5;

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
      request the imagery port knows how to serve. */
  const [imageryView, setImageryView] = useState<ImageryView | '3d'>('street');
  const [agents] = useState<AgentDescriptorView[]>(initialAgents);
  const [subscriptions, setSubscriptions] = useState<SubscriptionView[]>(initialSubscriptions);
  const [events, setEvents] = useState<AuditEventView[]>(initialEvents);
  const [decisions, setDecisions] = useState<PolicyDecisionView[]>(initialDecisions);
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
  const [busy, setBusy] = useState(false);
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

  /** The hand-run slow-loop pass: idle, in flight, or finished with a word. */
  const [passRunning, setPassRunning] = useState(false);
  //: When the last pass finished, for the off-screen slow-loop line. Null until
  //: one has, because "no pass yet" and "a pass just now" are different claims.
  const [passAt, setPassAt] = useState<number | null>(null);
  //: Seconds until the demo dispatches, or null when nothing is scheduled.
  const [callIn, setCallIn] = useState<number | null>(null);
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
    dispatch: async (_a: string, _n: string, _c: IntakeChannel) => {},
  });
  const [passNotice, setPassNotice] = useState<string | null>(null);

  const stream = useBriefStream(incident?.incident_id ?? null);
  const announcedRef = useRef<number>(0);

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

      const [statsResult, queueResult, eventsResult, decisionsResult] = await Promise.all([
        browserGet<DistrictStatsView>(`/api/v1/districts/${districtId}/stats`, { signal }),
        browserGet<QueueView>(`/api/v1/districts/${districtId}/queue`, { signal }),
        browserGet<AuditEventView[]>('/api/v1/internal/audit/events?limit=60', { signal }),
        browserGet<PolicyDecisionView[]>('/api/v1/internal/audit/decisions?limit=60', { signal }),
        fireIsStale ? refreshFireActivity(signal) : Promise.resolve(),
      ]);
      // An aborted request comes back `ok: false`, so a torn-down poll writes
      // no state: there is no unmount guard to forget.
      if (statsResult.ok) setStats(statsResult.data);
      if (queueResult.ok) setQueue(queueResult.data);
      if (eventsResult.ok) setEvents(eventsResult.data);
      if (decisionsResult.ok) setDecisions(decisionsResult.data);
    },
    [districtId, refreshFireActivity],
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
  const runSlowLoopPass = useCallback(async () => {
    setPassRunning(true);
    setPassNotice(null);
    const result = await browserPost<Record<string, unknown>>(
      `/api/v1/districts/${districtId}/poll`,
    );
    if (!result.ok) {
      setPassRunning(false);
      setPassNotice(`Slow-loop pass failed: ${result.error.message}`);
      return;
    }
    // Read the district back before saying the pass is done, so the counts on
    // screen are the ones the pass produced rather than the ones before it.
    await refreshStandby(undefined, { forceFireActivity: true });
    setPassRunning(false);
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
    async (addressId: string, narrative = '', channel: IntakeChannel = 'CALL_911') => {
      dispatchedRef.current = true;
      setCallIn(null);
      setBusy(true);
      setNotice(null);
      // The narrative is kept so the intake panel can check a quote against
      // the offsets it claims. Without the source text, a span is unverifiable.
      setNarrative(narrative);
      const result = await browserPost<OpenIncidentResponse>('/api/v1/incidents', {
        address: addressId,
        cad_ref: `CAD-${Date.now().toString().slice(-6)}`,
        alarm_level: 2,
        ...(narrative ? { intake_narrative: narrative, intake_channel: channel } : {}),
      });
      setBusy(false);
      if (!result.ok) {
        setNotice(`Could not open an incident: ${result.error.message}`);
        return;
      }
      setIncident(result.data);
      setOutcomes([]);
      announcedRef.current = 0;
      await openProfile(result.data.address_id);
      // Prose is asked for only after the instant brief is on screen.
      void browserPost(`/api/v1/incidents/${result.data.incident_id}/brief/enrich`);
      // And the drone goes up. Not awaited: the brief is what the first ninety
      // seconds are for, and the sweep paints onto it as each wall lands.
      void flyDroneSweep(result.data.incident_id, result.data.address_id);
    },
    [openProfile, flyDroneSweep],
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
   * **Auto-dispatch is gated on the backend calling itself fake.** `status.mode`
   * comes from `/api/v1/system/status`; anything other than the string `fake`
   * -- including a status this console has not managed to read yet -- means no
   * call is placed. Software that invented a 911 call on a real deployment
   * would be the worst thing in this repository, so the gate is a positive
   * check on a known value rather than an absence of a live flag. Do not
   * relax it into `!== 'live'`.
   *
   * Both timers live here, in the effect the incident tears down, so an open
   * incident silences the demo without a second piece of state deciding that.
   */
  useEffect(() => {
    const demo = !incident && status?.mode === 'fake' && !autoCallOff && !dispatchedRef.current;

    // Runs in both modes, slower during an incident. A pass is one HTTP request
    // that returns before it resolves; the brief arrives on its own SSE stream
    // and neither waits on the other.
    const passes = setInterval(
      () => {
        // Skipped rather than queued: a pass still running means the work this
        // tick would have done is already happening.
        const now = demoRef.current;
        if (!now.passRunning && !now.busy) void now.runPass();
      },
      incident ? AUTO_PASS_INCIDENT_MS : AUTO_PASS_MS,
    );

    if (incident) {
      setCallIn(null);
      return () => clearInterval(passes);
    }
    if (!demo) {
      return () => clearInterval(passes);
    }

    const warn = setTimeout(
      () => setCallIn(Math.round(CALL_WARNING_MS / 1000)),
      Math.max(0, AUTO_CALL_MS - CALL_WARNING_MS),
    );
    const tick = setInterval(
      () => setCallIn((left) => (left === null || left <= 0 ? left : left - 1)),
      1000,
    );
    const call = setTimeout(() => {
      const top = demoRef.current.queue?.entries?.[0]?.address_id;
      if (!top) return;
      dispatchedRef.current = true;
      setCallIn(null);
      const sample = SAMPLE_CALLS[0];
      if (!sample) return;
      void demoRef.current.dispatch(top, sample.text, sample.channel);
    }, AUTO_CALL_MS);

    return () => {
      clearInterval(passes);
      clearInterval(tick);
      clearTimeout(warn);
      clearTimeout(call);
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
      setBusy(true);
      setNotice(null);
      const result = await browserPost<{ referral_id: string; status: string }>(
        `/api/v1/conflicts/${conflictId}/referral`,
      );
      setBusy(false);
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
      setBusy(true);
      setNotice(null);
      const result = await browserPost<{ case_number?: string }>(
        `/api/v1/referrals/${referralId}/approve`,
        { approved_by: 'captain' },
      );
      setBusy(false);
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
      setBusy(true);
      const result = await browserPost<ResolutionResponse>(
        `/api/v1/incidents/${incident.incident_id}/resolutions`,
        {
          conflict_id: submission.conflictId,
          observed_value: submission.observedValue,
          resolved_by: submission.resolvedBy,
          note: submission.note,
        },
      );
      setBusy(false);
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
      setBusy(true);
      const result = await browserPost<ResourceOutcomeView>(
        `/api/v1/incidents/${incident.incident_id}/resources`,
        { kind_id: kindId },
      );
      setBusy(false);
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
      setBusy(true);
      const result = await browserPost<Record<string, unknown>>(
        `/api/v1/incidents/${incident.incident_id}/approvals/${approvalId}`,
      );
      setBusy(false);
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
      setBusy(true);
      await browserPost(`/api/v1/incidents/${incident.incident_id}/thermal`, {
        face,
        // Recorded footage, never presented as a live flight.
        region_temps_c: [21, 24, 96],
        coverage: 0.8,
        source: 'recorded',
      });
      setBusy(false);
      await openProfile(incident.address_id);
    },
    [incident, openProfile],
  );

  const closeIncident = useCallback(async () => {
    if (!incident) return;
    setBusy(true);
    const result = await browserPost<CloseIncidentResponse>(
      `/api/v1/incidents/${incident.incident_id}/close`,
      { closed_by: 'bc-09' },
    );
    setBusy(false);
    if (!result.ok) {
      setNotice(`Could not close the incident: ${result.error.message}`);
      return;
    }
    setNotice(
      `Incident closed. Grant revoked, log sealed with ${result.data.log_entries} entries.`,
    );
    sweepRef.current.stop = true;
    setIncident(null);
    // Back to standby, updated: the resolution and the survey both landed.
    await refreshStandby();
    await openProfile(incident.address_id);
  }, [incident, openProfile, refreshStandby]);

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
      <StructureModel geometry={geometry} view={view} forceFallback={forceSvgGeometry} />
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
            referrals={[...profile.open_referrals, ...staged]}
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
        {passRunning ? 'Slow-loop pass running: sources, facts, conflicts, ranking.' : passNotice}
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
        <BackendSignal initial={readiness} statusMissing={status === null} />
      </header>

      {incident && (
        <IncidentBanner
          incidentId={incident.incident_id}
          addressId={incident.address_id}
          alarmLevel={2}
          dispatchedAt={incident.dispatched_at}
          coldStart={incident.cold_start}
          onClose={closeIncident}
          closing={busy}
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
          /* Standby: the same two columns an incident uses, so the screen does
             not change shape under an officer at the moment a fire starts. The
             slow loop holds the left, wide; the rest carries the region --
             what is burning out there and what the weather is doing -- then
             the structures whose records disagree, then whichever one is open.
             Stacks below `lg`, where two columns is one unreadable column. */
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-px bg-line lg:gap-x-4 lg:bg-ground lg:p-2 lg:grid-cols-[clamp(320px,24vw,420px)_minmax(0,1fr)] lg:overflow-hidden">
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

            <div className="flex min-w-0 flex-col gap-px bg-line lg:min-h-0 lg:overflow-y-auto">
              <FireActivityMap activity={fireActivity} error={fireActivityError} />

              <RecordsDisagree
                entries={queue?.entries ?? []}
                openConflicts={stats?.open_conflicts ?? null}
                selectedAddressId={selected}
                onSelect={openProfile}
              />

              {profile && (
                <>
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
                </>
              )}
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
                <BriefPanel emission={latest} />
                {incident.intake && (
                  <div className="mt-4">
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
