/**
 * Everything about the agent currently selected, in one fixed place.
 *
 * This is the body of what used to be `AgentCard`, drawn once instead of nine
 * times. Nothing was cut on the way: the role summary is no longer clamped to
 * two lines, the provenance is still every value a NIOSH investigation reads,
 * and the glyph and the reasoning terminal are the same components they were.
 *
 * The pane does not float and does not push anything down. That is the whole
 * reason it is a pane rather than a popover: it holds while somebody talks over
 * it, and the thing being pointed at does not move.
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
import type { FleetState } from '@/components/fleet/FleetRow';
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

export function latency(ms: number): string {
  return ms >= 1000 ? `${Math.round(ms / 1000)}s` : `${ms}ms`;
}

/**
 * The glyph for one agent.
 *
 * Switched on the agent id rather than on a capability, because these are nine
 * specific jobs and the drawing is only honest if it is the drawing of that
 * job. Anything unrecognised gets the plain tick row instead of somebody else's
 * picture.
 */
/**
 * The Google technology each agent runs on, read off the code that runs it.
 *
 * Not in the descriptor, deliberately: a catalog entry declares what an agent
 * is *allowed* to do -- its scopes, its budget, its write targets -- and the
 * model behind it is an implementation choice that can change without changing
 * the contract. So this is a console-side map, and every entry below was
 * traced rather than assumed:
 *
 * - `records-watcher` calls `triage` on the Gemma model before `extract` on
 *   Gemini, and recalls open questions from the memory bank.
 * - `hazard-watcher` runs a LangGraph identity loop over Gemini, with the same
 *   memory bank behind it.
 * - `geometry-watcher` derives height from the Solar API and USGS elevation;
 *   no language model is involved in a subtraction.
 * - `structure-watch` has no model at all. Its conflict rules and its four
 *   weighted signals are deterministic, and that is the point of them.
 * - `referral-clerk` and `agency-notifier` share `ActionFlow`, which composes
 *   wording through `model.compose` and falls back to the template.
 * - `incident-interceptor` reads the intake and composes the brief through the
 *   model, and routes with a LangGraph focus composer.
 * - `sensor-fusion` is the only multimodal caller: `container.vision` reads a
 *   frame and returns observations bound to image regions.
 * - `incident-recorder` runs its closing graph over the model.
 *
 * **What did the work, not what stored it.** Memory Bank is not chipped: it is
 * where an open question is kept, and keeping is not reasoning. What *is*
 * chipped is whatever actually produced the answer, which for two of these is
 * not a language model at all -- and saying "no model" was the wrong way to
 * put that. `geometry-watcher` derives height by subtracting a USGS ground
 * datum from a Solar API roof plane; `structure-watch` runs the deterministic
 * conflict rules and the weighted ranking. Both are techniques with names, and
 * an empty-looking chip made two working agents read as unequipped ones.
 *
 * The model ids are the ones this build is configured with --
 * `GEMINI_MODEL=gemini-3.5-flash` and `GEMMA_MODEL=gemma-4-26b-a4b-it-maas` in
 * settings. Written out rather than shown as "Gemini" because which model ran
 * is part of what makes a fact reproducible two years later, and because the
 * two are not interchangeable: the small one is only ever allowed to decide
 * whether a document is worth reading, never what it says.
 */
const TECH: Readonly<Record<string, readonly string[]>> = {
  'records-watcher': ['gemma-4-26b · triage', 'gemini-3.5-flash'],
  'hazard-watcher': ['gemini-3.5-flash'],
  'geometry-watcher': ['Solar API', 'USGS 3DEP'],
  'structure-watch': ['Conflict rules', 'A* ranking'],
  'referral-clerk': ['gemini-3.5-flash'],
  'agency-notifier': ['gemini-3.5-flash'],
  'incident-interceptor': ['gemini-3.5-flash'],
  'sensor-fusion': ['gemini-3.5-flash · vision'],
  'incident-recorder': ['gemini-3.5-flash'],
};


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

/** One `label value` pair. The label never repeats the value's own word --
 *  "Publisher fire", not the old "Publisher → by fire", which spent two lines
 *  saying one thing. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-muted">{label}</dt>
      <dd className="text-ink">{children}</dd>
    </div>
  );
}

/**
 * The superseded group, explained once.
 *
 * It used to render inside every fleet column, which put the same forty words
 * on screen twice. It is an agent-shaped thing without an agent, so it gets the
 * pane like any other selection.
 */
