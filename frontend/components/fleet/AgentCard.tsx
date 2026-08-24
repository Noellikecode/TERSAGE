/**
 * One agent, as a card: who it is, what it did, and what it decided.
 *
 * Three parts, in the order an officer asks for them. Identity and pinned
 * version first -- pinning is not devops trivia here, it is what a NIOSH
 * investigation reads to know which code produced a fact. Then the visual,
 * which is different for every agent because a rail of identical rows tells
 * you nothing about a fleet. Then the terminal, which is the agent's own
 * decisions in its own words.
 */

import {
  agentDecisions,
  attributableEvents,
  faceQuadrants,
  fanOut,
  massing,
  passBuckets,
  recorderLedger,
  referralPipeline,
  registryPips,
  terminalLines,
  audienceRows,
} from '@/components/fleet/derive';
import { ReasoningTerminal } from '@/components/fleet/ReasoningTerminal';
import {
  AudienceBars,
  FaceQuadrants,
  FanOutGlyph,
  GenericTicks,
  LedgerMeter,
  MassingGlyph,
  PassSpark,
  PipelineGlyph,
  RegistryPipsVisual,
  WeightBar,
} from '@/components/fleet/visuals';
import { StatusPill } from '@/components/StatusPill';
import type {
  AgentDescriptorView,
  AuditEventView,
  GeometryView,
  OpenIncidentResponse,
  PolicyDecisionView,
  SourceHealthView,
} from '@/lib/api/types';

export interface AgentActivity {
  /** Runs completed in the current window. */
  throughput: number;
  /** What it is doing right now, in one line. */
  current: string | null;
}

export interface FleetContext {
  events: AuditEventView[];
  decisions: PolicyDecisionView[];
  incident: OpenIncidentResponse | null;
  geometry: GeometryView | null;
  sources: SourceHealthView[];
  fleetIds: ReadonlySet<string>;
}

function latency(ms: number): string {
  return ms >= 1000 ? `${Math.round(ms / 1000)}s budget` : `${ms}ms budget`;
}

/**
 * The glyph for one agent.
 *
 * Switched on the agent id rather than on a capability, because these are
 * nine specific jobs and the drawing is only honest if it is the drawing of
 * that job. Anything unrecognised gets the plain tick row instead of somebody
 * else's picture.
 */
function VisualFor({
  agent,
  mine,
  mineDecisions,
  context,
}: {
  agent: AgentDescriptorView;
  mine: AuditEventView[];
  mineDecisions: PolicyDecisionView[];
  context: FleetContext;
}) {
  const id = agent.agent_id;
  switch (id) {
    case 'records-watcher':
      return <PassSpark agentId={id} passes={passBuckets(mine)} />;
    case 'hazard-watcher':
      return (
        <RegistryPipsVisual agentId={id} pips={registryPips(agent, context.sources, mine)} />
      );
    case 'geometry-watcher':
      return <MassingGlyph agentId={id} mass={massing(context.geometry)} />;
    case 'structure-watch':
      return <WeightBar agentId={id} />;
    case 'referral-clerk':
      return <PipelineGlyph agentId={id} pipeline={referralPipeline(mine, mineDecisions)} />;
    case 'incident-interceptor':
      return <FanOutGlyph agentId={id} lines={fanOut(context.incident)} />;
    case 'sensor-fusion':
      return <FaceQuadrants agentId={id} quadrants={faceQuadrants(context.geometry)} />;
    case 'agency-notifier':
      return <AudienceBars agentId={id} rows={audienceRows(mine, mineDecisions)} />;
    case 'incident-recorder':
      return <LedgerMeter agentId={id} ledger={recorderLedger(mine)} />;
    default:
      return <GenericTicks agentId={id} passes={passBuckets(mine)} />;
  }
}

export function AgentCard({
  agent,
  pinnedVersion,
  activity,
  context,
}: {
  agent: AgentDescriptorView;
  pinnedVersion: string | undefined;
  activity: AgentActivity | undefined;
  context: FleetContext;
}) {
  const mine = attributableEvents(context.events, agent, context.fleetIds);
  const mineDecisions = agentDecisions(context.decisions, agent);
  const lines = terminalLines(context.events, context.decisions, agent, context.fleetIds);
  const recorded = mine.length + mineDecisions.length;
  const drifted = pinnedVersion !== undefined && pinnedVersion !== agent.version;

  // Three states, because the console knows three different things. "running"
  // means a caller told us what this agent is doing right now; "active" means
  // it has recorded work this session and nothing more is claimed; "idle"
  // means nothing was recorded. Collapsing the middle one into "running"
  // would put a word on the screen no record supports.
  const state = activity?.current ? 'running' : recorded > 0 ? 'active' : 'idle';

  return (
    <li
      className="border border-line bg-surface p-3"
      aria-label={`${agent.agent_id}, ${state}, published by ${
        agent.publisher_department
      }, pinned ${pinnedVersion ? `at ${pinnedVersion}` : 'nowhere'}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-mono text-ink">{agent.agent_id}</span>
        <StatusPill tone={state === 'idle' ? 'muted' : 'live'} label={state} />
      </div>
      {/* Two lines, with the whole summary on hover. The glyph below says what
          this agent does; the prose only has to say which job it is. */}
      <p className="mt-1 line-clamp-2 text-micro leading-4 text-muted" title={agent.role_summary}>
        {agent.role_summary}
      </p>

      {/* One wrapped line rather than an eight-cell table. Every value here is
          provenance -- who published it, what version is pinned, how much it
          has done -- so none of it is trimmed, only the labels around it. */}
      <dl className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-micro">
        <div>
          <dt className="sr-only">Publisher</dt>
          <dd>
            <span className="text-muted">by </span>
            <span className="text-ink">{agent.publisher_department}</span>
          </dd>
        </div>
        <div>
          <dt className="sr-only">Pinned version</dt>
          <dd>
            <span className="text-muted">pin </span>
            <span className={drifted ? 'text-disputed' : 'text-ink'}>
              {pinnedVersion ? `@${pinnedVersion}` : 'not subscribed'}
            </span>
            {drifted && (
              <span className="text-disputed"> (catalog has {agent.version})</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="sr-only">Throughput</dt>
          <dd className="text-ink">
            {/* Keyed by the count, so the number flashes once when it moves and
                then sits still. */}
            <span key={activity ? activity.throughput : recorded} className="fleet-tick">
              {activity ? `${activity.throughput} runs` : `${recorded} recorded`}
            </span>
          </dd>
        </div>
        <div>
          <dt className="sr-only">Latency target</dt>
          <dd className="text-muted">{latency(agent.latency_target_ms)}</dd>
        </div>
      </dl>

      {activity?.current && (
        <p className="mt-2 border-l-2 border-live pl-2 text-micro text-ink">{activity.current}</p>
      )}

      <VisualFor
        agent={agent}
        mine={mine}
        mineDecisions={mineDecisions}
        context={context}
      />

      <ReasoningTerminal agentId={agent.agent_id} lines={lines} />

      {agent.write_targets.length > 0 && (
        <p className="mt-2 text-micro leading-4 text-muted">
          Writes to {agent.write_targets.join(', ')}
          {agent.approval_threshold !== 'NONE' && (
            <span className="text-disputed">
              {' '}
              · {agent.approval_threshold.toLowerCase()} approval required
            </span>
          )}
        </p>
      )}
    </li>
  );
}
