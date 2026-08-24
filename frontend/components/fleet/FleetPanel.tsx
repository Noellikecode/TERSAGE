/**
 * The fleet panel: one card per agent, for one loop at a time.
 *
 * Two rules this panel exists to hold.
 *
 * **A loop that is not running is not listed.** The incident agents do not
 * idle -- they do not exist until a dispatch wakes them -- so listing them in
 * standby as "idle" would claim a readiness state this system does not have.
 * The scope comes from the descriptor's own `loop` field and defaults to the
 * slow loop, which is the only one running when nothing is on fire. Filtering
 * on the descriptor rather than on a list of ids is deliberate: a tenth agent
 * added to the catalog cannot silently reintroduce this.
 *
 * **The slow loop does not stop when a fire starts.** During an incident the
 * console runs two of these side by side -- the incident loop on the left of
 * the structure, the slow loop on its right -- and both get full cards,
 * because a rail that vanished, or shrank to a row of chips, would tell an
 * officer the rest of the fleet had stopped. It has not; it is in the next
 * column, still writing facts while the fire burns.
 *
 * Superseded agents are never dropped. A brief recorded two years ago names
 * the agent version that produced it, and an id deleted from the catalog turns
 * that record into a reference to something this build has never heard of.
 * They stay listed, visibly retired, and are not counted as idle fleet.
 */

import { AgentCard, type AgentActivity, type FleetContext } from '@/components/fleet/AgentCard';
import { StatusPill } from '@/components/StatusPill';
import type {
  AgentDescriptorView,
  AuditEventView,
  GeometryView,
  OpenIncidentResponse,
  PolicyDecisionView,
  SourceHealthView,
  SubscriptionView,
} from '@/lib/api/types';

/**
 * One-shot motion, tied to a change that actually happened.
 *
 * A card flashes when its newest line is new and when its recorded count
 * ticks, and then stops. Nothing here loops: a permanent animation reads as
 * "working" even when nothing is happening, which is the same lie as a
 * fabricated log line, drawn instead of written.
 *
 * `globals.css` already flattens animations under `prefers-reduced-motion`;
 * the guard is repeated here so this component carries its own promise.
 */
const MOTION_CSS = `
@keyframes fleet-fresh {
  from { background-color: rgba(56, 189, 248, 0.24); }
  to { background-color: transparent; }
}
@keyframes fleet-tick {
  from { color: #38bdf8; }
  to { color: inherit; }
}
.fleet-fresh { animation: fleet-fresh 1.8s ease-out 1; }
.fleet-tick { animation: fleet-tick 1.8s ease-out 1; }
@media (prefers-reduced-motion: reduce) {
  .fleet-fresh, .fleet-tick { animation: none; }
}
`;

/**
 * The cards lay themselves out from the width they are given.
 *
 * `auto-fit` rather than a prop: in standby the panel is the main content area
 * and gets two or three columns, in an incident it is a 400px column and gets
 * one, and neither the caller nor a breakpoint has to be told which.
 */
const CARD_GRID = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 23rem), 1fr))',
  gap: '0.5rem',
  alignContent: 'start',
} as const;

export interface FleetPanelProps {
  agents: AgentDescriptorView[];
  /**
   * The whole published fleet, when this panel draws only part of it.
   *
   * Standby renders two columns, each handed half the slow loop. Event
   * attribution needs the *full* roster regardless: a write target can be
   * shared, and the rule for a shared target is "another fleet member's
   * work is that member's line". A column that only knew its own half
   * would claim the other half's writes and show one agent doing another's
   * work, in the exact panel an officer reads to see what each one did.
   *
   * Falls back to `agents` when absent, so a single-panel caller is
   * unaffected.
   */
  fleetRoster?: AgentDescriptorView[];
  subscriptions: SubscriptionView[];
  /**
   * Which loop this panel is showing. Defaults to the slow loop: it is the one
   * that runs when nothing is on fire, and an unscoped fleet listing agents
   * that only exist during an incident is the bug this default prevents.
   */
  loop?: 'SLOW' | 'INCIDENT';
  /** The reasoning terminals' data source. Filtered per agent by `actor`. */
  events?: AuditEventView[];
  decisions?: PolicyDecisionView[];
  /** The open incident, if any. Feeds the interceptor's fan-out glyph. */
  incident?: OpenIncidentResponse | null;
  /**
   * The structure currently on screen, if any. Feeds the geometry watcher's
   * massing glyph and the sensor fusion face coverage -- neither of which has
   * any other data path to this console.
   */
  geometry?: GeometryView | null;
  /** The district's source health, from the stats the console already fetches. */
  sources?: SourceHealthView[];
  /** Live run state, when a caller has it. Nothing here invents one. */
  activity?: Record<string, AgentActivity>;
  /**
   * Legacy: collapse to a row of chips. No current call site uses it -- the
   * slow loop gets its own column during an incident now -- and it is kept
   * only so a caller mid-rewrite renders something rather than nothing.
   */
  compressed?: boolean;
}

