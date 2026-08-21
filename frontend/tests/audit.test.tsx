/** The audit console: filters, replay, and version comparison. */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AuditConsole, diffEmissions } from '@/components/audit/AuditConsole';
import { DECISIONS, EVENTS, LOG, emission } from './fixtures';

const EMISSIONS = [
  emission({ version: 1 }),
  emission({
    version: 2,
    stage: 'ENRICHED',
    narrative: 'Wood-frame, two storeys on file.',
    narrative_available: true,
    model_invoked: true,
    content_hash: 'ffffffff00000000',
  }),
];

describe('the audit console', () => {
  it('lists policy decisions with the rule and the policy version', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={LOG} emissions={EMISSIONS} />);
    expect(screen.getByText('approval.required · policy 1.0.0 · decided by deterministic-policy-engine')).toBeInTheDocument();
  });

  it('filters decisions by outcome', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={LOG} emissions={EMISSIONS} />);
    fireEvent.change(screen.getByLabelText('Filter decisions by outcome'), {
      target: { value: 'DENY' },
    });
    expect(screen.getByText('No decisions match this filter.')).toBeInTheDocument();
  });

  it('filters audit events by kind, including injection blocks', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={LOG} emissions={EMISSIONS} />);
    fireEvent.change(screen.getByLabelText('Filter events by kind'), {
      target: { value: 'injection_blocked' },
    });
    // The filter is on the events list, not on the option label, so scope the
    // assertion to what the list itself now shows.
    const list = screen.getAllByRole('list').find((node) =>
      node.textContent?.includes('local-injection-detector'),
    );
    expect(list).toBeDefined();
    expect(within(list as HTMLElement).getByText('injection blocked')).toBeInTheDocument();
    expect(within(list as HTMLElement).queryByText('write executed')).not.toBeInTheDocument();
  });

  it('replays the incident log in order with content hashes', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={LOG} emissions={EMISSIONS} />);
    expect(screen.getByText('2 entries · sealed 09:00:00', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('000')).toBeInTheDocument();
    expect(screen.getByText('001')).toBeInTheDocument();
  });

  it('says plainly when there is nothing to replay', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={null} emissions={[]} />);
    expect(screen.getByText(/No incident log to replay/)).toBeInTheDocument();
  });

  it('compares two brief versions field by field', () => {
    render(<AuditConsole events={EVENTS} decisions={DECISIONS} log={LOG} emissions={EMISSIONS} />);
    const diff = screen.getByTestId('version-diff');
    expect(within(diff).getByText(/stage INSTANT → ENRICHED/)).toBeInTheDocument();
    expect(within(diff).getByText(/narrative absent → present/)).toBeInTheDocument();
  });
});

describe('diffing emissions', () => {
  it('reports a content hash change', () => {
    const changes = diffEmissions(EMISSIONS[0]!, EMISSIONS[1]!);
    expect(changes.some((c) => c.startsWith('content hash'))).toBe(true);
  });

  it('says so when nothing differs', () => {
    expect(diffEmissions(EMISSIONS[0]!, EMISSIONS[0]!)).toEqual(['no field-level differences']);
  });
});
