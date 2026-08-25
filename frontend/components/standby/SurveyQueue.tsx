'use client';

/**
 * The ranked survey queue: which building needs somebody to go and look.
 *
 * This is the answer to the only question standby exists to answer, so it is
 * the one thing on the page allowed to be loud. The address is set large enough
 * to read from a doorway and the rank sits beside it at the same weight;
 * everything else on the row -- the score, the status, the machinery -- is
 * support and is sized like support.
 *
 * **The top row shows its reasons without being asked.** They used to be behind
 * a "Why" button on every row, which meant the default state of the screen was
 * a list of addresses and a number nobody could check. The first row is the one
 * a chief acts on, so its reasoning is the content, not a disclosure. The rest
 * stay collapsed, because five expanded rows is the wall of text this panel was
 * trying to avoid.
 *
 * That every row *can* show its reasons is not a nicety: a chief who disagrees
 * with row three has to be able to see exactly which rule put it there, and a
 * queue that only showed a score would be asking them to trust an arithmetic
 * they cannot check.
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
      <div className="border border-dashed border-line p-6">
        <p className="text-title text-ink">No ranked structures yet</p>
        <p className="mt-1 text-body text-muted">Nothing is invented until a district is polled.</p>
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
            className={`border ${selected ? 'border-live bg-ground' : 'border-line bg-surface'}`}
          >
            <div className="flex flex-wrap items-center gap-3 p-3">
              <span className="font-mono text-title text-muted" aria-label={`Rank ${entry.rank}`}>
                {entry.rank}
              </span>
              <button
                type="button"
                onClick={() => onSelect?.(entry.address_id)}
                className={`flex-1 truncate px-1 text-left font-mono text-title focus-visible:outline focus-visible:outline-2 focus-visible:outline-live ${
                  selected ? 'text-live' : 'text-ink hover:text-live'
                }`}
              >
                {entry.address_id}
              </button>
              <StatusPill tone={statusTone(entry.status)} label={entry.status.toLowerCase()} />
              <span className="font-mono text-body text-muted">{entry.score.toFixed(2)}</span>
              <button
                type="button"
                aria-expanded={open}
                aria-controls={`reasons-${entry.entry_id}`}
                onClick={() => setExpanded(open ? null : entry.entry_id)}
                className="border border-line px-2 py-0.5 text-label uppercase text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                {open ? 'Hide why' : 'Why'}
              </button>
            </div>

            <div
              id={`reasons-${entry.entry_id}`}
              hidden={!open}
              className="border-t border-line px-3 py-2"
            >
              {/* The rule id is what makes a reason checkable, so it stays --
                  but it is evidence, not the sentence. The detail leads. */}
              <ul className="space-y-1.5">
                {entry.reasons.map((reason) => (
                  <li key={`${entry.entry_id}-${reason.rule_id}`} className="text-body">
                    <span className="text-ink">{reason.detail}</span>
                    <span className="ml-2 font-mono text-micro text-muted">
                      {reason.rule_id} · {reason.weight.toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
              {entry.assigned_company && (
                <p className="mt-2 text-body text-muted">
                  {entry.assigned_company}
                  {entry.calendar_event_ref && ` · ${entry.calendar_event_ref}`}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
