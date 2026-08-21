'use client';

/**
 * The ranked survey queue.
 *
 * Every row carries the reasons that produced it, inline and expandable. That
 * is not a nicety: a chief who disagrees with row three has to be able to see
 * exactly which rule put it there, and a queue that only showed a score would
 * be asking them to trust an arithmetic they cannot check.
 */

import { useState } from 'react';

import { StatusPill } from '@/components/StatusPill';
import type { QueueEntryView } from '@/lib/api/types';

function statusTone(status: string) {
  if (status === 'DISPATCHED') return 'live' as const;
  if (status === 'SURVEYED') return 'confirmed' as const;
  return 'muted' as const;
}

export function SurveyQueue({
  entries,
  onSelect,
  selectedAddressId,
}: {
  entries: QueueEntryView[];
  onSelect?: (addressId: string) => void;
  selectedAddressId?: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(entries[0]?.entry_id ?? null);

  if (entries.length === 0) {
    return (
      <div className="border border-dashed border-line p-6 text-muted">
        <p className="text-ink">No ranked structures yet</p>
        <p className="mt-1 max-w-prose text-micro leading-5">
          The delta ranker builds this queue from accumulated conflicts,
          confidence decay, source churn, and survey age. Until a district has
          been polled this stays empty rather than showing invented rows.
        </p>
      </div>
    );
  }

  return (
    <ol className="space-y-2" aria-label="Ranked survey queue">
      {entries.map((entry) => {
        const open = expanded === entry.entry_id;
        const selected = selectedAddressId === entry.address_id;
        return (
          <li
            key={entry.entry_id}
            className={`border bg-surface ${selected ? 'border-live' : 'border-line'}`}
          >
            <div className="flex flex-wrap items-center gap-2 p-3">
              <span className="font-mono text-xl text-ink" aria-label={`Rank ${entry.rank}`}>
                {entry.rank}
              </span>
              <button
                type="button"
                onClick={() => onSelect?.(entry.address_id)}
                className="flex-1 text-left font-mono text-ink underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {entry.address_id}
              </button>
              <span className="font-mono text-micro text-muted">
                score {entry.score.toFixed(3)}
              </span>
              <StatusPill tone={statusTone(entry.status)} label={entry.status.toLowerCase()} />
              <button
                type="button"
                aria-expanded={open}
                aria-controls={`reasons-${entry.entry_id}`}
                onClick={() => setExpanded(open ? null : entry.entry_id)}
                className="border border-line px-2 py-0.5 text-micro uppercase tracking-wide text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {open ? 'Hide why' : 'Why'}
              </button>
            </div>

            <div
              id={`reasons-${entry.entry_id}`}
              hidden={!open}
              className="border-t border-line px-3 py-2"
            >
              <ul className="space-y-1.5">
                {entry.reasons.map((reason) => (
                  <li key={`${entry.entry_id}-${reason.rule_id}`} className="text-micro">
                    <span className="font-mono text-muted">{reason.rule_id}</span>
                    <span className="mx-1 text-muted">·</span>
                    <span className="text-ink">{reason.detail}</span>
                    <span className="ml-1 text-muted">
                      (weight {reason.weight.toFixed(2)})
                    </span>
                  </li>
                ))}
              </ul>
              {entry.assigned_company && (
                <p className="mt-2 text-micro text-muted">
                  Assigned to {entry.assigned_company}
                  {entry.calendar_event_ref && ` · calendar ${entry.calendar_event_ref}`}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