export function SupersededDetail({ agents }: { agents: AgentDescriptorView[] }) {
  return (
    <div data-testid="fleet-detail-superseded">
      <h3 className="font-mono text-ink">Superseded · still catalogued</h3>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {agents.map((agent) => (
          <li key={agent.ref}>
            <StatusPill
              tone="muted"
              label={`${agent.agent_id} @${agent.version}`}
              title={`${agent.role_summary} Superseded, no longer scheduled.`}
            />
          </li>
        ))}
      </ul>
      <p className="mt-3 text-micro leading-5 text-muted">
        Not scheduled and given no worker. They stay resolvable because a brief recorded two years
        ago names the agent version that produced it, and an id deleted from the catalog would make
        that record unreadable.
      </p>
    </div>
  );
}

export function FleetDetail({
  agent,
  state,
  pinnedVersion,
  activity,
  context,
}: {
  agent: AgentDescriptorView;
  state: FleetState;
  pinnedVersion: string | undefined;
  activity: AgentActivity | undefined;
  context: FleetContext;
}) {
  const mine = attributableEvents(context.events, agent, context.fleetIds);
  const mineDecisions = agentDecisions(context.decisions, agent);
  const lines = terminalLines(context.events, context.decisions, agent, context.fleetIds);
  const recorded = mine.length + mineDecisions.length;
  const drifted = pinnedVersion !== undefined && pinnedVersion !== agent.version;

  return (
    <div data-testid={`fleet-detail-${agent.agent_id}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono text-ink">{agent.agent_id}</h3>
        <StatusPill tone={state === 'idle' ? 'muted' : 'live'} label={state} />
      </div>

      {/* What Google technology this agent actually runs on.
          Traced from the code, not from the catalog: the descriptor declares
          scopes and budgets, not models, so this map is maintained here and
          each entry was read off the agent that claims it. `structure-watch`
          carries no model chip because it has no `self._model` at all -- its
          conflict rules and its ranking are deterministic, and a chip implying
          otherwise would overstate what decides a survey queue. */}
      {(TECH[agent.agent_id] ?? []).length > 0 && (
        <ul className="mt-1.5 flex flex-wrap gap-1" data-testid={`fleet-tech-${agent.agent_id}`}>
          {(TECH[agent.agent_id] ?? []).map((tech) => (
            <li
              key={tech}
              className="rounded border border-live/40 bg-live/10 px-1.5 py-0.5 text-micro leading-4 text-live"
            >
              {tech}
            </li>
          ))}
        </ul>
      )}

      {/* Unclamped. There is room for the whole sentence now. */}
      <p className="mt-1.5 text-micro leading-5 text-muted">{agent.role_summary}</p>

      {/* Every value here is provenance -- who published it, which version is
          pinned, how much it has done, what it promised. None of it is trimmed. */}
      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-micro">
        <Fact label="Publisher">{agent.publisher_department}</Fact>
        <Fact label="Pinned">
          <span className={drifted ? 'text-disputed' : 'text-ink'}>
            {pinnedVersion ? `@${pinnedVersion}` : 'not subscribed'}
          </span>
          {drifted && <span className="text-disputed"> (catalog has {agent.version})</span>}
        </Fact>
        <Fact label="Budget">{latency(agent.latency_target_ms)}</Fact>
        <Fact label="Recorded">
          {activity ? `${activity.throughput} runs` : `${recorded} recorded`}
        </Fact>
      </dl>

      {activity?.current && (
        <p className="mt-2 border-l-2 border-live pl-2 text-micro text-ink">{activity.current}</p>
      )}

      <VisualFor agent={agent} mine={mine} mineDecisions={mineDecisions} context={context} />

      <ReasoningTerminal agentId={agent.agent_id} lines={lines} />

      {agent.write_targets.length > 0 && (
        <p className="mt-2 text-micro leading-5 text-muted">
          Writes to {agent.write_targets.join(', ')}
          {agent.approval_threshold !== 'NONE' && (
            <span className="text-disputed">
              {' '}
              · {agent.approval_threshold.toLowerCase()} approval required
            </span>
          )}
        </p>
      )}
    </div>
  );
}
