/**
 * The activity and audit stream.
 *
 * Timestamped, newest first, and every line names the actor. This is the
 * surface where "what has the fleet been doing" is answerable without opening
 * a log file, and where an injection block or a refused write is visible to the
 * person on shift rather than only to whoever reads the audit collection later.
 */

import { StatusPill } from '@/components/StatusPill';
import type { AuditEventView, PolicyDecisionView } from '@/lib/api/types';

/** Content hashes shortened to a matchable prefix.
 *
 * Deliberately narrow: only a long run of pure hex is shortened, because that
 * is a digest and nothing else. Readable identifiers -- `local-injection-
 * detector/1`, `act_wo_queue_sffd-district-03_sf-0450-hayes` -- are what an
 * officer actually reads off this surface, and truncating those would trade
 * one kind of unreadability for a worse one.
 *
 * Exported because the audit console renders the same detail maps and must
 * shorten them the same way; two copies would drift.
 */
export function summarize(value: string): string {
  return /^[0-9a-f]{32,}$/i.test(value) ? `${value.slice(0, 12)}…` : value;
}

export type StreamItem =
  | { kind: 'audit'; at: string; event: AuditEventView }
  | { kind: 'decision'; at: string; decision: PolicyDecisionView };

const NOTABLE: Record<string, { tone: 'alarm' | 'disputed' | 'confirmed' | 'muted'; label: string }> =
  {
    injection_blocked: { tone: 'alarm', label: 'injection blocked' },
    screen_unavailable: { tone: 'disputed', label: 'screen unavailable' },
    model_output_rejected: { tone: 'disputed', label: 'model output rejected' },
    write_executed: { tone: 'confirmed', label: 'external write' },
    write_replayed: { tone: 'muted', label: 'write replayed' },
    approval_granted: { tone: 'confirmed', label: 'approval granted' },
    grant_minted: { tone: 'muted', label: 'grant minted' },
    grant_revoked: { tone: 'muted', label: 'grant revoked' },
    emergency_exception: { tone: 'alarm', label: 'emergency exception' },
    dead_lettered: { tone: 'alarm', label: 'dead lettered' },
    rms_flushed: { tone: 'muted', label: 'records flushed' },
  };

const ACTION_TONE: Record<string, 'confirmed' | 'disputed' | 'alarm' | 'muted'> = {
  ALLOW: 'confirmed',
  DERIVE: 'muted',
  WITHHOLD_JURISDICTION: 'disputed',
  REQUIRE_APPROVAL: 'disputed',
  DENY: 'alarm',
};

function clock(at: string): string {
  return at.slice(11, 19) || at;
}

export function toStreamItems(
  events: AuditEventView[],
  decisions: PolicyDecisionView[],
): StreamItem[] {
  const items: StreamItem[] = [
    ...events.map((event) => ({ kind: 'audit' as const, at: event.occurred_at, event })),
    ...decisions.map((decision) => ({
      kind: 'decision' as const,
      at: decision.decided_at,
      decision,
    })),
  ];
  // Newest first: on a fireground the last thing that happened is the thing
  // somebody is asking about.
  return items.sort((a, b) => b.at.localeCompare(a.at));
}

export function ActivityStream({ items, limit = 12 }: { items: StreamItem[]; limit?: number }) {
  if (items.length === 0) {
    return (
      <p className="border border-dashed border-line p-4 text-micro leading-5 text-muted">
        Nothing recorded yet. This stream shows policy decisions, injection
        blocks, external writes, approvals, and grant activity as they happen.
      </p>
    );
  }

  return (
    <ol className="space-y-1.5" aria-label="Activity and audit stream">
      {items.slice(0, limit).map((item) => {
        if (item.kind === 'audit') {
          const meta = NOTABLE[item.event.kind] ?? { tone: 'muted' as const, label: item.event.kind };
          return (
            <li key={item.event.audit_id} className="border border-line bg-surface p-2">
              <div className="flex flex-wrap items-center gap-2">
                <time className="font-mono text-micro text-muted" dateTime={item.at}>
                  {clock(item.at)}
                </time>
                <StatusPill tone={meta.tone} label={meta.label} />
                <span className="font-mono text-micro text-ink">{item.event.actor}</span>
                {item.event.target && (
                  <span className="text-micro text-muted">→ {item.event.target}</span>
                )}
              </div>
              {Object.keys(item.event.detail).length > 0 && (
                // Hashes and ids are the provenance and they matter -- but a
                // 64-character digest rendered in full turns the one surface
                // an officer glances at into a wall. Truncated to a prefix
                // that is still enough to match against the audit record.
                <p className="mt-1 break-words font-mono text-micro text-muted">
                  {Object.entries(item.event.detail)
                    .map(([key, value]) => `${key}=${summarize(String(value))}`)
                    .join('  ')}
                </p>
              )}
            </li>
          );
        }

        const decision = item.decision;
        return (
          <li key={decision.decision_id} className="border border-line bg-surface p-2">
            <div className="flex flex-wrap items-center gap-2">
              <time className="font-mono text-micro text-muted" dateTime={item.at}>
                {clock(item.at)}
              </time>
              <StatusPill
                tone={ACTION_TONE[decision.action] ?? 'muted'}
                label={decision.action.toLowerCase().replace(/_/g, ' ')}
              />
              <span className="font-mono text-micro text-ink">{decision.agent_id}</span>
              <span className="text-micro text-muted">→ {decision.target}</span>
            </div>
            <p className="mt-1 text-micro leading-5 text-ink">{decision.justification}</p>
            <p className="mt-0.5 font-mono text-micro text-muted">
              {decision.rule_id} · policy {decision.policy_version} · {decision.decided_by}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
