/**
 * The 911 intake surface.
 *
 * These tests are about the epistemics, not the layout. The backend refuses
 * structurally to turn a caller's words into a structural fact; the console
 * has to agree, and the way it agrees is visible: every reported line carries
 * the caller's own quote, is marked as reported, and never claims to be
 * confirmed.
 *
 * The quote check is the one that matters most. A model asked for character
 * offsets can return well-formed offsets pointing at the wrong sentence, and
 * an officer reading the console has no way to know unless the console checks.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { IntakePanel } from '@/components/incident/IntakePanel';
import { DispatchPanel, SAMPLE_CALLS } from '@/components/standby/DispatchPanel';
import type { IntakeResponse } from '@/lib/api/types';

import { INTAKE, SAMPLE_NARRATIVE } from './fixtures';

function intake(overrides: Partial<IntakeResponse> = {}): IntakeResponse {
  return { ...INTAKE, ...overrides };
}

describe('what the call reported', () => {
  it('shows the caller quote beside every reported value', () => {
    render(<IntakePanel intake={intake()} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByText(/Two people are still inside\./)).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('marks every line as reported, never as confirmed', () => {
    render(<IntakePanel intake={intake()} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByText('reported')).toBeInTheDocument();
    expect(screen.queryByText(/confirmed/i)).not.toBeInTheDocument();
  });

  it('flags a quote that does not appear where it claims to', () => {
    // The offsets are real but point at the wrong sentence. The backend drops
    // these; if one ever reaches the console, the console says so rather than
    // rendering it as though it were traceable.
    render(
      <IntakePanel
        intake={intake({
          reported: [
            {
              intake_key: 'intake.people_trapped',
              reported_value: '2',
              quoted_text: 'Two people are still inside.',
              start_offset: 0,
              end_offset: 28,
            },
          ],
        })}
        narrative={SAMPLE_NARRATIVE}
      />,
    );

    expect(screen.getByTestId('quote-mismatch')).toBeInTheDocument();
  });

  it('says the call was not read, rather than showing an empty list', () => {
    // "The model was down" and "the caller said nothing relevant" are
    // different statements. Rendering both as an empty list is the exact
    // absence-as-none failure this project refuses.
    render(
      <IntakePanel
        intake={intake({ accepted: false, rejection_reason: 'screen unavailable', reported: [] })}
        narrative={SAMPLE_NARRATIVE}
      />,
    );

    expect(screen.getByTestId('intake-unread')).toHaveTextContent(/not read/);
    expect(screen.getByTestId('intake-unread')).toHaveTextContent(/screen unavailable/);
    expect(screen.queryByTestId('intake-reported')).not.toBeInTheDocument();
  });

  it('distinguishes a call that reported nothing from one that was not read', () => {
    render(<IntakePanel intake={intake({ reported: [] })} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByText(/reported nothing about the attributes/)).toBeInTheDocument();
    expect(screen.queryByTestId('intake-unread')).not.toBeInTheDocument();
  });

  it('names what the call did not settle', () => {
    render(<IntakePanel intake={intake()} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByText(/did not settle/)).toHaveTextContent('access obstruction');
  });

  it('shows which agents the narrative woke and which rules fired', () => {
    render(<IntakePanel intake={intake()} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByTestId('intake-woken')).toHaveTextContent('sensor-fusion');
    expect(screen.getByTestId('intake-woken')).toHaveTextContent('route.people-reported-inside');
  });

  it('names a wake the grant could not cover, and the scope it lacked', () => {
    render(
      <IntakePanel
        intake={intake({
          withheld: [
            {
              agent_ref: 'hazard-watcher@1.0.0',
              missing_scopes: ['read:tier-ii-metadata'],
              rule_ids: ['reported-hazardous-material-is-checked-against-tier-ii'],
            },
          ],
        })}
        narrative={SAMPLE_NARRATIVE}
      />,
    );

    const withheld = screen.getByTestId('intake-withheld');
    expect(withheld).toHaveTextContent('hazard-watcher@1.0.0');
    expect(withheld).toHaveTextContent('read:tier-ii-metadata');
  });

  it('names the agent version that woke, not just the agent', () => {
    // A replay two years later has to say which version ran.
    render(<IntakePanel intake={intake()} narrative={SAMPLE_NARRATIVE} />);

    expect(screen.getByTestId('intake-woken')).toHaveTextContent('sensor-fusion@1.0.0');
  });

  it('says when a woken agent did not actually start', () => {
    render(
      <IntakePanel
        intake={intake({
          woken: [
            {
              agent_ref: 'sensor-fusion@1.0.0',
              intake_keys: [],
              rule_ids: [],
              started: false,
            },
          ],
        })}
        narrative={SAMPLE_NARRATIVE}
      />,
    );

    expect(screen.getByTestId('intake-woken')).toHaveTextContent('not started');
  });

  it('reports an injection the screen caught in the transcript', () => {
    render(
      <IntakePanel
        intake={intake({ screen_findings: ['instruction-override', 'role-reassignment'] })}
        narrative={SAMPLE_NARRATIVE}
      />,
    );

    expect(screen.getByText(/instruction-override/)).toBeInTheDocument();
    expect(screen.getByText(/treated as data/)).toBeInTheDocument();
  });
});

describe('dispatching with a narrative', () => {
  it('sends the transcript and the channel with the dispatch', () => {
    const onDispatch = vi.fn();
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={onDispatch} />);

    fireEvent.change(screen.getByLabelText(/transcript or CAD narrative/i), {
      target: { value: 'Smoke on the third floor.' },
    });
    fireEvent.click(screen.getByTestId('dispatch-button'));

    // Typed text has no recording: the fourth argument is the audio that goes
    // with a sample, and there is none for something somebody wrote.
    expect(onDispatch).toHaveBeenCalledWith(
      'sf-0450-hayes',
      'Smoke on the third floor.',
      'CALL_911',
      undefined,
    );
  });

  it('dispatches with no narrative at all', () => {
    // The narrative is optional and must stay optional: an incident opens on
    // an address alone and the instant brief is unchanged.
    const onDispatch = vi.fn();
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={onDispatch} />);

    fireEvent.click(screen.getByTestId('dispatch-button'));

    expect(onDispatch).toHaveBeenCalledWith('sf-0450-hayes', '', 'CALL_911', undefined);
  });

  it('a sample call fills the box and carries its own channel', () => {
    const onDispatch = vi.fn();
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={onDispatch} />);

    const cad = SAMPLE_CALLS.find((s) => s.channel === 'CAD_NARRATIVE')!;
    fireEvent.click(screen.getByRole('button', { name: cad.label }));
    fireEvent.click(screen.getByTestId('dispatch-button'));

    expect(onDispatch).toHaveBeenCalledWith(
      'sf-0450-hayes',
      cad.text,
      'CAD_NARRATIVE',
      cad.audioSrc,
    );
  });

  it('labels the sample calls as synthetic', () => {
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={vi.fn()} />);

    expect(screen.getByText(/synthetic/i)).toBeInTheDocument();
  });

  it('ships a red-team transcript, so the screen can be demonstrated', () => {
    const poisoned = SAMPLE_CALLS.find((s) => s.id === 'poisoned');
    expect(poisoned).toBeDefined();
    expect(poisoned!.text).toMatch(/IGNORE ALL PREVIOUS INSTRUCTIONS/);
  });
});
