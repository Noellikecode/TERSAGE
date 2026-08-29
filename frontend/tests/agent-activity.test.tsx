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

import { act, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  actorFor,
  activityStreamFrom,
  AgentActivity,
  describeEntry,
  facetsForEntry,
  identityFor,
  releaseDelayFor,
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

describe('how fast the queue is dealt', () => {
  it('holds the caught-up pace at a length a single arrival is legible at', () => {
    // One message waiting is work arriving live, and it gets the full beat.
    expect(releaseDelayFor(1)).toBe(260);
    expect(releaseDelayFor(0)).toBe(260);
  });

  it('deals faster the further behind it is, down to a floor', () => {
    // The rate is the whole replacement for the bulk dump: a deep queue is
    // drained by shortening the wait, never by putting several on screen at
    // once. Monotonic so a queue that grew always speeds up.
    const delays = [1, 2, 4, 8, 20, 40].map(releaseDelayFor);
    expect(delays).toEqual([...delays].sort((a, b) => b - a));
    expect(releaseDelayFor(4)).toBe(65);
    // The floor is roughly three frames. Below it two arrivals begin on the
    // same paint and read as one motion, which is the dump wearing an
    // animation.
    expect(releaseDelayFor(40)).toBe(45);
    expect(releaseDelayFor(500)).toBe(45);
  });

  it('drains a forty-deep backlog in a couple of seconds, not a minute', async () => {
    // The reason the bulk dump existed. A reconnect replays from Last-Event-ID
    // and forty messages at the caught-up pace is ten seconds of watching
    // history; the adaptive rate covers it without grouping any of them.
    let total = 0;
    for (let queued = 40; queued >= 1; queued -= 1) total += releaseDelayFor(queued);
    expect(total).toBeLessThan(2500);
    // And the tail slows back down, so the last messages land at the pace of
    // work happening rather than at the pace of a replay.
    expect(releaseDelayFor(1)).toBeGreaterThan(releaseDelayFor(40) * 4);
  });
});

describe('the panel', () => {
  // The column starts blank and releases one message per timer tick, so nothing
  // a render puts on screen is there synchronously. The clock is fake and never
  // advances on its own, which keeps every wait below exactly what the code
  // asked for rather than what the machine running the test managed.
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * One step of the clock, shorter than the release floor.
   *
   * Every wait the panel asks for is at least 45ms, so a step this size can
   * only ever cover one of them: a step that lands two messages is the code
   * grouping them and not the clock jumping over them. Advancing by a beat
   * longer than the slowest wait would prove nothing, because several releases
   * fall inside one such jump.
   */
  const STEP_MS = 20;

  async function step() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STEP_MS);
    });
  }

  /**
   * How many messages are on screen.
   *
   * By card rather than by list item: a handoff's references are themselves a
   * list, so counting `listitem` counts a message's pointers as arrivals and
   * reads a single card landing as three.
   */
  function dealt(): number {
    return screen.queryAllByTestId(/^activity-message-/).length;
  }

  /** The column's length after each step, for as long as asked. */
  async function sampleWhileDealing(steps: number): Promise<number[]> {
    const counts: number[] = [];
    for (let i = 0; i < steps; i += 1) {
      await step();
      counts.push(dealt());
    }
    return counts;
  }

  /** Run the clock until exactly one more message has landed. */
  async function landOne() {
    const before = dealt();
    for (let i = 0; i < 40; i += 1) {
      await step();
      if (dealt() > before) return;
    }
  }

  /** Let `count` messages land, one at a time. */
  async function landAll(count: number) {
    for (let i = 0; i < count; i += 1) await landOne();
  }

  /** A sweep's worth of one agent's actions, oldest first. */
  function burst(count: number, from = 600) {
    return Array.from({ length: count }, (_, i) =>
      entry({
        sequence: from + i,
        entry_type: 'AGENT_ANALYSIS',
        content: { agent_ref: 'sensor-fusion@1.0.0', headline: `step ${i}` },
      }),
    );
  }

  it('says nothing has been recorded rather than rendering an empty box', () => {
    render(<AgentActivity entries={[]} />);
    expect(screen.getByText(/Nothing recorded yet/i)).toBeInTheDocument();
  });

  it('starts blank even when the incident already had entries at mount', async () => {
    // The column is watched, not consulted. An admin who opens it onto a
    // running incident cannot tell which messages predated them, so priming the
    // panel with "what was already there" was just the whole history landing in
    // one frame -- the grouping this panel exists to avoid.
    render(<AgentActivity entries={[FOCUS, HANDOFF]} />);
    expect(dealt()).toBe(0);
    expect(screen.getByText(/Nothing recorded yet/i)).toBeInTheDocument();

    await landOne();
    expect(dealt()).toBe(1);
    await landAll(3);
    expect(dealt()).toBe(4);
  });

  it('renders a message per action, newest first, with what it was handed', async () => {
    render(<AgentActivity entries={[FOCUS, HANDOFF]} />);
    await landAll(4);
    const stream = screen.getByTestId('activity-stream');
    const rows = within(stream).getAllByRole('listitem');
    // The handoff is sequence 3 and the composition sequence 2, so the wake is
    // at the top and nothing above it has been overwritten.
    expect(rows[0]).toHaveTextContent(/woken and running/);
    expect(within(stream).getByText('conflict_4923ad5')).toBeInTheDocument();
    expect(within(stream).getAllByText(/from the slow loop/i).length).toBeGreaterThan(0);
  });

  it('scrolls the stream rather than growing the column', async () => {
    // Every action is kept, so the list is unbounded and something has to give.
    // It is the list that scrolls, not the page.
    render(<AgentActivity entries={[FOCUS, HANDOFF]} />);
    await landAll(4);
    expect(screen.getByTestId('activity-stream').className).toMatch(/overflow-y-auto/);
  });

  it('deals a burst one message at a time rather than in a single frame', async () => {
    // Four faces of a sweep, or seven agencies notified together, arrive in one
    // render. Put on screen at once they read as a list that changed; dealt one
    // at a time they read as agents working.
    const faces = ['ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'].map((face, i) =>
      entry({
        sequence: 60 + i,
        entry_type: 'AGENT_ANALYSIS',
        content: { agent_ref: 'sensor-fusion@1.0.0', headline: `registered ${face}` },
      }),
    );
    const { rerender } = render(<AgentActivity entries={[]} />);
    rerender(<AgentActivity entries={faces} />);
    expect(dealt()).toBe(0);

    await landOne();
    // The oldest first, so the burst stacks up the way the agent acted.
    expect(dealt()).toBe(1);
    expect(screen.getByText(/registered ALPHA/)).toBeInTheDocument();

    await landAll(3);
    expect(dealt()).toBe(4);
    // And newest on top once they have all landed.
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent(/DELTA/);
  });

  it('lands a burst of twenty one at a time, never a group', async () => {
    // The bug the admin reported: a burst over the old catch-up threshold was
    // put on screen in one frame, and bursts are routine now. The count may
    // only ever climb by one, however deep the queue gets.
    render(<AgentActivity entries={burst(20)} />);
    const counts = await sampleWhileDealing(200);
    let previous = 0;
    for (const count of counts) {
      expect(count - previous).toBeLessThanOrEqual(1);
      previous = count;
    }
    expect(previous).toBe(20);
    // Every intermediate count was on screen at some point, which is what
    // "individually" means from the chair in front of it.
    expect(new Set(counts).size).toBe(21);
  });

  it('drains a forty-deep backlog in seconds without ever grouping it', async () => {
    // A reconnect replays from Last-Event-ID, and that whole replay used to be
    // dumped on screen at once. Forty now arrive one by one and it still costs
    // a couple of seconds, because the queue's depth is what sets the rate.
    render(<AgentActivity entries={burst(40, 900)} />);
    const counts = await sampleWhileDealing(150);
    let previous = 0;
    for (const count of counts) {
      expect(count - previous).toBeLessThanOrEqual(1);
      previous = count;
    }
    // The clock only moves when a step moves it, so steps-to-drain is the time
    // the panel actually asked for rather than the machine's own pace.
    const drainedAfter = (counts.indexOf(40) + 1) * STEP_MS;
    expect(counts.indexOf(40)).toBeGreaterThan(-1);
    expect(drainedAfter).toBeLessThan(3000);
  });

  it('animates each arrival on its own rather than several sharing one', async () => {
    // The stagger is only worth its seconds if each landing is its own motion.
    // Exactly one row carries the arrival class per release: the one that just
    // landed, and never the ones already sitting there.
    const { container } = render(<AgentActivity entries={burst(6, 700)} />);
    for (let i = 0; i < 6; i += 1) {
      await landOne();
      expect(container.querySelectorAll('.activity-message-arrived')).toHaveLength(1);
    }
  });

  it('animates only the messages that are new to this render', async () => {
    // Without this every row re-animates whenever any agent acts, and a stream
    // of twenty flashes in full each time one message lands.
    const { rerender } = render(<AgentActivity entries={[HANDOFF]} />);
    await landOne();
    rerender(<AgentActivity entries={[HANDOFF, FOCUS]} />);
    await landAll(3);
    const settled = screen.getByTestId('activity-message-3:sensor-fusion');
    expect(settled.className).not.toMatch(/activity-message-arrived/);
  });

  it('names a scope the incident grant could not cover', async () => {
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
    await landOne();
    expect(screen.getByText(/write:utility-shutoff/)).toBeInTheDocument();
  });
});
