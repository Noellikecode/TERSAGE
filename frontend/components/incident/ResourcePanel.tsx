'use client';

/**
 * Notifications and staged approval cards.
 *
 * The split on this screen mirrors the one in the gateway, and the screen is
 * not where it is decided. Telling the water department goes out; cutting their
 * gas comes back as a card that says exactly what will happen if a chief taps
 * it. If this component had the list wrong, the backend would still refuse --
 * which is the point of the check living there.
 */

import { StatusPill } from '@/components/StatusPill';
import type { ResourceOutcomeView } from '@/lib/api/types';

export const NOTIFICATIONS = [
  { id: 'water-supply', label: 'Water department' },
  { id: 'public-works', label: 'Public works' },
  { id: 'exposure', label: 'Exposure address' },
  { id: 'building-department', label: 'Building department' },
] as const;

export const COMMITMENTS = [
  { id: 'gas-shutoff', label: 'Gas shutoff' },
  { id: 'electric-shutoff', label: 'Electric shutoff' },
  { id: 'road-closure', label: 'Road closure (PD)' },
  { id: 'hazmat-team', label: 'Hazmat team' },
  { id: 'collapse-rescue', label: 'Collapse rescue' },
] as const;

const ACTION_TONE = {
  ALLOW: 'confirmed',
  DERIVE: 'muted',
  WITHHOLD_JURISDICTION: 'disputed',
  REQUIRE_APPROVAL: 'disputed',
  DENY: 'alarm',
} as const;

export function ResourcePanel({
  outcomes,
  onRequest,
  onApprove,
  busy = false,
}: {
  outcomes: ResourceOutcomeView[];
  onRequest?: (kindId: string) => void;
  onApprove?: (approvalId: string) => void;
  busy?: boolean;
}) {
  const byKind = new Map(outcomes.map((outcome) => [outcome.kind_id, outcome]));
  const staged = outcomes.filter((o) => o.action === 'REQUIRE_APPROVAL' && o.approval_id);

  return (
    <div className="space-y-3">
      <section aria-labelledby="notify-heading">
        <h3 id="notify-heading" className="text-micro uppercase tracking-widest text-muted">
          Notify — autonomous
        </h3>
        <p className="mt-1 text-micro leading-5 text-muted">
          Informing an agency that remains free to act or not.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {NOTIFICATIONS.map((kind) => {
            const outcome = byKind.get(kind.id);
            return (
              <button
                key={kind.id}
                type="button"
                disabled={busy || Boolean(outcome?.external_ref)}
                onClick={() => onRequest?.(kind.id)}
                className="border border-line px-3 py-1 text-micro text-ink disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {kind.label}
                {outcome?.external_ref && (
                  <span className="ml-2 text-confirmed">sent {outcome.external_ref}</span>
                )}
              </button>
            );
          })}
        </div>
      </section>

      <section aria-labelledby="commit-heading">
        <h3 id="commit-heading" className="text-micro uppercase tracking-widest text-muted">
          Commit — requires a human
        </h3>
        <p className="mt-1 text-micro leading-5 text-muted">
          Spending another agency&apos;s resources. Staged and prefilled; the
          gateway is what requires the tap, not this screen.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {COMMITMENTS.map((kind) => {
            const outcome = byKind.get(kind.id);
            return (
              <button
                key={kind.id}
                type="button"
                disabled={busy || Boolean(outcome)}
                onClick={() => onRequest?.(kind.id)}
                className="border border-disputed px-3 py-1 text-micro text-disputed disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {kind.label}
                {outcome && <span className="ml-2">{outcome.action.toLowerCase()}</span>}
              </button>
            );
          })}
        </div>
      </section>

      {staged.length > 0 && (
        <section aria-labelledby="staged-heading" className="space-y-2">
          <h3 id="staged-heading" className="text-micro uppercase tracking-widest text-disputed">
            Awaiting approval
          </h3>
          {staged.map((outcome) => (
            <article
              key={outcome.approval_id}
              className="border border-disputed bg-surface p-3"
              aria-label={`Staged approval: ${outcome.kind_id}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill tone="disputed" label="needs approval" />
                <span className="font-mono text-ink">{outcome.kind_id}</span>
              </div>
              <p className="mt-1 font-mono text-micro text-muted">
                {outcome.rule_id} · decision {outcome.decision_id}
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => outcome.approval_id && onApprove?.(outcome.approval_id)}
                className="mt-2 border border-confirmed px-3 py-1 text-micro uppercase tracking-wide text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                Approve and send
              </button>
            </article>
          ))}
        </section>
      )}

      {outcomes.filter((o) => o.external_ref).length > 0 && (
        <section aria-labelledby="sent-heading">
          <h3 id="sent-heading" className="text-micro uppercase tracking-widest text-muted">
            Sent
          </h3>
          <ul className="mt-1 space-y-1">
            {outcomes
              .filter((o) => o.external_ref)
              .map((outcome) => (
                <li key={outcome.decision_id} className="flex flex-wrap items-center gap-2">
                  <StatusPill tone={ACTION_TONE[outcome.action]} label={outcome.action.toLowerCase()} />
                  <span className="font-mono text-micro text-ink">{outcome.kind_id}</span>
                  <span className="font-mono text-micro text-muted">{outcome.external_ref}</span>
                </li>
              ))}
          </ul>
        </section>
      )}
    </div>
  );
}
