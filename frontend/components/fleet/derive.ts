/**
 * What the fleet panel is allowed to say, derived from what the console has.
 *
 * Everything here is a pure function over data the command center already
 * fetched: the agent catalog, the department's subscriptions, the redacted
 * audit events, the policy decisions, and -- when one is open -- the incident.
 * There is no fleet telemetry endpoint, and inventing one on the client would
 * mean a number on a fireground screen that no record can be reconciled with.
 *
 * So the rule these helpers follow is: derive it, or say nothing. Every shape
 * below can come back empty, and the components render that emptiness as a
 * stated absence rather than a zero that looks like a measurement.
 */

import type { PillTone } from '@/components/StatusPill';
import type {
  AgentDescriptorView,
  AuditEventView,
  FaceView,
  GeometryView,
  OpenIncidentResponse,
  PolicyDecisionView,
  SourceHealthView,
} from '@/lib/api/types';

/** Carried over from the activity stream: one vocabulary for one system. */
export const EVENT_TONE: Record<string, { tone: PillTone; label: string }> = {
  injection_blocked: { tone: 'alarm', label: 'injection blocked' },
  screen_unavailable: { tone: 'disputed', label: 'screen unavailable' },
  model_output_rejected: { tone: 'disputed', label: 'model output rejected' },
  write_executed: { tone: 'confirmed', label: 'external write' },
  write_replayed: { tone: 'muted', label: 'write replayed' },
  write_compensated: { tone: 'disputed', label: 'write compensated' },
  notification_sent: { tone: 'confirmed', label: 'notification sent' },
  approval_granted: { tone: 'confirmed', label: 'approval granted' },
  grant_minted: { tone: 'muted', label: 'grant minted' },
  grant_revoked: { tone: 'muted', label: 'grant revoked' },
  emergency_exception: { tone: 'alarm', label: 'emergency exception' },
  circuit_opened: { tone: 'alarm', label: 'circuit opened' },
  circuit_closed: { tone: 'muted', label: 'circuit closed' },
  dead_lettered: { tone: 'alarm', label: 'dead lettered' },
  rms_flushed: { tone: 'muted', label: 'records flushed' },
  // What an agent did on one pass. `confirmed` because a pass that ran is the
  // ordinary good case -- the counts in its detail say whether it found
  // anything, and an empty pass is not a fault.
  agent_pass: { tone: 'confirmed', label: 'pass complete' },
  // `muted` rather than `confirmed`: a step is the ordinary tick of work in
  // progress, and a terminal where every one of a dozen lines is green reads as
  // a wall of nothing. The pass that closes them out is the green one.
  agent_step: { tone: 'muted', label: 'analyzed' },
};

export const ACTION_TONE: Record<string, PillTone> = {
  ALLOW: 'confirmed',
  DERIVE: 'muted',
  WITHHOLD_JURISDICTION: 'disputed',
  REQUIRE_APPROVAL: 'disputed',
  DENY: 'alarm',
};

/** Audit kinds that mean the agent could not reach something. */
const UNREACHABLE_KINDS = new Set(['circuit_opened', 'dead_lettered', 'screen_unavailable']);

/** Audit kinds that mean something left the department. */
const WRITE_KINDS = new Set([
  'write_executed',
  'write_replayed',
  'notification_sent',
  'rms_flushed',
]);

export function clock(at: string): string {
  return at.slice(11, 19) || at;
}

/**
 * Order two backend instants. Every timestamp comparison on this screen.
 *
 * A plain `<`, and deliberately **not** `localeCompare`. These are
 * `datetime.isoformat()` strings and Python elides the fractional part when it
 * is exactly zero, so the log holds both `...T21:03:14+00:00` and
 * `...T21:03:14.223456+00:00` -- and `localeCompare` orders those two *wrong*.
 * ICU collation gives punctuation its own weights, in which `.` sorts before
 * `+`, so it reports the sub-second instant as the earlier of the pair. Code
 * unit order gets it right, because `+` (0x2B) genuinely precedes `.` (0x2E)
 * and a fractional part therefore reads as later within its own second, which
 * is what it is.
 *
 * It only bites within a single second, and only when one side has no
 * fractional part -- which is why live mode, whose `SystemClock` has a
 * microsecond field that is essentially never zero, hides it entirely, and why
 * the `SteppingClock` demo, which steps 50 ms from a whole-second epoch and so
 * mints one every twentieth reading, is where it lives. A floor anchored on
 * such an instant silently drops the whole second of work after it.
 */
