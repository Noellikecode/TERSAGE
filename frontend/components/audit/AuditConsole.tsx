'use client';

/**
 * The audit console.
 *
 * Filters over policy decisions, injection blocks, and external writes, plus
 * incident replay and version comparison. What an investigator opens when the
 * question is "why was this allowed" or "what did the commander see".
 *
 * Nothing here can leak a record: the detail was redacted on the way in, so
 * even a caller entitled to read the audit log gets field names and hashes.
 */

import { useMemo, useState } from 'react';

import { summarize } from '@/components/standby/ActivityStream';
import { StatusPill } from '@/components/StatusPill';
import type {
  AuditEventView,
  BriefEmissionView,
  IncidentReplayView,
  IncidentLogView,
  PolicyDecisionView,
} from '@/lib/api/types';

const ACTIONS = ['ALL', 'ALLOW', 'DERIVE', 'WITHHOLD_JURISDICTION', 'REQUIRE_APPROVAL', 'DENY'];
const KINDS = [
  'ALL',
  'injection_blocked',
  'model_output_rejected',
  'write_executed',
  'approval_granted',
  'grant_minted',
  'grant_revoked',
];

const ACTION_TONE = {
  ALLOW: 'confirmed',
  DERIVE: 'muted',
  WITHHOLD_JURISDICTION: 'disputed',
  REQUIRE_APPROVAL: 'disputed',
  DENY: 'alarm',
} as const;

/** What changed between two brief versions, field by field. */
export function diffEmissions(a: BriefEmissionView, b: BriefEmissionView): string[] {
  const changes: string[] = [];
  if (a.stage !== b.stage) changes.push(`stage ${a.stage} → ${b.stage}`);
  if (a.narrative_available !== b.narrative_available) {
    changes.push(
      `narrative ${a.narrative_available ? 'present' : 'absent'} → ${
        b.narrative_available ? 'present' : 'absent'
      }`,
    );
  }
  if (a.unknowns.length !== b.unknowns.length) {
    changes.push(`unknowns ${a.unknowns.length} → ${b.unknowns.length}`);
  }
  if (a.conflict_ids.length !== b.conflict_ids.length) {
    changes.push(`conflicts ${a.conflict_ids.length} → ${b.conflict_ids.length}`);
  }
  const sectionsA = a.sections.length;
  const sectionsB = b.sections.length;
  if (sectionsA !== sectionsB) changes.push(`sections ${sectionsA} → ${sectionsB}`);
  if (a.content_hash !== b.content_hash) {
    changes.push(`content hash ${a.content_hash.slice(0, 8)} → ${b.content_hash.slice(0, 8)}`);
  }
  return changes.length > 0 ? changes : ['no field-level differences'];
}

