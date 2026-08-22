/**
 * The fleet rail: who is running, published by whom, pinned at which version.
 *
 * Pinning is not devops trivia on this screen. A NIOSH investigation has to
 * reconstruct which code produced a fact two years ago, so the version a
 * department is pinned to is a fact an officer can read here.
 *
 * Compresses to a single strip during an incident -- the slow loop does not
 * stop when a fire starts, and a rail that vanished would imply it had.
 */

import { StatusPill } from '@/components/StatusPill';
import type { AgentDescriptorView, SubscriptionView } from '@/lib/api/types';

export interface AgentActivity {
  /** Runs completed in the current window. */
  throughput: number;
  /** What it is doing right now, in one line. */
  current: string | null;
}

function latency(ms: number): string {
  return ms >= 1000 ? `${Math.round(ms / 1000)}s budget` : `${ms}ms budget`;
}

export function AgentRail({
  agents,
  subscriptions,
  activity = {},
  compressed = false,
  loop,
}: {
  agents: AgentDescriptorView[];
  subscriptions: SubscriptionView[];
  activity?: Record<string, AgentActivity>;
  compressed?: boolean;
  loop?: 'SLOW' | 'INCIDENT';
}) {
  const pinned = new Map(subscriptions.map((s) => [s.agent_id, s.pinned_version]));
  const inLoop = loop ? agents.filter((a) => a.loop === loop) : agents;

  // Superseded agents stay in the catalog on purpose: a brief recorded two
  // years ago names the agent version that produced it, and an id deleted
  // from the registry turns that record into a reference to something this
  // build has never heard of. But they are not the running fleet, and a rail
  // that shows a retired agent as "idle" says the department runs thirteen
  // agents when it schedules nine.
  const shown = inLoop.filter((a) => !a.deprecated_at);
  const superseded = inLoop.filter((a) => a.deprecated_at);

  if (shown.length === 0 && superseded.length === 0) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        No agents published. The registry reported an empty catalog.
      </p>
    );
  }

  if (compressed) {
    return (
      <ul className="flex flex-wrap gap-1.5" aria-label="Slow-loop fleet, still running">
        {shown.map((agent) => (
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

  return (
    <ul className="space-y-2">
      {shown.map((agent) => {
        const running = activity[agent.agent_id];
        const pin = pinned.get(agent.agent_id);
        const drifted = pin !== undefined && pin !== agent.version;
        return (
          <li key={agent.ref} className="border border-line bg-surface p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-mono text-ink">{agent.agent_id}</span>
              <StatusPill
                tone={running?.current ? 'live' : 'muted'}
                label={running?.current ? 'running' : 'idle'}
              />
            </div>
            <p className="mt-1 text-micro leading-5 text-muted">{agent.role_summary}</p>
            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-micro">
              <dt className="text-muted">Publisher</dt>
              <dd className="text-ink">{agent.publisher_department}</dd>
              <dt className="text-muted">Pinned</dt>
              <dd className={drifted ? 'text-disputed' : 'text-ink'}>
                {pin ? `@${pin}` : 'not subscribed'}
                {drifted && ` (catalog has ${agent.version})`}
              </dd>
              <dt className="text-muted">Throughput</dt>
              <dd className="text-ink">{running ? `${running.throughput} runs` : '0 runs'}</dd>
              <dt className="text-muted">Target</dt>
              <dd className="text-ink">{latency(agent.latency_target_ms)}</dd>
            </dl>
            {running?.current && (
              <p className="mt-2 border-l-2 border-live pl-2 text-micro text-ink">
                {running.current}
              </p>
            )}
            {agent.write_targets.length > 0 && (
              <p className="mt-2 text-micro text-muted">
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
      })}

      {superseded.length > 0 && (
        <li className="border border-dashed border-line p-3" data-testid="superseded-agents">
          <p className="text-micro uppercase tracking-widest text-muted">
            Superseded · still catalogued
          </p>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {superseded.map((agent) => (
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
            Not scheduled and given no worker. They stay resolvable because a brief recorded
            two years ago names the agent version that produced it, and an id deleted from the
            catalog would make that record unreadable.
          </p>
        </li>
      )}
    </ul>
  );
}
