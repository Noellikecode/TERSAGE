'use client';

/**
 * What the system found, said in the words a fire officer already uses.
 *
 * This is the panel the whole slow loop exists to produce, and until now it was
 * a number in a stat tile. The finding itself -- *the permit says two storeys
 * and the lidar measures three* -- was three clicks deep, while the machinery
 * that produced it had the top of the screen. That is backwards: an inspector
 * acts on the finding, and nobody outside this repository acts on a pinned
 * agent version.
 *
 * **The building is the unit, not the conflict.** An inspector drives to an
 * address, not to a `conflict_id`. Each card is one structure and names its
 * worst disagreement.
 *
 * **It says what it cannot see.** The ranking cites only the most severe
 * conflict at each structure, so a building with three disagreements
 * contributes one line here. The district's true open count comes from the
 * stats and is printed beside this one whenever the two differ -- because a
 * panel that showed two of four conflicts and said nothing would be the same
 * silent cap this project refuses everywhere else.
 */

import type { QueueEntryView } from '@/lib/api/types';

/** The rule that carries a conflict into the ranking. */
const CONFLICT_RULE = 'rank.open-conflict-severity';

export interface Disagreement {
  addressId: string;
  conflictId: string | null;
  canonicalKey: string | null;
  severity: number | null;
  /** The finding, with the machine preamble stripped. */
  summary: string;
  status: string;
}

/**
 * Pull the findings out of the ranking reasons the console already holds.
 *
 * No new endpoint: the ranker already had to name the conflict to justify the
 * score, so the detail is on the queue entry. Reading it here rather than
 * re-fetching keeps this panel exactly as fresh as the queue beside it.
 */
export function disagreementsIn(entries: QueueEntryView[]): Disagreement[] {
  const found: Disagreement[] = [];
  for (const entry of entries) {
    const reason = entry.reasons.find((r) => r.rule_id === CONFLICT_RULE);
    if (!reason) continue;
    // "Severity 4 conflict open: Permit records 2 storeys; lidar DSM measures 3."
    // The severity becomes a badge, so it does not need saying twice.
    const match = /^Severity\s+(\d+)\s+conflict open:\s*(.*)$/i.exec(reason.detail);
    found.push({
      addressId: entry.address_id,
      conflictId: reason.conflict_id ?? null,
      canonicalKey: reason.canonical_key ?? null,
      severity: match?.[1] ? Number(match[1]) : null,
      summary: (match?.[2] ?? reason.detail).trim(),
      status: entry.status,
    });
  }
  return found.sort((a, b) => (b.severity ?? 0) - (a.severity ?? 0));
}

/** Severity is a number the rules produce; this is what it means to a crew. */
function severityTone(severity: number | null): string {
  if (severity === null) return 'border-line text-muted';
  if (severity >= 4) return 'border-alarm text-alarm';
  if (severity >= 3) return 'border-disputed text-disputed';
  return 'border-line text-muted';
}

export function RecordsDisagree({
  entries,
  /** The district's real open-conflict count, from stats. */
  openConflicts,
  selectedAddressId,
  onSelect,
  headless = false,
}: {
  entries: QueueEntryView[];
  openConflicts?: number | null;
  selectedAddressId?: string | null;
  onSelect?: (addressId: string) => void;
  /**
   * Drop this panel's own landmark and heading, because something outside it
   * already carries both.
   *
   * The console puts this inside a `PanelCard`, which is the labelled region
   * and owns the name. Rendering the section here as well produced two nested
   * regions called "Records disagree" -- ambiguous to anyone navigating by
   * landmark, and caught by the accessibility suite rather than by eye.
   * Standalone -- which is how the tests below render it -- it keeps its own.
   */
  headless?: boolean;
}) {
  const found = disagreementsIn(entries);

  const Frame = headless ? 'div' : 'section';
  const frameProps = headless
    ? {}
    : { 'aria-labelledby': 'disagree-heading' };

  return (
    <Frame
      {...frameProps}
      className="shrink-0 bg-ground px-4 py-3"
      data-testid="records-disagree"
    >
      {!headless && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 id="disagree-heading" className="text-micro uppercase tracking-widest text-muted">
            Records disagree
          </h2>
          <p className="text-micro text-muted">
            Where the paperwork and the measurement do not match. A crew should look.
          </p>
        </div>
      )}

      {found.length === 0 ? (
        <p className="mt-2 text-micro text-muted">
          No structure in this district has an open disagreement.
        </p>
      ) : (
        <>
          <ul className="mt-2 space-y-2" aria-label="Structures where records disagree">
            {found.map((item) => (
              <li
                key={`${item.addressId}-${item.conflictId ?? item.canonicalKey}`}
                data-testid={`disagreement-${item.addressId}`}
                className={`border bg-surface p-3 ${
                  selectedAddressId === item.addressId ? 'border-live' : 'border-line'
                }`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  {/* The same structure is also a chip in the ranked queue
                      below, and both open it. Two buttons with one accessible
                      name is a screen reader reading "sf-0450-hayes" twice with
                      no way to tell them apart, so this one says which it is. */}
                  <button
                    type="button"
                    onClick={() => onSelect?.(item.addressId)}
                    aria-label={`Open ${item.addressId}, records disagree`}
                    className="font-mono text-body text-ink underline-offset-4 hover:text-live hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                  >
                    {item.addressId}
                  </button>
                  {item.severity !== null && (
                    <span
                      className={`border px-2 py-0.5 text-micro uppercase tracking-wide ${severityTone(item.severity)}`}
                    >
                      severity {item.severity}
                    </span>
                  )}
                </div>

                {/* The finding, in the sentence the rule wrote. */}
                <p className="mt-1 text-body text-ink">{item.summary}</p>

                {/* Evidence, small: the attribute in dispute and the id an
                    investigator would quote. Neither is the headline. */}
                <p className="mt-1 font-mono text-micro text-muted">
                  {item.canonicalKey ?? 'attribute not named'}
                  {item.conflictId && ` · ${item.conflictId.slice(0, 16)}`}
                </p>
              </li>
            ))}
          </ul>

          {/* No silent caps. If the district holds more open conflicts than the
              ranking cited, say so rather than letting this list read as all
              of them. */}
          {typeof openConflicts === 'number' && openConflicts > found.length && (
            <p className="mt-2 text-micro text-muted" data-testid="disagreement-shortfall">
              Showing the worst disagreement at each of {found.length}{' '}
              {found.length === 1 ? 'structure' : 'structures'}. The district has {openConflicts}{' '}
              open in total — open a structure to see the rest.
            </p>
          )}
        </>
      )}
    </Frame>
  );
}
