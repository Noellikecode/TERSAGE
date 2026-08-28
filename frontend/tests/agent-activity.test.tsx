/**
 * The incident-loop activity stream, and the attribution rule that is easy to
 * get wrong.
 *
 * Every entry in the incident log is *written* by `incident-recorder`, so
 * `agent_versions` says `incident-recorder` on a handoff that is about
 * `sensor-fusion`. Reading the writer as the subject files every wake under the
 * recorder and draws an empty fleet — which looks like a working panel, because
 * one row is still moving.
 *
 * The second rule this file pins down is the one that arrived with the stream:
 * **one entry produces one message, not one per writer.** An analysis names
 * both the recorder and the analysing agent, and printing both would duplicate
 * every action and make half the stream recorder rows.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  actorFor,
  activityStreamFrom,
  AgentActivity,
  describeEntry,
  facetsForEntry,
  identityFor,
  splitAgentRef,
} from '@/components/incident/AgentActivity';
import type { IncidentLogEntryFrame } from '@/lib/api/stream';

function entry(over: Partial<IncidentLogEntryFrame> = {}): IncidentLogEntryFrame {
  return {
    sequence: 1,
    entry_type: 'BRIEF_EMITTED',
    occurred_at: '2026-08-27T08:00:00Z',
    agent_versions: { 'incident-recorder': '1.0.0' },
    content_hash: 'abc',
    content: {},
    ...over,
  };
}

const HANDOFF = entry({
  sequence: 3,
  entry_type: 'AGENT_HANDOFF',
  agent_versions: { 'incident-recorder': '1.0.0' },
  content: {
    agent_ref: 'sensor-fusion@1.0.0',
    rule_ids: ['rule.thermal'],
    intake_keys: ['structure.stories'],
    note: 'woken',
    started: true,
    missing_scopes: [],
  },
});

const FOCUS = entry({
  sequence: 2,
  entry_type: 'FOCUS_COMPOSED',
  agent_versions: { 'incident-interceptor': '1.0.0' },
  content: {
    agent_ids: ['sensor-fusion', 'agency-notifier'],
    pointer_count: 3,
    focus: {
      per_agent: [
        {
          agent_id: 'sensor-fusion',
          headline: 'Caller says third floor; the records disagree about how many there are.',
          pointers: [
            { kind: 'CONFLICT', ref: 'conflict_4923ad5', reason: 'open since March', priority: 1 },
            { kind: 'FACT', ref: 'fact_77aa', reason: 'lidar measured 5', priority: 3 },
          ],
        },
        {
          agent_id: 'agency-notifier',
          headline: 'A rooftop array crews cannot de-energise from the panel.',
          pointers: [{ kind: 'FACT', ref: 'fact_solar1', reason: 'solar array on segment 0', priority: 2 }],
        },
      ],
    },
  },
});

describe('attribution', () => {
  it('files a handoff under the agent woken, not the recorder that wrote it', () => {
    const stream = activityStreamFrom([HANDOFF]);
    const fusion = stream.find((m) => m.agentId === 'sensor-fusion');
    expect(fusion).toBeDefined();
    expect(fusion?.version).toBe('1.0.0');
    expect(fusion?.ruleIds).toEqual(['rule.thermal']);
  });

  it('splits an agent ref into id and pinned version', () => {
    expect(splitAgentRef('sensor-fusion@1.0.0')).toEqual({
      agentId: 'sensor-fusion',
      version: '1.0.0',
    });
    // A ref with no version keeps the whole string rather than inventing one.
    expect(splitAgentRef('sensor-fusion')).toEqual({ agentId: 'sensor-fusion', version: null });
  });

  it('gives one focus entry a handoff message per agent it points at', () => {
    // The slow loop -> incident loop handoff. One entry, several messages.
    const stream = activityStreamFrom([FOCUS]);
    const handoffs = stream.filter((m) => m.kind === 'handoff');
    const fusion = handoffs.find((m) => m.agentId === 'sensor-fusion');
    const notifier = handoffs.find((m) => m.agentId === 'agency-notifier');
    expect(fusion?.pointers.map((p) => p.ref)).toEqual(['conflict_4923ad5', 'fact_77aa']);
    expect(notifier?.pointers.map((p) => p.ref)).toEqual(['fact_solar1']);
    expect(fusion?.handoffHeadline).toMatch(/third floor/);
  });

  it('reads pointers highest priority first, which is the focus’s own order', () => {
    const fusion = activityStreamFrom([FOCUS]).find(
      (m) => m.kind === 'handoff' && m.agentId === 'sensor-fusion',
    );
    expect(fusion?.pointers[0]?.priority).toBe(1);
  });

  it('attributes an ordinary entry to its writer', () => {
    expect(actorFor(entry({ agent_versions: { 'incident-recorder': '1.0.0' } })).agentId).toBe(
      'incident-recorder',
    );
  });

  it('credits the specific writer, not the recorder that writes everything', () => {
    // Both names are on the entry. Crediting both would print the message
    // twice, and the recorder is on every entry there is.
    const stream = activityStreamFrom([
      entry({
        sequence: 6,
        entry_type: 'BRIEF_EMITTED',
        agent_versions: { 'incident-recorder': '1.0.0', 'incident-interceptor': '1.0.0' },
      }),
    ]);
    expect(stream).toHaveLength(1);
    expect(stream[0]?.agentId).toBe('incident-interceptor');
  });

  it('marks an entry it cannot attribute rather than guessing an owner', () => {
    expect(actorFor(entry({ agent_versions: {} })).agentId).toBe('unattributed');
  });
});

describe('agents that record their own work', () => {
  it('files an analysis under the agent that did it, not the recorder', () => {
    // Every entry is written by `incident-recorder`, so both names are on the
    // entry and only one of them analysed anything.
    const stream = activityStreamFrom([
      entry({
        sequence: 7,
        entry_type: 'AGENT_ANALYSIS',
        agent_versions: { 'incident-recorder': '1.0.0', 'sensor-fusion': '1.0.0' },
        content: {
          agent_ref: 'sensor-fusion@1.0.0',
          headline: 'registered ALPHA from the drone sweep',
          detail: 'peak 166 C across scanned faces; 3 face(s) still UNSCANNED',
          refs: ['frame_6b94', 'ALPHA'],
        },
      }),
    ]);
    expect(stream.map((m) => m.agentId)).toEqual(['sensor-fusion']);
    expect(stream[0]?.headline).toBe('registered ALPHA from the drone sweep');
    expect(stream[0]?.detail).toMatch(/UNSCANNED/);
  });

  it('files a notification under the notifier', () => {
    const stream = activityStreamFrom([
      entry({
        sequence: 8,
        entry_type: 'NOTIFICATION_SENT',
        agent_versions: { 'incident-recorder': '1.0.0', 'agency-notifier': '1.0.0' },
        content: { target: 'agency-notifications', external_ref: 'MSG-1', autonomous: true },
      }),
    ]);
    expect(stream.map((m) => m.agentId)).toEqual(['agency-notifier']);
  });

  it('gives each face its own message so a sweep reads as four events', () => {
    // The reason the panel is a stream. As one card per agent this was a single
    // row that changed four times, and three of the four walls were never
    // legible -- a sweep looked like one event that kept editing itself.
    const faces = ['ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'].map((face, i) =>
      entry({
        sequence: 10 + i,
        entry_type: 'AGENT_ANALYSIS',
        agent_versions: { 'incident-recorder': '1.0.0', 'sensor-fusion': '1.0.0' },
        content: { agent_ref: 'sensor-fusion@1.0.0', headline: `registered ${face} from the drone sweep` },
      }),
    );
    const stream = activityStreamFrom(faces);
    expect(stream).toHaveLength(4);
    // Newest first, so the last wall flown is at the top and every earlier one
    // is still there beneath it.
    expect(stream[0]?.headline).toMatch(/DELTA/);
    expect(stream[3]?.headline).toMatch(/ALPHA/);
  });
});

describe('the stream', () => {
  it('keeps every step rather than replacing one with the next', () => {
    const stream = activityStreamFrom([
      entry({ sequence: 1, entry_type: 'BRIEF_EMITTED', content: { version: 1, stage: 'INSTANT' } }),
      entry({ sequence: 4, entry_type: 'BRIEF_EMITTED', content: { version: 2, stage: 'ENRICHED' } }),
    ]);
    expect(stream).toHaveLength(2);
    expect(stream.map((m) => m.headline)).toEqual(['emitted brief v2', 'emitted brief v1']);
  });

  it('drops a replayed entry instead of printing it twice', () => {
    // The stream resumes from Last-Event-ID and a race can deliver one twice.
    // A duplicated message would read as the agent having acted twice.
    const one = entry({ sequence: 9, content: { version: 3, stage: 'AMENDMENT' } });
    const stream = activityStreamFrom([one, one]);
    expect(stream).toHaveLength(1);
  });

  it('puts the newest action at the top', () => {
    const stream = activityStreamFrom([FOCUS, HANDOFF]);
    expect(stream[0]?.sequence).toBe(3);
  });

  it('sorts a handoff under the composition that produced it', () => {
    // Same sequence, because they are the same entry. The composition is the
    // action; the handoffs are what it produced, so they read beneath it.
    const stream = activityStreamFrom([FOCUS]).filter((m) => m.sequence === 2);
    expect(stream[0]?.kind).toBe('action');
    expect(stream.slice(1).every((m) => m.kind === 'handoff')).toBe(true);
  });
});

describe('what an entry says', () => {
  it('turns entry types into words rather than printing the enum', () => {
    expect(describeEntry(entry({ entry_type: 'INTAKE_READ' })).headline).toBe(
      'read the dispatch narrative',
    );
    expect(describeEntry(HANDOFF).headline).toBe('woken and running');
  });

  it('says an agent was selected but not started', () => {
    const notStarted = describeEntry(
      entry({ entry_type: 'AGENT_HANDOFF', content: { started: false, rule_ids: [] } }),
    );
    expect(notStarted.headline).toBe('selected, not started');
  });

  it('shows an unknown entry type as itself rather than inventing a sentence', () => {
    expect(describeEntry(entry({ entry_type: 'SOMETHING_NEW' })).headline).toBe('something new');
  });

  it('drops a detail the entry does not carry instead of printing undefined', () => {
    expect(describeEntry(entry({ entry_type: 'FACT_OBSERVED', content: {} })).detail).toBeNull();
  });
});

describe('per-agent identity', () => {
  it('gives each incident agent its own hue and glyph', () => {
    const ids = [
      'incident-interceptor',
      'sensor-fusion',
      'agency-notifier',
      'incident-recorder',
    ];
    const colors = ids.map((id) => identityFor(id).color);
    const glyphs = ids.map((id) => identityFor(id).glyph);
    expect(new Set(colors).size).toBe(4);
    // The glyph is the second channel: deutan separation between the purple
    // and the blue is ΔE 5.0, so shape has to distinguish what hue cannot.
    expect(new Set(glyphs).size).toBe(4);
  });

  it('never tints an agent with a reserved status colour', () => {
    // An agent the colour of `alarm` reads as an alarm.
    const status = ['#4ade80', '#fbbf24', '#f87171', '#38bdf8'];
    for (const id of Object.keys({
      'incident-interceptor': 0,
      'sensor-fusion': 0,
      'agency-notifier': 0,
      'incident-recorder': 0,
    })) {
      expect(status).not.toContain(identityFor(id).color);
    }
  });

  it('gives an unknown or retired agent the neutral rather than a new hue', () => {
    // Fixed per agent, never cycled -- and a superseded agent should look it.
    expect(identityFor('brief-reconciler').color).toBe('#7c8b9a');
    expect(identityFor('something-new').color).toBe('#7c8b9a');
  });
});

describe('per-message structure', () => {
  it('shows sensor-fusion which wall this frame was registered to', () => {
    // Per message, not per agent: the wall belongs to the frame that was
    // actually flown, and an aggregate "faces registered" line on every row
    // would repeat the whole sweep on each of its four steps.
    const facets = facetsForEntry(
      entry({
        sequence: 20,
        entry_type: 'AGENT_ANALYSIS',
        content: {
          agent_ref: 'sensor-fusion@1.0.0',
          headline: 'read a drone frame and resolved it to BRAVO',
          detail: '3 storey bands observed; 2 face(s) still UNSCANNED',
          refs: ['frame_1', 'BRAVO'],
        },
      }),
      'sensor-fusion',
    );
    expect(facets.find((f) => f.label === 'Face')?.value).toBe('BRAVO');
  });

  it('shows the notifier who it told and whether a human was needed', () => {
    const facets = facetsForEntry(
      entry({
        sequence: 30,
        entry_type: 'NOTIFICATION_SENT',
        content: { target: 'water-supply', external_ref: 'MSG-1', autonomous: true },
      }),
      'agency-notifier',
    );
    expect(facets.find((f) => f.label === 'Agency')?.value).toBe('water-supply');
    expect(facets.find((f) => f.label === 'Authority')?.value).toBe('autonomous');
  });

  it('says plainly when a commitment needed a human', () => {
    const facets = facetsForEntry(
      entry({
        sequence: 31,
        entry_type: 'NOTIFICATION_SENT',
        content: { target: 'gas-shutoff', external_ref: 'MSG-2', autonomous: false },
      }),
      'agency-notifier',
    );
    expect(facets.find((f) => f.label === 'Authority')?.value).toBe('human approved');
  });

  it('shows the interceptor how far the brief got', () => {
    const facets = facetsForEntry(
      entry({
        sequence: 40,
        entry_type: 'BRIEF_EMITTED',
        agent_versions: { 'incident-interceptor': '1.0.0' },
        content: { version: 4, stage: 'AMENDMENT' },
      }),
      'incident-interceptor',
    );
    expect(facets.find((f) => f.label === 'Version')?.value).toBe('v4');
    expect(facets.find((f) => f.label === 'Stage')?.value).toBe('amendment');
  });

  it('tells the recorder’s rows apart by which entry it just wrote', () => {
    // Otherwise every recorder message reads "wrote the log" and none say what.
    const facets = facetsForEntry(
      entry({ sequence: 41, entry_type: 'AGENT_HANDOFF', content: {} }),
      'incident-recorder',
    );
    expect(facets.find((f) => f.label === 'Entry')?.value).toBe('agent handoff');
  });

  it('adds no line to an entry that carries nothing structured', () => {
    expect(
      facetsForEntry(entry({ sequence: 50, entry_type: 'SOMETHING_NEW' }), 'unattributed'),
    ).toEqual([]);
  });
});

describe('the panel', () => {
  it('says nothing has been recorded rather than rendering an empty box', () => {
    render(<AgentActivity entries={[]} />);
    expect(screen.getByText(/Nothing recorded yet/i)).toBeInTheDocument();
  });

  it('renders a message per action, newest first, with what it was handed', () => {
    render(<AgentActivity entries={[FOCUS, HANDOFF]} />);
    const stream = screen.getByTestId('activity-stream');
    const rows = within(stream).getAllByRole('listitem');
    // The handoff is sequence 3 and the composition sequence 2, so the wake is
    // at the top and nothing above it has been overwritten.
    expect(rows[0]).toHaveTextContent(/woken and running/);
    expect(within(stream).getByText('conflict_4923ad5')).toBeInTheDocument();
    expect(within(stream).getAllByText(/from the slow loop/i).length).toBeGreaterThan(0);
  });

  it('scrolls the stream rather than growing the column', () => {
    // Every action is kept, so the list is unbounded and something has to give.
    // It is the list that scrolls, not the page.
    render(<AgentActivity entries={[FOCUS, HANDOFF]} />);
    expect(screen.getByTestId('activity-stream').className).toMatch(/overflow-y-auto/);
  });

  it('animates only the messages that are new to this render', () => {
    // Without this every row re-animates whenever any agent acts, and a stream
    // of twenty flashes in full each time one message lands.
    const { rerender } = render(<AgentActivity entries={[HANDOFF]} />);
    rerender(<AgentActivity entries={[HANDOFF, FOCUS]} />);
    const settled = screen.getByTestId('activity-message-3:sensor-fusion');
    expect(settled.className).not.toMatch(/activity-message-arrived/);
  });

  it('names a scope the incident grant could not cover', () => {
    // "Nobody told the recorder" is only answerable if the reason is beside it.
    render(
      <AgentActivity
        entries={[
          entry({
            sequence: 5,
            entry_type: 'AGENT_HANDOFF',
            content: {
              agent_ref: 'agency-notifier@1.0.0',
              rule_ids: ['rule.utility'],
              started: false,
              missing_scopes: ['write:utility-shutoff'],
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText(/write:utility-shutoff/)).toBeInTheDocument();
  });
});
