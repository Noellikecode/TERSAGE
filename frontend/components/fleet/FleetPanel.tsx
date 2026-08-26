/**
 * The fleet panel: a list of agents, and one pane about the selected one.
 *
 * Three rules this panel exists to hold.
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
 * the structure, the slow loop on its right -- because a panel that vanished
 * would tell an officer the rest of the fleet had stopped. It has not; it is
 * in the next column, still writing facts while the fire burns.
 *
 * **One panel, one selection.** The rows are the fleet at rest; the pane is the
 * one agent being asked about. Drawing all nine agents' provenance, glyphs and
 * terminals at once put five screens of scroll on the page before anything had
 * happened, and made the thing an officer wanted -- which agents are working --
 * the hardest thing to find.
 *
 * Superseded agents are never dropped. A brief recorded two years ago names
 * the agent version that produced it, and an id deleted from the catalog turns
 * that record into a reference to something this build has never heard of.
 * They stay listed, visibly retired, and are not counted as idle fleet.
 */

import { useCallback, useMemo, useState } from 'react';

import {
  FleetDetail,
  SupersededDetail,
  type AgentActivity,
  type FleetContext,
} from '@/components/fleet/FleetDetail';
import { FleetRow, type FleetState } from '@/components/fleet/FleetRow';
import { agentDecisions, attributableEvents } from '@/components/fleet/derive';
import type {
  AgentDescriptorView,
  AuditEventView,
  GeometryView,
  OpenIncidentResponse,
  PolicyDecisionView,
  SourceHealthView,
  SubscriptionView,
} from '@/lib/api/types';

export type { AgentActivity, FleetContext };

/**
 * One-shot motion, tied to a change that actually happened.
 *
 * Nothing here loops: a permanent animation reads as "working" even when
 * nothing is happening, which is the same lie as a fabricated log line, drawn
 * instead of written.
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

/** The id the superseded group answers to. Not an agent id, and it cannot
 *  collide with one: agent ids never carry a colon. */
const SUPERSEDED_KEY = 'fleet:superseded';

export interface FleetPanelProps {
  agents: AgentDescriptorView[];
  /**
   * The whole published fleet, when this panel draws only part of it.
   *
   * Event attribution needs the *full* roster: a write target can be shared,
   * and the rule for a shared target is "another fleet member's work is that
   * member's line". A panel that only knew part of the fleet would claim the
   * rest's writes and show one agent doing another's work.
   *
   * Falls back to `agents` when absent.
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
}: FleetPanelProps) {
  const pinned = new Map(subscriptions.map((s) => [s.agent_id, s.pinned_version]));
  // The descriptor decides, never a list of ids kept in the console.
  const scope = loop ?? 'SLOW';
  const scoped = agents.filter((agent) => agent.loop === scope);
  const live = scoped.filter((agent) => !agent.deprecated_at);
  const superseded = scoped.filter((agent) => agent.deprecated_at);

  const context: FleetContext = useMemo(
    () => ({
      events,
      decisions,
      incident,
      geometry,
      sources,
      fleetIds: new Set((fleetRoster ?? agents).map((agent) => agent.agent_id)),
    }),
    [events, decisions, incident, geometry, sources, fleetRoster, agents],
  );

  /**
   * Three states, because the console knows three different things. "running"
   * means a caller told us what this agent is doing right now; "active" means
   * it has recorded work this session and nothing more is claimed; "idle" means
   * nothing was recorded. Collapsing the middle one into "running" would put a
   * word on the screen no record supports.
   */
  const rows = useMemo(
    () =>
      live.map((agent) => {
        const recorded =
          attributableEvents(events, agent, context.fleetIds).length +
          agentDecisions(decisions, agent).length;
        const act = activity[agent.agent_id];
        const state: FleetState = act?.current ? 'running' : recorded > 0 ? 'active' : 'idle';
        return {
          agent,
          state,
          // The same rule the card used: a supplied run count wins, and the
          // console never invents one it was not given.
          metric: act ? `${act.throughput} runs` : `${recorded} recorded`,
        };
      }),
    [live, events, decisions, activity, context.fleetIds],
  );

  /**
   * Catalog order, not activity order.
   *
   * The first working agent opens the pane so it is never empty and never opens
   * on something idle. Ordering by activity instead would move the selection
   * out from under somebody the moment an agent wrote a fact, which is exactly
   * when they were reading it.
   */
  const opening =
    rows.find((row) => row.state === 'running')?.agent.agent_id ??
    rows.find((row) => row.state === 'active')?.agent.agent_id ??
    rows[0]?.agent.agent_id ??
    (superseded.length > 0 ? SUPERSEDED_KEY : null);

  // Click pins; hover and focus only preview. The pinned one is what the pane
  // falls back to when the pointer leaves, which is what lets somebody talk
  // over this without the pane emptying under them.
  const [held, setHeld] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const shown = preview ?? held ?? opening;

  const clearPreview = useCallback(() => setPreview(null), []);

  if (live.length === 0 && superseded.length === 0) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        {scope === 'INCIDENT'
          ? 'No incident agents published. The registry reported an empty catalog.'
          : 'No agents published. The registry reported an empty catalog.'}
      </p>
    );
  }

  const shownRow = rows.find((row) => row.agent.agent_id === shown);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <style>{MOTION_CSS}</style>

      <ul
        className="shrink-0"
        aria-label={scope === 'INCIDENT' ? 'Incident fleet' : 'Fleet'}
        onMouseLeave={clearPreview}
        onBlur={clearPreview}
      >
        {rows.map((row) => (
          <FleetRow
            key={row.agent.ref}
            agent={row.agent}
            state={row.state}
            metric={row.metric}
            selected={row.agent.agent_id === shown}
            onSelect={() => setHeld(row.agent.agent_id)}
            onPreview={() => setPreview(row.agent.agent_id)}
          />
        ))}

        {superseded.length > 0 && (
          <li>
            <button
              type="button"
              onClick={() => setHeld(SUPERSEDED_KEY)}
              onMouseEnter={() => setPreview(SUPERSEDED_KEY)}
              onFocus={() => setPreview(SUPERSEDED_KEY)}
              aria-current={shown === SUPERSEDED_KEY ? 'true' : undefined}
              data-testid="superseded-agents"
              className={`flex w-full items-baseline gap-3 border-l-4 px-3 py-2.5 text-left text-body transition-colors ${
                shown === SUPERSEDED_KEY
                  ? 'border-line bg-raised text-ink'
                  : 'border-transparent text-muted hover:bg-raised hover:text-ink'
              }`}
            >
              <span aria-hidden="true" className="text-title leading-none">
                ·
              </span>
              <span className="flex-1 truncate">
                {superseded.length} superseded · still catalogued
              </span>
            </button>
          </li>
        )}
      </ul>

      {/* The pane. `aria-live` because the thing that changed is here and not
          where the pointer is. */}
      <div
        className="min-h-0 flex-1 overflow-y-auto border-t border-line pt-2"
        aria-live="polite"
        data-testid="fleet-detail"
      >
        {shown === SUPERSEDED_KEY ? (
          <SupersededDetail agents={superseded} />
        ) : shownRow ? (
          <FleetDetail
            agent={shownRow.agent}
            state={shownRow.state}
            pinnedVersion={pinned.get(shownRow.agent.agent_id)}
            activity={activity[shownRow.agent.agent_id]}
            context={context}
          />
        ) : null}
      </div>
    </div>
  );
}
