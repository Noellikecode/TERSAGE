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
 */
export function terminalLines(
  events: AuditEventView[],
  decisions: PolicyDecisionView[],
  agent: AgentDescriptorView,
  fleetIds: ReadonlySet<string>,
  limit = 14,
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
  lines.sort((a, b) => a.at.localeCompare(b.at));
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
  const ordered = [...events].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
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
