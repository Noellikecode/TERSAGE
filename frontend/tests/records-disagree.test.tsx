/**
 * The finding, said in words a fire officer already uses.
 *
 * The failure this prevents is a product failure rather than a rendering one:
 * the slow loop exists to notice that the permit says two storeys and the lidar
 * measures three, and until this panel that sentence lived three clicks deep
 * while the machinery that produced it had the top of the screen.
 *
 * The other failure is the one this project refuses everywhere else. The
 * ranking cites only the worst conflict at each structure, so this panel can
 * only ever show some of a district's open conflicts. It has to say so.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RecordsDisagree, disagreementsIn } from '@/components/standby/RecordsDisagree';
import type { QueueEntryView } from '@/lib/api/types';

function entry(
  addressId: string,
  reasons: { rule_id: string; detail: string; conflict_id?: string; canonical_key?: string }[],
): QueueEntryView {
  return {
    entry_id: `queue_${addressId}`,
    address_id: addressId,
    rank: 1,
    score: 0.8,
    status: 'RANKED',
    reasons: reasons.map((r) => ({
      rule_id: r.rule_id,
      detail: r.detail,
      weight: 0.8,
      canonical_key: r.canonical_key ?? null,
      conflict_id: r.conflict_id ?? null,
    })),
    assigned_company: null,
    dispatched_at: null,
    calendar_event_ref: null,
    survey_id: null,
  } as QueueEntryView;
}

const HAYES = entry('sf-0450-hayes', [
  {
    rule_id: 'rank.open-conflict-severity',
    detail: 'Severity 4 conflict open: Permit records 2 storeys; lidar DSM measures 3.',
    conflict_id: 'conflict_63aac5a435f47e75ab2c2d97',
    canonical_key: 'structure.stories',
  },
  { rule_id: 'rank.never-surveyed', detail: 'No company survey on record' },
]);

const MISSION = entry('sf-0415-mission', [
  {
    rule_id: 'rank.open-conflict-severity',
    detail: 'Severity 2 conflict open: Filed records disagree on structure.stories: 61 vs 62.',
    conflict_id: 'conflict_452982fe260d72eb93f2a870',
    canonical_key: 'structure.stories',
  },
]);

/** Ranked, never surveyed, nothing in dispute. The common case. */
const QUIET = entry('sf-1215-fell', [
  { rule_id: 'rank.never-surveyed', detail: 'No company survey on record' },
]);

describe('reading the findings out of the ranking', () => {
  it('takes only the entries whose score was justified by a conflict', () => {
    const found = disagreementsIn([HAYES, QUIET, MISSION]);
    expect(found.map((f) => f.addressId)).toEqual(['sf-0450-hayes', 'sf-0415-mission']);
  });

  it('strips the machine preamble and keeps the sentence', () => {
    // The severity becomes a badge; saying it twice is noise.
    const [first] = disagreementsIn([HAYES]);
    expect(first!.summary).toBe('Permit records 2 storeys; lidar DSM measures 3.');
    expect(first!.severity).toBe(4);
  });

  it('worst first, so the card a crew acts on is the top one', () => {
    expect(disagreementsIn([MISSION, HAYES]).map((f) => f.severity)).toEqual([4, 2]);
  });

  it('keeps a detail it cannot parse rather than dropping the finding', () => {
    // A rule that changes its wording must not make a real conflict vanish.
    const odd = entry('sf-9-x', [
      { rule_id: 'rank.open-conflict-severity', detail: 'Something the parser has not seen.' },
    ]);
    const [only] = disagreementsIn([odd]);
    expect(only!.summary).toBe('Something the parser has not seen.');
    expect(only!.severity).toBeNull();
  });
});

describe('what the panel says', () => {
  it('leads with the finding, not the machinery', () => {
    render(<RecordsDisagree entries={[HAYES, MISSION, QUIET]} />);
    expect(screen.getByText('Permit records 2 storeys; lidar DSM measures 3.')).toBeInTheDocument();
    const card = screen.getByTestId('disagreement-sf-0450-hayes');
    expect(card).toHaveTextContent('severity 4');
  });

  it('lists only structures in dispute', () => {
    render(<RecordsDisagree entries={[HAYES, MISSION, QUIET]} />);
    expect(screen.queryByTestId('disagreement-sf-1215-fell')).not.toBeInTheDocument();
  });

  it('says the district holds more than it is showing', () => {
    // The ranking cites one conflict per structure. Two cards over four open
    // conflicts, said plainly rather than left to read as all of them.
    render(<RecordsDisagree entries={[HAYES, MISSION]} openConflicts={4} />);
    const note = screen.getByTestId('disagreement-shortfall');
    expect(note).toHaveTextContent('worst disagreement at each of 2 structures');
    expect(note).toHaveTextContent('district has 4 open in total');
  });

  it('stays quiet when it is showing everything', () => {
    render(<RecordsDisagree entries={[HAYES, MISSION]} openConflicts={2} />);
    expect(screen.queryByTestId('disagreement-shortfall')).not.toBeInTheDocument();
  });

  it('says nothing is in dispute rather than drawing an empty list', () => {
    render(<RecordsDisagree entries={[QUIET]} />);
    expect(screen.getByText(/No structure in this district has an open disagreement/))
      .toBeInTheDocument();
    expect(screen.queryByLabelText('Structures where records disagree')).not.toBeInTheDocument();
  });

  it('opens the structure when the address is clicked', () => {
    const onSelect = vi.fn();
    render(<RecordsDisagree entries={[HAYES]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /Open sf-0450-hayes/ }));
    expect(onSelect).toHaveBeenCalledWith('sf-0450-hayes');
  });

  it('does not share an accessible name with the same address in the queue', () => {
    // Both open the structure. A screen reader hearing "sf-0450-hayes" twice
    // with no way to tell them apart is the failure this guards.
    render(<RecordsDisagree entries={[HAYES]} />);
    expect(screen.queryByRole('button', { name: 'sf-0450-hayes' })).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Open sf-0450-hayes, records disagree' }),
    ).toBeInTheDocument();
  });

  it('keeps the evidence an investigator would quote, small', () => {
    render(<RecordsDisagree entries={[HAYES]} />);
    const card = screen.getByTestId('disagreement-sf-0450-hayes');
    // One line: the attribute in dispute, then the id, both small.
    expect(within(card).getByText(/structure\.stories/)).toHaveTextContent('conflict_63aac5a');
  });
});
