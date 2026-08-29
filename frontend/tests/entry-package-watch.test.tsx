/**
 * The console says what the loop is doing about the entry package.
 *
 * This panel exists because silence was indistinguishable from failure. A loop
 * that was working, a loop whose composition had been cancelled mid-run by a
 * model timeout, and a loop with autonomy switched off all rendered as the same
 * empty screen — which is why the same live failure survived several rounds of
 * investigation. Every assertion here is that one of those states is now
 * legible and distinct from the others.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  EntryPackageWatch,
  describeAutonomy,
} from '@/components/incident/EntryPackageWatch';
import type { AutonomyDiagnosticsView } from '@/lib/api/entry-packages';

function view(over: Partial<AutonomyDiagnosticsView> = {}): AutonomyDiagnosticsView {
  return {
    incident_id: 'inc_1',
    autonomy_enabled: true,
    tracked: true,
    opened_at: '2026-08-28T09:00:00Z',
    age_s: 20,
    deadline_armed: true,
    deadline_at: '2026-08-28T09:01:51Z',
    deadline_in_s: 91,
    composing: false,
    attempts: 0,
    failures: 0,
    composed_package_id: '',
    composed_trigger: '',
    failed_at: null,
    failed_trigger: '',
    failed_error_type: '',
    failed_error_message: '',
    failed_criteria: [],
    outstanding_criteria: ['thermal.coverage'],
    assessment_error: '',
    packages: 0,
    ...over,
  };
}

describe('what the console says while there is no card yet', () => {
  it('counts down to the composition, and names what it is waiting on', () => {
    const { text, tone } = describeAutonomy(view());
    expect(text).toMatch(/composes the entry package in 91s/);
    expect(text).toMatch(/thermal\.coverage/);
    expect(tone).toBe('muted');
  });

  it('says outright when the loop will never compose on its own', () => {
    // Not an error, and not a wait either. An officer reading a countdown that
    // was never going to fire is worse served than one told to press a button.
    const { text, tone } = describeAutonomy(view({ autonomy_enabled: false }));
    expect(text).toMatch(/autonomy is switched off/);
    expect(tone).toBe('disputed');
  });

  it('distinguishes a backend that is not tracking this incident at all', () => {
    // The restart case. Nothing is scheduled and nothing ever will be, which
    // reads identically to "still working" from an empty package list.
    const { text, tone } = describeAutonomy(view({ tracked: false }));
    expect(text).toMatch(/not tracking the incident/);
    expect(tone).toBe('alarm');
  });

  it('names the failure and what was outstanding when a composition died', () => {
    // The live failure that took several rounds to find: the run was cancelled
    // mid-composition and the exception was swallowed by the shrug policy.
    const { text, tone } = describeAutonomy(
      view({
        failures: 1,
        failed_error_type: 'TimeoutError',
        failed_error_message: 'the run was cancelled at its budget',
        failed_criteria: ['thermal.coverage', 'hazard.resolved'],
      }),
    );
    expect(text).toMatch(/cancelled at its budget/);
    expect(text).toMatch(/thermal\.coverage, hazard\.resolved/);
    expect(tone).toBe('alarm');
  });

  it('falls back to the error type when no message was recorded', () => {
    const { text } = describeAutonomy(
      view({ failures: 1, failed_error_type: 'NotFoundError', failed_error_message: '' }),
    );
    expect(text).toMatch(/NotFoundError/);
  });

  it('says a composition is under way rather than counting down through it', () => {
    const { text } = describeAutonomy(view({ composing: true }));
    expect(text).toMatch(/composing the entry package now/);
  });

  it('says nothing is scheduled when no deadline is armed', () => {
    const { text } = describeAutonomy(
      view({ deadline_armed: false, deadline_in_s: null }),
    );
    expect(text).toMatch(/No composition is scheduled/);
  });
});

describe('the panel itself', () => {
  it('renders nothing once a package exists, because the card owns the screen', () => {
    render(<EntryPackageWatch incidentId="inc_1" hasPackage />);
    expect(screen.queryByTestId('entry-package-watch')).not.toBeInTheDocument();
  });

  it('renders nothing outside an incident', () => {
    render(<EntryPackageWatch incidentId={null} hasPackage={false} />);
    expect(screen.queryByTestId('entry-package-watch')).not.toBeInTheDocument();
  });

  it('says nothing before it has asked, rather than guessing at the loop', () => {
    // The wait is deliberate: a status line during the seconds an officer is
    // reading the brief would be noise, and nothing is known yet anyway.
    render(<EntryPackageWatch incidentId="inc_1" hasPackage={false} />);
    expect(screen.queryByTestId('entry-package-watch')).not.toBeInTheDocument();
  });
});
