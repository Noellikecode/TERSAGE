'use client';

/**
 * The survey queue, grouped by what actually separates the structures.
 *
 * This panel used to be a flat grid of numbered chips: `1 sf-0450-hayes`,
 * `2 sf-0415-mission`, on down past ninety. The numbers were the problem, not
 * the chips. In a real district poll there are **six distinct scores across a
 * hundred structures**, and ninety-five of them are tied at the bottom on
 * identical reasons -- never surveyed, confidence decayed, sources churned, no
 * open conflict. Ordering those ninety-five alphabetically and then numbering
 * them 6 through 100 tells a captain that 47 is more urgent than 48. It is not.
 * Nothing separates them, and a queue that implies otherwise is claiming a
 * precision the ranker never produced.
 *
 * So the rows are grouped by score. A band of one is one structure worth
 * looking at on its own. A band of ninety-five is a tie, and it says so, once,
 * with the rules that put every one of them there -- and stays folded until
 * somebody asks, because ninety-five addresses is a reference list, not a
 * decision.
 *
 * The rule ids stay visible. A captain who disagrees with a band has to be able
 * to see which rule put it there; a score with no rule behind it is arithmetic
 * nobody can check.
 */

import { useState } from 'react';

import { StatusPill } from '@/components/StatusPill';
import type { PillTone } from '@/components/StatusPill';
import type { QueueEntryView } from '@/lib/api/types';

/** Bands larger than this fold. Small enough that a fold means something,
 *  large enough that a genuine cluster of five is still just shown. */
const FOLD_ABOVE = 12;

export interface Band {
  /** The score every entry in this band shares, to two places. */
  score: string;
  entries: QueueEntryView[];
  /** Rule ids that fired for *every* entry, which is what makes it a tie. */
  sharedRules: string[];
}

/**
 * Group consecutive entries that share a score.
 *
 * Consecutive rather than by-value: the queue arrives ranked, and a band that
 * pulled together non-adjacent entries would be reordering the ranker's answer
 * rather than describing it.
 */
export function bandsOf(entries: QueueEntryView[]): Band[] {
  const bands: Band[] = [];
  for (const entry of entries) {
    const score = entry.score.toFixed(2);
    const last = bands[bands.length - 1];
    if (last && last.score === score) last.entries.push(entry);
    else bands.push({ score, entries: [entry], sharedRules: [] });
  }
  for (const band of bands) {
    // Only rules that fired for every entry. A rule that fired for some of them
    // does not explain the band, and printing it as though it did would be the
    // same overstatement the numbering was.
    const [first, ...rest] = band.entries;
    const shared = new Set((first?.reasons ?? []).map((r) => r.rule_id));
    for (const entry of rest) {
      const here = new Set(entry.reasons.map((r) => r.rule_id));
      for (const rule of [...shared]) if (!here.has(rule)) shared.delete(rule);
    }
    band.sharedRules = [...shared].sort();
  }
  return bands;
}

/** `rank.confidence-decay` reads as "confidence decay". Derived rather than
 *  mapped, so a rule added to the ranker needs no edit here to be readable. */
export function ruleLabel(ruleId: string): string {
  return ruleId.replace(/^rank\./, '').replace(/-/g, ' ');
}

function tone(status: string): PillTone {
  if (status === 'DISPATCHED') return 'live';
  if (status === 'SURVEYED') return 'confirmed';
  return 'muted';
}

function Chip({
  entry,
  selected,
  onSelect,
}: {
  entry: QueueEntryView;
  selected: boolean;
  onSelect?: (addressId: string) => void;
}) {
  return (
    <li
      className={`flex items-center gap-1.5 border bg-surface px-2 py-1 ${
        selected ? 'border-live' : 'border-line'
      }`}
    >
      <button
        type="button"
        onClick={() => onSelect?.(entry.address_id)}
        className="font-mono text-micro text-ink underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
      >
        {entry.address_id}
      </button>
      {entry.status !== 'RANKED' && (
        <StatusPill tone={tone(entry.status)} label={entry.status.toLowerCase()} />
      )}
    </li>
  );
}

function BandBlock({
  band,
  selectedAddressId,
  onSelect,
}: {
  band: Band;
  selectedAddressId?: string | null;
  onSelect?: (addressId: string) => void;
}) {
  const folds = band.entries.length > FOLD_ABOVE;
  const [open, setOpen] = useState(false);
  const tied = band.entries.length > 1;

  const chips = (
    <ul className="mt-1 flex flex-wrap gap-1" aria-label={`Structures scoring ${band.score}`}>
      {band.entries.map((entry) => (
        <Chip
          key={entry.entry_id}
          entry={entry}
          selected={selectedAddressId === entry.address_id}
          onSelect={onSelect}
        />
      ))}
    </ul>
  );

  return (
    <li data-testid={`band-${band.score}`} data-count={band.entries.length}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-mono text-micro text-ink">{band.score}</span>
        <span className="text-micro text-muted">
          {band.entries.length} {band.entries.length === 1 ? 'structure' : 'structures'}
          {tied && ', tied'}
        </span>
        {band.sharedRules.length > 0 && (
          <span className="font-mono text-micro text-muted">
            · {band.sharedRules.map(ruleLabel).join(' · ')}
          </span>
        )}
      </div>

      {tied && (
        // Said once per band rather than implied ninety-five times by a number.
        <p className="text-micro text-muted">
          Nothing separates {band.entries.length === 2 ? 'these two' : 'them'}; ordered by address.
        </p>
      )}

      {folds ? (
        <>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((was) => !was)}
            data-testid={`band-toggle-${band.score}`}
            className="mt-1 border border-line px-2 py-0.5 text-micro uppercase tracking-wide text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
          >
            {open ? 'Hide' : `Show all ${band.entries.length}`}
          </button>
          {open && chips}
        </>
      ) : (
        chips
      )}
    </li>
  );
}

export function RankedBands({
  entries,
  selectedAddressId,
  onSelect,
}: {
  entries: QueueEntryView[];
  selectedAddressId?: string | null;
  onSelect?: (addressId: string) => void;
}) {
  if (entries.length === 0) {
    return <p className="mt-1 text-micro text-muted">No ranked structures yet</p>;
  }

  return (
    <ol className="mt-1.5 space-y-2" aria-label="Ranked structures">
      {bandsOf(entries).map((band) => (
        <BandBlock
          key={band.score}
          band={band}
          selectedAddressId={selectedAddressId}
          onSelect={onSelect}
        />
      ))}
    </ol>
  );
}