function CompressedStrip({
  agents,
  pinned,
  activity,
  label,
}: {
  agents: AgentDescriptorView[];
  pinned: Map<string, string>;
  activity: Record<string, AgentActivity>;
  label: string;
}) {
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label={label}>
      {agents.map((agent) => (
        <li key={agent.ref}>
          <StatusPill
            tone={activity[agent.agent_id]?.current ? 'live' : 'muted'}
            label={`${agent.agent_id} @${pinned.get(agent.agent_id) ?? agent.version}`}
            title={`${agent.role_summary} Published by ${agent.publisher_department}.`}
          />
        </li>
      ))}
    </ul>
  );
}

function SupersededGroup({ agents }: { agents: AgentDescriptorView[] }) {
  return (
    <li
      className="border border-dashed border-line p-3"
      style={{ gridColumn: '1 / -1' }}
      data-testid="superseded-agents"
    >
      <p className="text-micro uppercase tracking-widest text-muted">Superseded · still catalogued</p>
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
      <p className="mt-2 text-micro leading-5 text-muted">
        Not scheduled and given no worker. They stay resolvable because a brief recorded two years
        ago names the agent version that produced it, and an id deleted from the catalog would make
        that record unreadable.
      </p>
    </li>
  );
}

export function FleetPanel({
  agents,
  fleetRoster,
  subscriptions,
  loop,
  events = [],
  decisions = [],
  incident = null,
  geometry = null,
  sources = [],
  activity = {},
  compressed = false,
}: FleetPanelProps) {
  const pinned = new Map(subscriptions.map((s) => [s.agent_id, s.pinned_version]));
  // The descriptor decides, never a list of ids kept in the console.
  const scope = loop ?? 'SLOW';
  const scoped = agents.filter((agent) => agent.loop === scope);
  const live = scoped.filter((agent) => !agent.deprecated_at);
  const superseded = scoped.filter((agent) => agent.deprecated_at);

  const context: FleetContext = {
    events,
    decisions,
    incident,
    geometry,
    sources,
    fleetIds: new Set((fleetRoster ?? agents).map((agent) => agent.agent_id)),
  };

  if (live.length === 0 && superseded.length === 0) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        {scope === 'INCIDENT'
          ? 'No incident agents published. The registry reported an empty catalog.'
          : 'No agents published. The registry reported an empty catalog.'}
      </p>
    );
  }

  if (compressed) {
    return (
      <CompressedStrip
        agents={live}
        pinned={pinned}
        activity={activity}
        label={
          scope === 'INCIDENT' ? 'Incident fleet' : 'Slow-loop fleet, still running'
        }
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <style>{MOTION_CSS}</style>
      <ul
        className="min-h-0 flex-1 overflow-y-auto pr-1"
        style={CARD_GRID}
        aria-label={scope === 'INCIDENT' ? 'Incident fleet' : 'Fleet'}
      >
        {live.map((agent) => (
          <AgentCard
            key={agent.ref}
            agent={agent}
            pinnedVersion={pinned.get(agent.agent_id)}
            activity={activity[agent.agent_id]}
            context={context}
          />
        ))}
        {superseded.length > 0 && <SupersededGroup agents={superseded} />}
      </ul>
    </div>
  );
}
