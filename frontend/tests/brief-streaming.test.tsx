/**
 * The brief filling in, rather than landing in slabs.
 *
 * Two separate claims are under test, and neither is an animation:
 *
 * 1. A line is dated to the emission that **first carried it**, so a reading
 *    the fleet produced during the incident is distinguishable from one that
 *    was read out of the record at v1.
 * 2. Prose composed token by token is rendered as it arrives, and is marked
 *    provisional until the persisted emission replaces it.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { BriefPanel, itemKey, trackItems } from '@/components/incident/BriefPanel';
import type { BriefEmissionView, BriefItemView } from '@/lib/api/types';

function item(label: string, value: string): BriefItemView {
  return {
    label,
    value_render: value,
    status: 'CONFIRMED',
    canonical_key: null,
    fact_id: null,
    provenance: null,
    derivation_note: null,
    reported_note: null,
    withheld_note: null,
  };
}

function emission(
  version: number,
  stage: BriefEmissionView['stage'],
  items: BriefItemView[],
): BriefEmissionView {
  return {
    emission_id: `em-${version}`,
    incident_id: 'inc-1',
    version,
    stage,
    sections: [{ key: 'CONSTRUCTION', items }],
    unknowns: [],
    unavailable: [],
    withheld: [],
    conflict_ids: [],
    narrative: null,
    narrative_available: false,
    model_invoked: false,
    profile_snapshot_id: 'snap-1',
    agent_versions: {},
    produced_at: new Date().toISOString(),
    persisted_at: new Date().toISOString(),
    content_hash: 'abc123def456',
  };
}

const V1 = emission(1, 'INSTANT', [item('construction', 'Type III')]);
const V2 = emission(2, 'AMENDMENT', [
  item('construction', 'Type III'),
  item('thermal ALPHA', '166 C'),
]);

describe('dating a line to the pass that produced it', () => {
  it('remembers the first version each line appeared in', () => {
    const tracked = trackItems([V2, V1]); // deliberately out of order
    expect(tracked.get(itemKey('CONSTRUCTION', item('construction', 'Type III')))?.firstSeen).toBe(1);
    expect(tracked.get(itemKey('CONSTRUCTION', item('thermal ALPHA', '166 C')))?.firstSeen).toBe(2);
  });

  it('treats a changed value as a new reading, not the same line', () => {
    // A face that was UNSCANNED and now reads 166 C is the drone sweep having
    // flown it. Keying on the label alone would let that arrive silently.
    const before = emission(1, 'INSTANT', [item('thermal ALPHA', 'UNSCANNED')]);
    const after = emission(2, 'AMENDMENT', [item('thermal ALPHA', '166 C')]);
    const tracked = trackItems([before, after]);
    expect(tracked.get(itemKey('CONSTRUCTION', item('thermal ALPHA', '166 C')))?.firstSeen).toBe(2);
  });

  it('marks a line the fleet produced during the incident, and only that one', () => {
    render(<BriefPanel emission={V2} emissions={[V1, V2]} />);
    const thermal = screen.getByText('thermal ALPHA').closest('div')!;
    expect(thermal).toHaveAttribute('data-arrived', 'true');
    expect(within(thermal).getByText(/New in v2/)).toBeInTheDocument();
    // The v1 line was read from stored state and is not new. `getAllByText`
    // because the section heading is also the word "construction".
    const line = screen.getAllByText('construction').find((n) => n.tagName === 'DT')!;
    expect(line.closest('div')).not.toHaveAttribute('data-arrived');
  });

  it('marks nothing on the instant brief, where every line arrives together', () => {
    // v1 is a read of one snapshot. Nothing in it "just happened", and marking
    // it as though it had would be the invented progress this panel refuses.
    const { container } = render(<BriefPanel emission={V1} emissions={[V1]} />);
    expect(container.querySelectorAll('[data-arrived="true"]')).toHaveLength(0);
  });
});

describe('prose being written', () => {
  it('renders provisional text and says it is not in the log yet', () => {
    render(
      <BriefPanel emission={V1} emissions={[V1]} draftNarrative="Three storeys, Type" writing />,
    );
    expect(screen.getByTestId('brief-narrative-draft')).toHaveTextContent('Three storeys, Type');
    expect(screen.getByText(/provisional, not yet in the incident log/i)).toBeInTheDocument();
  });

  it('lets the persisted narrative win once the record has it', () => {
    const persisted: BriefEmissionView = {
      ...V2,
      narrative: 'The persisted size-up.',
      narrative_available: true,
      model_invoked: true,
      stage: 'ENRICHED',
    };
    render(
      <BriefPanel
        emission={persisted}
        emissions={[V1, persisted]}
        draftNarrative="a half-written draft"
      />,
    );
    expect(screen.getByText('The persisted size-up.')).toBeInTheDocument();
    expect(screen.queryByTestId('brief-narrative-draft')).toBeNull();
  });
});