export function AuditConsole({
  events,
  decisions,
  log,
  emissions,
  replay,
  onReplay,
  replayBusy = false,
}: {
  events: AuditEventView[];
  decisions: PolicyDecisionView[];
  /** The ordered incident record, when an incident has run. */
  log: IncidentLogView | null;
  emissions: BriefEmissionView[];
  /** The incident reconstructed from its own record. Null until requested. */
  replay?: IncidentReplayView | null;
  onReplay?: () => void;
  replayBusy?: boolean;
}) {
  const [action, setAction] = useState('ALL');
  const [kind, setKind] = useState('ALL');
  const [compareA, setCompareA] = useState<number>(emissions[0]?.version ?? 1);
  const [compareB, setCompareB] = useState<number>(
    emissions[emissions.length - 1]?.version ?? 1,
  );

  const filteredDecisions = useMemo(
    () => decisions.filter((d) => action === 'ALL' || d.action === action),
    [decisions, action],
  );
  const filteredEvents = useMemo(
    () => events.filter((e) => kind === 'ALL' || e.kind === kind),
    [events, kind],
  );

  const left = emissions.find((e) => e.version === compareA);
  const right = emissions.find((e) => e.version === compareB);

  return (
    <div className="space-y-4">
      <section aria-labelledby="decisions-heading">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="decisions-heading" className="text-micro uppercase tracking-widest text-muted">
            Policy decisions
          </h3>
          <label className="text-micro text-muted">
            <span className="sr-only">Filter decisions by outcome</span>
            <select
              value={action}
              onChange={(event) => setAction(event.target.value)}
              className="border border-line bg-ground px-2 py-0.5 text-micro text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              {ACTIONS.map((option) => (
                <option key={option} value={option}>
                  {option.toLowerCase().replace(/_/g, ' ')}
                </option>
              ))}
            </select>
          </label>
          <span className="text-micro text-muted">{filteredDecisions.length} shown</span>
        </div>

        {filteredDecisions.length === 0 ? (
          <p className="mt-2 border border-dashed border-line p-3 text-micro text-muted">
            No decisions match this filter.
          </p>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {filteredDecisions.slice(0, 50).map((decision) => (
              <li key={decision.decision_id} className="border border-line bg-surface p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill
                    tone={ACTION_TONE[decision.action as keyof typeof ACTION_TONE] ?? 'muted'}
                    label={decision.action.toLowerCase().replace(/_/g, ' ')}
                  />
                  <span className="font-mono text-micro text-ink">{decision.agent_id}</span>
                  <span className="text-micro text-muted">→ {decision.target}</span>
                  <span className="ml-auto font-mono text-micro text-muted">
                    {decision.classification}
                  </span>
                </div>
                <p className="mt-1 text-micro leading-5 text-ink">{decision.justification}</p>
                <p className="mt-0.5 font-mono text-micro text-muted">
                  {decision.rule_id} · policy {decision.policy_version} · decided by{' '}
                  {decision.decided_by}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="events-heading">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="events-heading" className="text-micro uppercase tracking-widest text-muted">
            Audit events
          </h3>
          <label className="text-micro text-muted">
            <span className="sr-only">Filter events by kind</span>
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value)}
              className="border border-line bg-ground px-2 py-0.5 text-micro text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              {KINDS.map((option) => (
                <option key={option} value={option}>
                  {option.replace(/_/g, ' ')}
                </option>
              ))}
            </select>
          </label>
          <span className="text-micro text-muted">{filteredEvents.length} shown</span>
        </div>

        {filteredEvents.length === 0 ? (
          <p className="mt-2 border border-dashed border-line p-3 text-micro text-muted">
            No audit events match this filter.
          </p>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {filteredEvents.slice(0, 50).map((event) => (
              <li key={event.audit_id} className="border border-line bg-surface p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill
                    tone={event.kind === 'injection_blocked' ? 'alarm' : 'muted'}
                    label={event.kind.replace(/_/g, ' ')}
                  />
                  <span className="font-mono text-micro text-ink">{event.actor}</span>
                  {event.target && <span className="text-micro text-muted">→ {event.target}</span>}
                </div>
                <p className="mt-1 break-words font-mono text-micro text-muted">
                  {Object.entries(event.detail)
                    .map(([key, value]) => `${key}=${summarize(String(value))}`)
                    .join('  ')}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="log-heading">
        <h3 id="log-heading" className="text-micro uppercase tracking-widest text-muted">
          Incident log
        </h3>
        {log ? (
          <>
            <p className="mt-1 text-micro text-muted">
              {log.entries.length} entries
              {log.sealed_at ? ` · sealed ${log.sealed_at.slice(11, 19)}` : ' · not sealed'}
              {log.unflushed > 0 && (
                <span className="text-disputed"> · {log.unflushed} buffered for records</span>
              )}
            </p>
            <ol className="mt-2 space-y-1">
              {log.entries.map((entry) => (
                <li key={entry.sequence} className="flex flex-wrap gap-2 text-micro">
                  <span className="font-mono text-muted">
                    {String(entry.sequence).padStart(3, '0')}
                  </span>
                  <time className="font-mono text-muted" dateTime={entry.occurred_at}>
                    {entry.occurred_at.slice(11, 19)}
                  </time>
                  <span className="text-ink">{entry.entry_type.replace(/_/g, ' ')}</span>
                  <span className="font-mono text-muted">
                    {entry.content_hash.slice(0, 8)}
                  </span>
                </li>
              ))}
            </ol>
          </>
        ) : (
          <p className="mt-2 border border-dashed border-line p-3 text-micro leading-5 text-muted">
            No incident log. Opening an incident produces one, and every brief
            version is written to it before it is displayed.
          </p>
        )}
      </section>

      {/* Replay is not the log. The log is what this process holds; the replay
          is the incident reconstructed from its own record and re-hashed, so
          it can say whether the record was altered. That distinction is the
          reason version pinning exists here at all: a NIOSH investigator two
          years on has to know not just what the commander was told, but that
          nothing has been edited since. */}
      <section aria-labelledby="replay-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 id="replay-heading" className="text-micro uppercase tracking-widest text-muted">
            Replay from the record
          </h3>
          {onReplay && (
            <button
              type="button"
              disabled={replayBusy}
              onClick={onReplay}
              className="border border-line px-2 py-1 text-micro uppercase tracking-wide text-muted hover:text-ink disabled:opacity-50"
              data-testid="replay-button"
            >
              {replayBusy ? 'Replaying…' : 'Replay incident'}
            </button>
          )}
        </div>

        {!replay ? (
          <p className="mt-2 border border-dashed border-line p-3 text-micro leading-5 text-muted">
            Not replayed. A replay re-reads the sealed record and re-computes every
            content hash, so it answers a different question from the log above:
            whether what is stored is still what was written.
          </p>
        ) : (
          <div className="mt-2" data-testid="replay-result">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill
                tone={replay.intact ? 'confirmed' : 'alarm'}
                label={replay.intact ? 'record intact' : 'record altered'}
              />
              <span className="font-mono text-micro text-muted">
                digest {replay.digest.slice(0, 12)}
              </span>
              <span className="text-micro text-muted">{replay.entries.length} entries</span>
            </div>

            {!replay.intact && (
              <p className="mt-2 border border-alarm/40 bg-alarm/10 p-2 text-micro text-alarm">
                Sequences {replay.tampered_sequences.join(', ')} do not match their recorded
                hash. Those entries cannot be relied on.
              </p>
            )}

            <dl className="mt-2 space-y-1 text-micro">
              <div className="flex flex-wrap gap-2">
                <dt className="uppercase tracking-widest text-muted">agent versions</dt>
                <dd className="font-mono text-ink">
                  {Object.entries(replay.agent_versions)
                    .map(([agent, version]) => `${agent}@${version}`)
                    .join('  ') || 'none recorded'}
                </dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="uppercase tracking-widest text-muted">policy versions</dt>
                <dd className="font-mono text-ink">
                  {replay.policy_versions.join(', ') || 'none recorded'}
                </dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="uppercase tracking-widest text-muted">profile snapshot</dt>
                <dd className="font-mono text-ink">
                  {replay.profile_snapshot_id}
                  {/* An unavailable snapshot means the brief cannot be
                      re-derived, only re-read. Say which. */}
                  <span className={replay.snapshot_available ? 'text-confirmed' : 'text-disputed'}>
                    {replay.snapshot_available ? ' · available' : ' · no longer available'}
                  </span>
                </dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="uppercase tracking-widest text-muted">sealed</dt>
                <dd className="font-mono text-ink">
                  {replay.sealed_at ?? 'not sealed — the incident is still open'}
                </dd>
              </div>
            </dl>
          </div>
        )}
      </section>

      {emissions.length > 1 && (
        <section aria-labelledby="compare-heading">
          <h3 id="compare-heading" className="text-micro uppercase tracking-widest text-muted">
            Compare brief versions
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <label className="text-micro text-muted">
              <span className="sr-only">First version to compare</span>
              <select
                value={compareA}
                onChange={(event) => setCompareA(Number(event.target.value))}
                className="border border-line bg-ground px-2 py-0.5 text-micro text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {emissions.map((e) => (
                  <option key={e.version} value={e.version}>
                    v{e.version} {e.stage.toLowerCase()}
                  </option>
                ))}
              </select>
            </label>
            <span aria-hidden="true" className="text-muted">
              →
            </span>
            <label className="text-micro text-muted">
              <span className="sr-only">Second version to compare</span>
              <select
                value={compareB}
                onChange={(event) => setCompareB(Number(event.target.value))}
                className="border border-line bg-ground px-2 py-0.5 text-micro text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {emissions.map((e) => (
                  <option key={e.version} value={e.version}>
                    v{e.version} {e.stage.toLowerCase()}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <ul className="mt-2 space-y-1" data-testid="version-diff">
            {left && right ? (
              diffEmissions(left, right).map((change) => (
                <li key={change} className="font-mono text-micro text-ink">
                  {change}
                </li>
              ))
            ) : (
              <li className="text-micro text-muted">Select two versions.</li>
            )}
          </ul>
        </section>
      )}
    </div>
  );
}