export function compareAt(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

// ------------------------------------------------------------- attribution

/**
 * Detail keys whose *values* may be rendered.
 *
 * The audit sink redacts detail on the way in -- field names and hashes, never
 * record contents -- so this is a second fence rather than the only one. It
 * exists because the terminal is the one surface on this screen that prints
 * backend strings verbatim, and a record which never held a document cannot
 * leak one only for as long as nobody prints an unfamiliar field.
 *
 * A key that is not listed still appears, by name, with no value. That keeps
 * the line honest about what was recorded without repeating it.
 */
const RENDERABLE_DETAIL_KEYS = new Set([
  'action_id',
  'address_id',
  'agent_ref',
  'approval_id',
  'approved',
  'attempted',
  'conflict_id',
  'decision_id',
  'error_type',
  'external_ref',
  'flushed',
  'kind_id',
  'patterns',
  'record_ref',
  'referral_id',
  'replayed',
  'rule_id',
  'screen',
  'source_id',
  'threshold',
]);

/** Long values are not identifiers, whatever their key says. */
const MAX_DETAIL_VALUE = 48;

export function detailText(detail: Record<string, string>): string {
  return Object.entries(detail)
    .map(([key, value]) =>
      RENDERABLE_DETAIL_KEYS.has(key) && value.length <= MAX_DETAIL_VALUE
        ? `${key}=${value}`
        : key,
    )
    .join(' ');
}

/**
 * Events this agent is answerable for.
 *
 * `actor` is the agent id, so that is the primary filter. A write target the
 * agent owns is the second: the captain who approves a referral is recorded as
 * the actor, and a referral clerk whose terminal never showed the approval
 * would be hiding the one step it is not allowed to take itself.
 */
export function attributableEvents(
  events: AuditEventView[],
  agent: AgentDescriptorView,
  fleetIds: ReadonlySet<string>,
): AuditEventView[] {
  const targets = new Set(agent.write_targets);
  return events.filter((event) => {
    if (event.actor === agent.agent_id) return true;
    if (event.actor === agent.ref) return true;
    if (!event.target || !targets.has(event.target)) return false;
    // Another fleet member's work on a shared target is that member's line.
    return !fleetIds.has(event.actor);
  });
}

export function agentDecisions(
  decisions: PolicyDecisionView[],
  agent: AgentDescriptorView,
): PolicyDecisionView[] {
  return decisions.filter(
    (decision) => decision.agent_id === agent.agent_id || decision.agent_id === agent.ref,
  );
}

// ------------------------------------------------------------ the session

/**
 * The floor under every fleet counter: the backend instant this console started
 * watching.
 *
 * `make live-demo` runs against a real Firestore, and Firestore keeps the audit
 * log across restarts. So the log a freshly loaded console reads is not empty
 * -- it holds every pass and every fire of every previous run, hours of them --
 * and a counter over that opens at a number the officer in front of it has
 * watched nothing produce. Scoping to the pass in flight did not fix it,
 * because *the pass in flight is itself read out of the log*: with no new pass
 * yet run, the newest `agent_pass` in the log is one from the last run, and the
 * console anchored on it and displayed its totals. The in-memory demo could
 * never show this, because there the log genuinely does start empty.
 *
 * The rule this restores: a counter shows what this session watched happen, and
 * a fresh load therefore reads `0 recorded` and `idle` for every agent no
 * matter what Firestore already holds.
 *
 * **Anchored on the newest instant in the first read, not on the browser
 * clock.** `occurred_at` and `decided_at` are stamped by the backend; a floor
 * from `Date.now()` would be stamped by the tablet, and the two clocks
 * disagree -- a laptop a minute fast against Cloud Run hides a minute of live
 * work, a minute slow admits a minute of stale work, and neither failure is
 * visible on screen. Worse, they are not even the same *string*: the backend
 * writes `datetime.isoformat()` (`+00:00`, microseconds elided when zero) and
 * the browser writes `toISOString()` (`Z`, always milliseconds), and every
 * comparison in this file is a string comparison. Taking the floor out of the
 * log keeps it in the backend's clock and the backend's format, which is the
 * only pair the comparison below is valid over.
 *
 * The tradeoff, stated plainly: a pass already running when the console loads
 * has its earlier half below the floor and is not counted, and an event sharing
 * the anchor's exact timestamp that arrives in a later poll is dropped by the
 * strict `>` in `since`. Both err toward showing less than happened. That is
 * the right direction for this screen -- a fireground counter that under-reports
 * work it did not see is honest, and one that inherits a previous shift's totals
 * is the bug being fixed.
 *
 * `null` when the first read came back empty: an empty log has no backend
 * instant to anchor on, and it needs none -- everything that arrives after it
 * arrived while this session was watching.
 */
export function sessionFloor(
  events: readonly AuditEventView[],
  decisions: readonly PolicyDecisionView[],
): string | null {
  let newest: string | null = null;
  const consider = (at: string): void => {
    if (!newest || compareAt(at, newest) > 0) newest = at;
  };
  for (const event of events) consider(event.occurred_at);
  // Decisions count toward the floor as well. They are written by the gateway
  // on the same clock and they feed the same counters, and a floor taken from
  // events alone would let a decision recorded before the console loaded past
  // it whenever the gateway wrote after the last agent did.
  for (const decision of decisions) consider(decision.decided_at);
  return newest;
}

/** Events strictly after the floor. The floor's own event is pre-session. */
export function eventsSince(events: AuditEventView[], since: string | null): AuditEventView[] {
  if (!since) return events;
  return events.filter((event) => compareAt(event.occurred_at, since) > 0);
}

/** Decisions strictly after the floor, on the same rule. */
export function decisionsSince(
  decisions: PolicyDecisionView[],
  since: string | null,
): PolicyDecisionView[] {
  if (!since) return decisions;
  return decisions.filter((decision) => compareAt(decision.decided_at, since) > 0);
}

// --------------------------------------------------------------- the pass

/**
 * The two kinds that carry a slow-loop pass's own correlation id.
 *
 * Only these two. `write_executed`, `injection_blocked` and a rejected draft
 * each mint a fresh correlation because each stands alone in the log, so they
 * identify no pass -- which is why the window below is a timestamp and not a
 * correlation filter. Scoping on the correlation alone would have dropped the
 * work order and every blocked injection out of the pass that produced them.
 */
const PASS_KINDS = new Set(['agent_pass', 'agent_step']);

export interface PassWindow {
  /** The correlation id `run_slow_loop` minted for this pass. */
  correlationId: string;
  /** When this pass's first recorded event landed. */
  since: string;
}

/**
 * The slow-loop pass in flight, read out of the log itself.
 *
 * There is no endpoint for this. `POST /districts/{id}/poll` returns the pass's
 * report and the report does not carry the correlation id the pass ran under,
 * and the console does not drive every pass anyway -- a scheduler does, and a
 * console that only knew about passes it started would show nothing during the
 * ones it did not. So the log is the source: the newest `agent_pass` or
 * `agent_step` written by an agent in this column names the pass in flight,
 * because those are the only events that carry the pass's own correlation.
 *
 * `actorIds` restricts that to this column's fleet on purpose. `incident-
 * recorder` writes `agent_step` too, and during a fire its steps are the newest
 * in the log -- an unrestricted read would move the slow loop's window every
 * time the incident recorder ticked.
 *
 * The window returned is a *timestamp*, not the correlation, for the reason
 * `PASS_KINDS` gives: a third of a pass's events do not carry it. Everything
 * from the pass's first event onward is the pass, which is true as long as one
 * slow loop runs at a time -- which is what `run_slow_loop` does, one pass per
 * request, agents in sequence.
 *
 * `null` when no agent in this column has recorded a pass or a step at all.
 * The caller then counts the whole session, which is the honest reading of a
 * log with no pass boundary in it rather than a zero nothing supports.
 */
export function currentPass(
  events: AuditEventView[],
  actorIds: ReadonlySet<string>,
): PassWindow | null {
  let newest: AuditEventView | null = null;
  for (const event of events) {
    if (!PASS_KINDS.has(event.kind) || !actorIds.has(event.actor)) continue;
    if (!newest || compareAt(event.occurred_at, newest.occurred_at) > 0) newest = event;
  }
  if (!newest) return null;
  const correlationId = newest.correlation_id;
  let since = newest.occurred_at;
  for (const event of events) {
    if (event.correlation_id !== correlationId) continue;
    if (compareAt(event.occurred_at, since) < 0) since = event.occurred_at;
  }
  return { correlationId, since };
}

// ---------------------------------------------------------------- terminal

export interface TerminalLine {
  id: string;
  at: string;
  tone: PillTone;
  label: string;
  /** Actor, when it is not the agent itself. */
  actor: string | null;
  body: string;
  note: string | null;
}

/**
 * The tail, oldest first.
 *
 * Newest-last on purpose: this box reads like a console someone left running,
 * and the eye goes to the bottom of one of those.
 *
 * The limit was 14, and 14 is smaller than the work. One live slow-loop pass
 * measured 36 audit events with 14 of them `records-watcher`'s alone, and one
 * incident puts around 38 recorder steps in the log -- so the surface labelled
 * *activity* was silently cutting the tail off the busiest agents at the exact
 * moment they were busiest, and an officer counting lines counted the cap
 * rather than the fleet. It was never a display constraint either: the box
 * scrolls, and `max-h-28` is what decides how much of the tail is visible at
 * once. 60 covers several passes of the loudest slow agent and a whole
 * incident's recorder, and it stays a bound because it is a bound on what one
 * session recorded, not on a log that outlives the session.
 */
export function terminalLines(
  events: AuditEventView[],
  decisions: PolicyDecisionView[],
  agent: AgentDescriptorView,
  fleetIds: ReadonlySet<string>,
  limit = 60,
): TerminalLine[] {
  const lines: TerminalLine[] = [
    ...attributableEvents(events, agent, fleetIds).map((event) => {
      const meta = EVENT_TONE[event.kind] ?? { tone: 'muted' as PillTone, label: event.kind };
      const detail = detailText(event.detail);
      return {
        id: event.audit_id,
        at: event.occurred_at,
        tone: meta.tone,
        label: meta.label,
        actor: event.actor === agent.agent_id ? null : event.actor,
        body: [event.target ? `→ ${event.target}` : '', detail].filter(Boolean).join('  '),
        note: null,
      };
    }),
    ...agentDecisions(decisions, agent).map((decision) => ({
      id: decision.decision_id,
      at: decision.decided_at,
      tone: ACTION_TONE[decision.action] ?? ('muted' as PillTone),
      label: decision.action.toLowerCase().replace(/_/g, ' '),
      actor: null,
      body: `→ ${decision.target}  ${decision.rule_id}  policy ${decision.policy_version}`,
      // The policy engine's own words about its own decision. Deterministic,
      // and never derived from a document.
      note: decision.justification,
    })),
  ];
  lines.sort((a, b) => compareAt(a.at, b.at));
  return lines.slice(Math.max(0, lines.length - limit));
}

// ----------------------------------------------------------------- visuals

export interface Pass {
  correlationId: string;
  at: string;
  count: number;
}

/**
 * One correlation id is one pass.
 *
 * This counts *recorded* events per pass, which is not the same as facts
 * written: the audit feed records exceptions, writes, and grants, and a fact
 * append is a profile event the console does not fetch. The caption says so.
 */
export function passBuckets(events: AuditEventView[]): Pass[] {
  const byCorrelation = new Map<string, Pass>();
  const ordered = [...events].sort((a, b) => compareAt(a.occurred_at, b.occurred_at));
  for (const event of ordered) {
    const existing = byCorrelation.get(event.correlation_id);
    if (existing) {
      existing.count += 1;
    } else {
      byCorrelation.set(event.correlation_id, {
        correlationId: event.correlation_id,
        at: event.occurred_at,
        count: 1,
      });
    }
  }
  return [...byCorrelation.values()];
}

/**
 * The registries each polling agent reads, as the source catalog names them.
 *
 * Hard-coded because it is a property of the build, the same way the ranking
 * weights are: `sources/catalog.py` fixes these ids and the agents are written
 * against them. Health comes from the district's own source report.
 */
export const REGISTRIES: Record<string, readonly string[]> = {
  'hazard-watcher': ['epa-frs', 'phmsa-pipelines', 'nrel-ev', 'tier-ii-confidential'],
  'records-watcher': ['sf-permits', 'sf-assessor', 'sf-fire-inspections', 'sf-violations'],
  'geometry-watcher': ['sf-parcels', 'google-solar', 'usgs-3dep'],
};

export type ReachState = 'reached' | 'unreachable' | 'unreported';

export interface RegistryPip {
  sourceId: string;
  short: string;
  state: ReachState;
  detail: string;
}

function shortSourceName(sourceId: string): string {
  const tail = sourceId.split('-')[0] ?? sourceId;
  return tail.length > 6 ? tail.slice(0, 6) : tail;
}

export function registryPips(
  agent: AgentDescriptorView,
  sources: SourceHealthView[],
  events: AuditEventView[],
): RegistryPip[] {
  const ids = REGISTRIES[agent.agent_id] ?? [];
  const health = new Map(sources.map((source) => [source.source_id, source]));
  return ids.map((sourceId) => {
    const reported = health.get(sourceId);
    if (reported) {
      return {
        sourceId,
        short: shortSourceName(sourceId),
        state: reported.available ? ('reached' as const) : ('unreachable' as const),
        detail: `${reported.mode.toLowerCase()}, circuit ${reported.circuit_state.toLowerCase()}`,
      };
    }
    // No health report. The audit feed can still say the agent could not
    // reach it, which is worth more than a hopeful dot.
    const failed = events.some(
      (event) => event.target === sourceId && UNREACHABLE_KINDS.has(event.kind),
    );
    return {
      sourceId,
      short: shortSourceName(sourceId),
      state: failed ? ('unreachable' as const) : ('unreported' as const),
      detail: failed ? 'reported unreachable in the audit feed' : 'no health reported',
    };
  });
}

/** The four structure-ranking weights, as `structure_watch.py` fixes them. */
export const RANK_WEIGHTS: readonly { ruleId: string; label: string; weight: number }[] = [
  { ruleId: 'rank.open-conflict-severity', label: 'conflict', weight: 0.4 },
  { ruleId: 'rank.confidence-decay', label: 'decay', weight: 0.25 },
  { ruleId: 'rank.source-churn', label: 'churn', weight: 0.2 },
  { ruleId: 'rank.survey-age', label: 'survey age', weight: 0.15 },
];

export interface Pipeline {
  staged: number;
  approved: number;
  filed: number;
}

export function referralPipeline(
  events: AuditEventView[],
  decisions: PolicyDecisionView[],
): Pipeline {
  return {
    // A draft that reached the gateway and was told a human must sign it.
    staged: decisions.filter((decision) => decision.action === 'REQUIRE_APPROVAL').length,
    approved: events.filter((event) => event.kind === 'approval_granted').length,
    filed: events.filter(
      (event) => event.kind === 'write_executed' || event.kind === 'write_replayed',
    ).length,
  };
}

export interface AudienceRow {
  target: string;
  sent: number;
  awaiting: number;
  refused: number;
}

export function audienceRows(
  events: AuditEventView[],
  decisions: PolicyDecisionView[],
): AudienceRow[] {
  const rows = new Map<string, AudienceRow>();
  const row = (target: string): AudienceRow => {
    const existing = rows.get(target);
    if (existing) return existing;
    const created = { target, sent: 0, awaiting: 0, refused: 0 };
    rows.set(target, created);
    return created;
  };
  for (const event of events) {
    if (!event.target || !WRITE_KINDS.has(event.kind)) continue;
    row(event.target).sent += 1;
  }
  for (const decision of decisions) {
    const current = row(decision.target);
    if (decision.action === 'REQUIRE_APPROVAL') current.awaiting += 1;
    else if (decision.action === 'DENY' || decision.action === 'WITHHOLD_JURISDICTION') {
      current.refused += 1;
    }
  }
  return [...rows.values()].sort((a, b) => a.target.localeCompare(b.target));
}

export interface Ledger {
  flushed: number;
  attempted: number;
  passes: number;
}

/** Log entries the recorder has written through to the records system. */
export function recorderLedger(events: AuditEventView[]): Ledger {
  let flushed = 0;
  let attempted = 0;
  let passes = 0;
  for (const event of events) {
    if (event.kind !== 'rms_flushed') continue;
    passes += 1;
    flushed += Number.parseInt(event.detail.flushed ?? '0', 10) || 0;
    attempted += Number.parseInt(event.detail.attempted ?? '0', 10) || 0;
  }
  return { flushed, attempted, passes };
}

export interface FanOutLine {
  agentId: string;
  ruleIds: string[];
  state: 'started' | 'not started' | 'withheld';
  note: string;
}

/** Who the interceptor woke, from the intake the incident carries. */
export function fanOut(incident: OpenIncidentResponse | null): FanOutLine[] {
  const intake = incident?.intake;
  if (!intake) return [];
  const bare = (ref: string): string => ref.split('@')[0] ?? ref;
  return [
    ...intake.woken.map((line) => ({
      agentId: bare(line.agent_ref),
      ruleIds: [...line.rule_ids],
      state: line.started ? ('started' as const) : ('not started' as const),
      note: line.agent_ref,
    })),
    ...intake.withheld.map((line) => ({
      agentId: bare(line.agent_ref),
      ruleIds: [...line.rule_ids],
      state: 'withheld' as const,
      note: `missing ${line.missing_scopes.join(', ')}`,
    })),
  ];
}

export type FaceState = 'scanned' | 'unscanned' | 'unavailable';

export interface FaceQuadrant {
  label: string;
  state: FaceState;
  reading: string;
}

const SIDES = ['ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'] as const;

export function faceQuadrants(geometry: GeometryView | null): FaceQuadrant[] {
  if (!geometry) return [];
  const byLabel = new Map<string, FaceView>(geometry.spec.faces.map((face) => [face.label, face]));
  return SIDES.map((label) => {
    const face = byLabel.get(label);
    if (!face) return { label, state: 'unscanned' as const, reading: 'no face in the spec' };
    if (face.thermal.kind === 'QUANTITY') {
      return {
        label,
        state: 'scanned' as const,
        reading: `${Math.round(face.thermal.magnitude)}${face.thermal.unit}`,
      };
    }
    if (face.thermal.kind === 'UNAVAILABLE') {
      return { label, state: 'unavailable' as const, reading: face.thermal.reason };
    }
    return { label, state: 'unscanned' as const, reading: 'unscanned' };
  });
}

export interface Massing {
  levels: { heightM: number; disputed: boolean }[];
  totalHeightM: number;
  collapseZoneM: number;
  addressId: string;
}

export function massing(geometry: GeometryView | null): Massing | null {
  if (!geometry) return null;
  return {
    levels: geometry.spec.levels.map((level) => ({
      heightM: level.height_m,
      disputed: level.status === 'DISPUTED',
    })),
    totalHeightM: geometry.total_height_m,
    collapseZoneM: geometry.spec.collapse_zone_radius_m,
    addressId: geometry.spec.address_id,
  };
}
