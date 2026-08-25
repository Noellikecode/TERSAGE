/**
 * The two surfaces an investigator and a captain need.
 *
 * **The referral gate.** An agent may draft a referral and may not file one.
 * That is the sharpest line in the slow loop -- a referral accuses a property
 * owner and commits another agency -- and until now the console could display
 * referrals but not act on one, so the gate existed in the backend and nowhere
 * a person could see it.
 *
 * **The replay.** The log says what this process holds. The replay re-reads the
 * sealed record and re-computes every hash, so it answers whether the record
 * still says what it said. They are different questions and the console used to
 * show the first under the second's name.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AuditConsole } from '@/components/audit/AuditConsole';
import { AgentRail } from '@/components/standby/AgentRail';
import { ConflictPanel } from '@/components/profile/ConflictPanel';
import type {
  AgentDescriptorView,
  ConflictView,
  IncidentReplayView,
  ReferralSummary,
} from '@/lib/api/types';

const CONFLICT: ConflictView = {
  conflict_id: 'conflict_0c93',
  rule_id: 'permit-vs-lidar-story-count',
  canonical_key: 'structure.stories',
  severity: 4,
  status: 'OPEN',
  summary: 'Permit records 2 storeys; lidar DSM measures 3.',
  fact_ids: ['fact_a', 'fact_b'],
  detected_at: '2026-08-20T08:00:00+00:00',
  resolved_by: null,
};

const STAGED: ReferralSummary = {
  referral_id: 'ref_0c93',
  status: 'AWAITING_APPROVAL',
  case_number: null,
  conflict_id: 'conflict_0c93',
};

const FILED: ReferralSummary = { ...STAGED, status: 'FILED', case_number: 'REF-00001' };

describe('the referral gate', () => {
  it('offers to draft a referral from an open conflict', () => {
    const onStage = vi.fn();
    render(
      <ConflictPanel conflicts={[CONFLICT]} referrals={[]} onStageReferral={onStage} />,
    );

    fireEvent.click(screen.getByTestId('stage-referral-conflict_0c93'));
    expect(onStage).toHaveBeenCalledWith('conflict_0c93');
  });

  it('does not offer to draft a second referral for the same conflict', () => {
    render(
      <ConflictPanel
        conflicts={[CONFLICT]}
        referrals={[STAGED]}
        onStageReferral={vi.fn()}
      />,
    );

    expect(screen.queryByTestId('stage-referral-conflict_0c93')).not.toBeInTheDocument();
  });

  it('a staged referral can be approved by a human', () => {
    const onApprove = vi.fn();
    render(
      <ConflictPanel conflicts={[]} referrals={[STAGED]} onApproveReferral={onApprove} />,
    );

    fireEvent.click(screen.getByTestId('approve-referral-ref_0c93'));
    expect(onApprove).toHaveBeenCalledWith('ref_0c93');
  });

  it('a filed referral shows its case number and cannot be approved again', () => {
    render(
      <ConflictPanel conflicts={[]} referrals={[FILED]} onApproveReferral={vi.fn()} />,
    );

    expect(screen.getByText(/case REF-00001/)).toBeInTheDocument();
    expect(screen.queryByTestId('approve-referral-ref_0c93')).not.toBeInTheDocument();
  });

  it('shows no approval control when the console cannot approve', () => {
    // A viewer-scoped console has no handler. The control is absent rather
    // than present-and-failing, because a button that always errors teaches
    // an officer to ignore buttons.
    render(<ConflictPanel conflicts={[]} referrals={[STAGED]} />);

    expect(screen.queryByTestId('approve-referral-ref_0c93')).not.toBeInTheDocument();
  });
});

const REPLAY: IncidentReplayView = {
  incident_id: 'inc-1',
  entries: [
    {
      sequence: 1,
      entry_id: 'entry-1',
      entry_type: 'brief_emitted',
      occurred_at: '2026-08-20T08:00:00+00:00',
      content_hash: 'abcdef1234567890',
      content: {},
      intact: true,
      agent_versions: { 'incident-interceptor': '1.0.0' },
      profile_snapshot_id: 'snap_abc',
    },
  ],
  digest: 'digest0123456789',
  intact: true,
  tampered_sequences: [],
  agent_versions: { 'incident-interceptor': '1.0.0' },
  policy_versions: ['1.0.0'],
  profile_snapshot_id: 'snap_abc',
  snapshot_available: true,
  sealed_at: '2026-08-20T09:00:00+00:00',
};

function auditProps(overrides: Partial<Parameters<typeof AuditConsole>[0]> = {}) {
  return {
    events: [],
    decisions: [],
    log: null,
    emissions: [],
    ...overrides,
  };
}

describe('replay from the record', () => {
  it('says it has not replayed rather than showing the live log as a replay', () => {
    render(<AuditConsole {...auditProps()} />);

    expect(screen.getByText(/Not replayed/)).toBeInTheDocument();
    expect(screen.queryByTestId('replay-result')).not.toBeInTheDocument();
  });

  it('reports an intact record with the versions that produced it', () => {
    render(<AuditConsole {...auditProps({ replay: REPLAY })} />);

    expect(screen.getByTestId('replay-result')).toBeInTheDocument();
    expect(screen.getByText('record intact')).toBeInTheDocument();
    expect(screen.getByText(/incident-interceptor@1\.0\.0/)).toBeInTheDocument();
  });

  it('names the exact sequences that no longer match their hash', () => {
    // "Something was altered" is not usable by an investigator. Which entry
    // is the only version of this that helps.
    render(
      <AuditConsole
        {...auditProps({
          replay: { ...REPLAY, intact: false, tampered_sequences: [3, 7] },
        })}
      />,
    );

    expect(screen.getByText('record altered')).toBeInTheDocument();
    expect(screen.getByText(/Sequences 3, 7/)).toBeInTheDocument();
  });

  it('says when the snapshot the brief was built from is gone', () => {
    render(
      <AuditConsole {...auditProps({ replay: { ...REPLAY, snapshot_available: false } })} />,
    );

    expect(screen.getByText(/no longer available/)).toBeInTheDocument();
  });

  it('says an open incident is not sealed instead of showing a blank', () => {
    render(<AuditConsole {...auditProps({ replay: { ...REPLAY, sealed_at: null } })} />);

    expect(screen.getByText(/not sealed/)).toBeInTheDocument();
  });
});


function agent(overrides: Partial<AgentDescriptorView>): AgentDescriptorView {
  return {
    ref: `${overrides.agent_id}@1.0.0`,
    agent_id: 'records-watcher',
    version: '1.0.0',
    publisher_department: 'fire',
    loop: 'SLOW',
    role_summary: 'does a thing',
    capabilities: ['READ'],
    required_scopes: ['read:profile'],
    classifications_accessed: ['PUBLIC'],
    write_targets: [],
    approval_threshold: 'NONE',
    input_schema_ref: 'firstdue.schemas.A',
    output_schema_ref: 'firstdue.schemas.B',
    latency_target_ms: 1000,
    published_at: '2026-08-20T08:00:00+00:00',
    deprecated_at: null,
    ...overrides,
  } as AgentDescriptorView;
}

describe('the fleet rail counts the fleet the department actually runs', () => {
  const active = agent({ agent_id: 'structure-watch' });
  const retired = agent({ agent_id: 'survey-ranker', deprecated_at: '2026-08-21T12:00:00+00:00' });

  it('does not show a superseded agent as an idle fleet member', () => {
    // The writeup says nine agents. A rail that lists thirteen as "idle" makes
    // that number look wrong to anybody who counts.
    render(<AgentRail agents={[active, retired]} subscriptions={[]} />);

    expect(screen.getByTestId('fleet-row-structure-watch')).toBeInTheDocument();
    expect(screen.queryByTestId('fleet-row-survey-ranker')).not.toBeInTheDocument();
  });

  it('keeps superseded agents visible, and says why they are kept', () => {
    // Hiding them would be the other error: version pinning exists so a
    // two-year-old brief can still name what produced it.
    render(<AgentRail agents={[active, retired]} subscriptions={[]} />);

    // Listed as one line, and the reason they are kept is a click away rather
    // than forty words repeated in every fleet column.
    fireEvent.click(screen.getByTestId('superseded-agents'));
    const group = screen.getByTestId('fleet-detail-superseded');
    expect(group).toHaveTextContent('survey-ranker @1.0.0');
    expect(group).toHaveTextContent(/names the agent version that produced it/);
  });

  it('shows no superseded group when every agent is current', () => {
    render(<AgentRail agents={[active]} subscriptions={[]} />);

    expect(screen.queryByTestId('superseded-agents')).not.toBeInTheDocument();
  });
});
