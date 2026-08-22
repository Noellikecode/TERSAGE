'use client';

/**
 * Conflicts, referrals, and the control that settles one.
 *
 * A conflict is closed by observation, not by a newer document -- so the
 * control here records what somebody saw, and it asks who saw it. There is no
 * "resolve in favour of the permit" button, because that decision is not one
 * this console is entitled to offer.
 */

import { useState } from 'react';

import { StatusPill } from '@/components/StatusPill';
import type { ConflictView, ReferralSummary } from '@/lib/api/types';

export interface ResolutionSubmission {
  conflictId: string;
  observedValue: string;
  resolvedBy: string;
  note: string;
}

export function ConflictPanel({
  conflicts,
  referrals,
  onResolve,
  onStageReferral,
  onApproveReferral,
  busy = false,
  disabledReason,
}: {
  conflicts: ConflictView[];
  referrals: ReferralSummary[];
  onResolve?: (submission: ResolutionSubmission) => void;
  /** Draft a referral from a conflict. Staged, never filed, until approved. */
  onStageReferral?: (conflictId: string) => void;
  /** The one human tap. A referral accuses a property owner, so a captain signs it. */
  onApproveReferral?: (referralId: string) => void;
  busy?: boolean;
  /** Set when resolution is not available -- e.g. no incident is open. */
  disabledReason?: string;
}) {
  const [active, setActive] = useState<string | null>(null);
  const [observed, setObserved] = useState('');
  const [who, setWho] = useState('');
  const [note, setNote] = useState('');

  const open = conflicts.filter((c) => c.status === 'OPEN');
  const resolved = conflicts.filter((c) => c.status === 'RESOLVED');

  return (
    <div className="space-y-3">
      {open.length === 0 && resolved.length === 0 && (
        <p className="border border-dashed border-line p-4 text-micro leading-5 text-muted">
          No disagreements recorded. Sources either agree or have not both
          reported -- this is not a statement that they were checked against
          each other and matched.
        </p>
      )}

      {open.map((conflict) => (
        <article key={conflict.conflict_id} className="border border-disputed bg-surface p-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone="disputed" label={`severity ${conflict.severity}`} />
            <span className="font-mono text-micro text-muted">{conflict.canonical_key}</span>
          </div>
          <p className="mt-2 text-ink">{conflict.summary}</p>
          <p className="mt-1 font-mono text-micro text-muted">
            {conflict.rule_id} · facts {conflict.fact_ids.join(', ')}
          </p>
          <p className="mt-1 text-micro text-muted">
            Both records are retained. Only a human observation closes this.
          </p>

          {/* A referral accuses a property owner and commits another agency,
              so the agent drafts it and stops. Staging is not filing. */}
          {onStageReferral && !referrals.some((r) => r.conflict_id === conflict.conflict_id) && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onStageReferral(conflict.conflict_id)}
              className="mt-2 border border-line px-2 py-1 text-micro uppercase tracking-wide text-muted hover:text-ink disabled:opacity-50"
              data-testid={`stage-referral-${conflict.conflict_id}`}
            >
              Draft building-department referral
            </button>
          )}

          {disabledReason ? (
            <p className="mt-3 border-l-2 border-line pl-2 text-micro text-muted">
              {disabledReason}
            </p>
          ) : active === conflict.conflict_id ? (
            <form
              className="mt-3 space-y-2"
              onSubmit={(event) => {
                event.preventDefault();
                onResolve?.({
                  conflictId: conflict.conflict_id,
                  observedValue: observed,
                  resolvedBy: who,
                  note,
                });
              }}
            >
              <div className="grid gap-2 sm:grid-cols-2">
                <label className="block text-micro">
                  <span className="text-muted">What did you observe?</span>
                  <input
                    required
                    value={observed}
                    onChange={(event) => setObserved(event.target.value)}
                    className="mt-1 w-full border border-line bg-ground px-2 py-1 font-mono text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                    placeholder="3"
                  />
                </label>
                <label className="block text-micro">
                  <span className="text-muted">Who observed it?</span>
                  <input
                    required
                    value={who}
                    onChange={(event) => setWho(event.target.value)}
                    className="mt-1 w-full border border-line bg-ground px-2 py-1 font-mono text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                    placeholder="bc-09"
                  />
                </label>
              </div>
              <label className="block text-micro">
                <span className="text-muted">Note (optional)</span>
                <input
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  className="mt-1 w-full border border-line bg-ground px-2 py-1 text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                  placeholder="Walked the Charlie side."
                />
              </label>
              <div className="flex gap-2">
                <button
                  type="submit"
                  disabled={busy}
                  className="border border-confirmed px-3 py-1 text-micro uppercase tracking-wide text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  {busy ? 'Recording…' : 'Record observation'}
                </button>
                <button
                  type="button"
                  onClick={() => setActive(null)}
                  className="border border-line px-3 py-1 text-micro uppercase tracking-wide text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setActive(conflict.conflict_id)}
              className="mt-3 border border-line px-3 py-1 text-micro uppercase tracking-wide text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              Settle on scene
            </button>
          )}
        </article>
      ))}

      {resolved.map((conflict) => (
        <article key={conflict.conflict_id} className="border border-line bg-surface p-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone="confirmed" label="resolved" />
            <span className="font-mono text-micro text-muted">{conflict.canonical_key}</span>
          </div>
          <p className="mt-2 text-muted">{conflict.summary}</p>
          <p className="mt-1 text-micro text-muted">
            Settled by {conflict.resolved_by ?? 'a human observation'}. Both original
            records are still stored.
          </p>
        </article>
      ))}

      {referrals.length > 0 && (
        <section aria-labelledby="referrals-heading" className="border border-line bg-surface p-3">
          <h3 id="referrals-heading" className="text-micro uppercase tracking-widest text-muted">
            Referrals
          </h3>
          <ul className="mt-2 space-y-1">
            {referrals.map((referral) => (
              <li key={referral.referral_id} className="text-micro">
                <div className="flex flex-wrap items-baseline gap-1">
                  <span className="font-mono text-ink">{referral.referral_id}</span>
                  <span className="text-muted">·</span>
                  <span className={referral.case_number ? 'text-confirmed' : 'text-disputed'}>
                    {referral.status}
                  </span>
                  {referral.case_number && (
                    <>
                      <span className="text-muted">·</span>
                      <span className="font-mono text-ink">case {referral.case_number}</span>
                    </>
                  )}
                </div>
                {/* Awaiting approval is the whole point of the referral: an
                    agent drafted it, a captain files it. The control only
                    exists while that is true. */}
                {!referral.case_number && onApproveReferral && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onApproveReferral(referral.referral_id)}
                    className="mt-1 border border-alarm px-2 py-1 text-micro uppercase tracking-wide text-alarm disabled:opacity-50"
                    data-testid={`approve-referral-${referral.referral_id}`}
                  >
                    Approve and file
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
