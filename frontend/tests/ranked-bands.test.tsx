/**
 * The survey queue, grouped by score.
 *
 * The failure this prevents is a claim, not a layout. A real district poll
 * produces six distinct scores across a hundred structures, and ninety-five of
 * them tie at the bottom on identical reasons. The old panel numbered them 1
 * through 100, which told a captain that 47 outranked 48. Nothing separates
 * them. These tests pin that the console says so.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RankedBands, bandsOf, ruleLabel } from '@/components/standby/RankedBands';
import type { QueueEntryView } from '@/lib/api/types';

function entry(
  rank: number,
  score: number,
  rules: string[] = ['rank.never-surveyed'],
  status = 'RANKED',
): QueueEntryView {
  return {
    entry_id: `queue_sf-${rank}`,
    address_id: `sf-${String(rank).padStart(4, '0')}-hyde`,
    rank,
    score,
    status,
    reasons: rules.map((rule_id) => ({
      rule_id,
      detail: `${rule_id} fired`,
      weight: 0.5,
      canonical_key: null,
      conflict_id: null,
    })),
    assigned_company: null,
    dispatched_at: null,
    calendar_event_ref: null,
    survey_id: null,
  } as QueueEntryView;
}

/** The real shape: one clear winner, a couple of near ones, a long tie. */
const REAL: QueueEntryView[] = [
  entry(1, 0.877, ['rank.open-conflict-severity', 'rank.never-surveyed'], 'DISPATCHED'),
  entry(2, 0.736, ['rank.open-conflict-severity']),
  entry(3, 0.57), entry(4, 0.57), entry(5, 0.57),
  ...Array.from({ length: 95 }, (_, i) =>
    entry(6 + i, 0.491, ['rank.never-surveyed', 'rank.confidence-decay', 'rank.source-churn']),
  ),
];

describe('grouping', () => {
  it('collapses a tie into one band and leaves distinct scores alone', () => {
    const bands = bandsOf(REAL);
    expect(bands.map((b) => [b.score, b.entries.length])).toEqual([
      ['0.88', 1],
      ['0.74', 1],
      ['0.57', 3],
      ['0.49', 95],
    ]);
  });

  it('names only the rules that fired for every entry in the band', () => {
    // A rule that fired for some of them does not explain the band, and
    // printing it as though it did is the same overstatement as the numbering.
    const mixed = [
      entry(1, 0.5, ['rank.never-surveyed', 'rank.source-churn']),
      entry(2, 0.5, ['rank.never-surveyed']),
    ];
    expect(bandsOf(mixed)[0]!.sharedRules).toEqual(['rank.never-surveyed']);
  });

  it('keeps the ranker’s order rather than regrouping across it', () => {
    // Same score either side of a different one. Describing the ranking means
    // leaving it alone, not gathering equal scores that were not adjacent.
    const bands = bandsOf([entry(1, 0.5), entry(2, 0.4), entry(3, 0.5)]);
    expect(bands.map((b) => b.entries.length)).toEqual([1, 1, 1]);
  });

  it('reads a rule id as words, without a hand-maintained map', () => {
    // A rule added to the ranker is readable here with no edit.
    expect(ruleLabel('rank.confidence-decay')).toBe('confidence decay');
    expect(ruleLabel('rank.open-conflict-severity')).toBe('open conflict severity');
  });
});

describe('what the panel says', () => {
  it('states a tie as a tie, once, instead of numbering it', () => {
    render(<RankedBands entries={REAL} />);

    const tail = screen.getByTestId('band-0.49');
    expect(tail).toHaveTextContent('95 structures, tied');
    expect(tail).toHaveTextContent(/Nothing separates them; ordered by address/);
    // Once for the band, not once per structure.
    expect(screen.getAllByText(/Nothing separates/)).toHaveLength(2);
  });

  it('folds a long tie and opens it on request', () => {
    render(<RankedBands entries={REAL} />);

    // Ninety-five addresses is a reference list, not a decision.
    expect(screen.queryByRole('button', { name: 'sf-0050-hyde' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('band-toggle-0.49'));
    expect(screen.getByRole('button', { name: 'sf-0050-hyde' })).toBeInTheDocument();
  });

  it('shows a small band without asking', () => {
    render(<RankedBands entries={REAL} />);
    const three = screen.getByTestId('band-0.57');
    expect(within(three).getAllByRole('button')).toHaveLength(3);
    expect(screen.queryByTestId('band-toggle-0.57')).not.toBeInTheDocument();
  });

  it('says one structure is one structure, and does not call it tied', () => {
    render(<RankedBands entries={REAL} />);
    const top = screen.getByTestId('band-0.88');
    expect(top).toHaveTextContent('1 structure');
    expect(top).not.toHaveTextContent('tied');
  });

  it('keeps a status that is not the default visible', () => {
    render(<RankedBands entries={REAL} />);
    expect(within(screen.getByTestId('band-0.88')).getByText('dispatched')).toBeInTheDocument();
  });

  it('opens a structure when its address is clicked', () => {
    const onSelect = vi.fn();
    render(<RankedBands entries={REAL} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: 'sf-0002-hyde' }));
    expect(onSelect).toHaveBeenCalledWith('sf-0002-hyde');
  });

  it('marks the structure currently open', () => {
    render(<RankedBands entries={REAL} selectedAddressId="sf-0002-hyde" />);
    const chip = screen.getByRole('button', { name: 'sf-0002-hyde' }).closest('li');
    expect(chip?.className).toContain('border-live');
  });

  it('says there is nothing rather than drawing an empty list', () => {
    render(<RankedBands entries={[]} />);
    expect(screen.getByText(/No ranked structures yet/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Ranked structures')).not.toBeInTheDocument();
  });
});
