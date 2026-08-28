/**
 * The incident loop working, one message per action, as it happens.
 *
 * The brief below this answers *what does the commander know*. This answers
 * *how did it get there* -- which agent was woken, under which rule, what the
 * slow loop handed it, and every step any of them has taken since. During the
 * ninety seconds this system exists for, both are live at once.
 *
 * **A stream, not a dashboard.** This was one card per agent, replaced in place
 * by that agent's next step. That answered "what is this agent doing now" and
 * threw away the answer to "what has happened" -- during a sweep, three of the
 * four faces scrolled past as a card that changed twice. Every action now gets
 * its own message and nothing is overwritten, newest at the top, scrollable.
 * The count of what an agent has done is a property of the stream now, not a
 * number on a card that replaced the evidence for it.
 *
 * **Every message is the log, not a narration of it.** The entries come from
 * `/incidents/{id}/log/stream`, which is the append-only record the department
 * keeps and an investigator reads two years later. Nothing here is derived from
 * a side channel invented for the console, and nothing is inferred: an entry
 * that cannot be attributed to an agent is shown as unattributed rather than
 * given to the most likely one.
 *
 * **Three different things attribute a message, and they are not the same.**
 *
 * - `AGENT_HANDOFF` names its subject in `content.agent_ref` -- the agent that
 *   was *woken*, which is not the agent that wrote the entry. The recorder
 *   writes every entry, so `agent_versions` on a handoff says
 *   `incident-recorder` and means it. Reading the writer as the subject would
 *   file every wake under the recorder and show an empty fleet.
 * - `FOCUS_COMPOSED` carries `content.focus.per_agent`, one entry per agent:
 *   the headline the head composed for it and the pointers it was given. This
 *   is the **slow loop handing knowledge to the incident loop**, and it is the
 *   only place that handoff is written down. It expands into one handoff
 *   message per agent, beside the composition itself.
 * - Everything else is attributed by `agent_versions`, which is the writer, and
 *   for those entries the writer *is* the actor.
 *
 * **One entry produces one message, not one per writer.** An analysis carries
 * both the recorder and the analysing agent in `agent_versions`; crediting both
 * would print it twice, and since the recorder writes *everything*, the stream
 * would be half recorder. The subject wins where there is one.
 *
 * **A pointer is a reference, never a value.** The focus carries ids, canonical
 * keys and one-line reasons about them -- never "three storeys". That rule is
 * enforced where the focus is built; this panel renders what it is given and
 * would show a value as the reference it is not, which is why it never
 * paraphrases a pointer into prose.
 */
'use client';

import { useEffect, useMemo, useRef } from 'react';

import type { IncidentLogEntryFrame } from '@/lib/api/stream';

// ------------------------------------------------------------------ identity

/**
 * One hue per incident-loop agent, and a glyph beside it.
 *
 * **Colour is reinforcement here, never the encoding.** Every card carries its
 * agent id in mono text at the top, so identity is legible with no colour at
 * all -- which is what makes a four-hue set defensible when a strict
 * all-pairs categorical palette caps at three on this surface.
 *
 * Measured with the dataviz validator against `surface` #12161c, all pairs:
 * lightness band and contrast pass, worst normal-vision ΔE **17.7** (floor 15),
 * worst deutan ΔE 5.0. That last number is the reason for the glyphs: a
 * deuteranope cannot separate the purple from the blue by hue, so each agent
 * also carries a shape, and the name is always there underneath both.
 *
 * The hues are the reference palette's dark steps. The console's own
 * `confirmed`/`disputed`/`alarm`/`live` tokens are deliberately *not* used:
 * those are status colours, and an agent tinted like an alarm would read as
 * one.
 *
 * Fixed per agent, never cycled. An agent this build does not know gets the
 * neutral rather than a generated hue -- and so do the superseded ones, which
 * is not a leftover but the right answer: a retired agent should look retired.
 */
interface AgentIdentity {
  color: string;
  glyph: string;
  /** What this agent is for, in one line, for the card's subtitle. */
  role: string;
}

const NEUTRAL: AgentIdentity = { color: '#7c8b9a', glyph: '·', role: '' };

const IDENTITIES: Readonly<Record<string, AgentIdentity>> = {
  'incident-interceptor': {
    color: '#3987e5',
    glyph: '◈',
    role: 'reads the call, opens the incident, streams the brief',
  },
  'sensor-fusion': {
    color: '#d95926',
    glyph: '▲',
    role: 'registers thermal frames to building faces',
  },
  'agency-notifier': {
    color: '#199e70',
    glyph: '◉',
    role: 'tells the agencies that need to hear it',
  },
  'incident-recorder': {
    color: '#a855c7',
    glyph: '▣',
    role: 'writes the log through to the records system',
  },
};

export function identityFor(agentId: string): AgentIdentity {
  return IDENTITIES[agentId] ?? NEUTRAL;
}

// ---------------------------------------------------------------- the shapes

/** One thing an agent was pointed at, exactly as the focus recorded it. */
export interface ActivityPointer {
  kind: string;
  ref: string;
  reason: string;
  priority: number;
}

/**
 * One thing one agent did, as one message in the stream.
 *
 * Flat on purpose. Everything a message needs to render is read off its own
 * entry when the stream is built, so nothing in the panel has to reach back
 * into the log to draw a row -- which is what keeps a stream that grows all
 * incident long from re-deriving the whole history on every frame.
 */
export interface ActivityMessage {
  /** Unique per message. An entry that fans out to several agents makes one
   *  message each, so the sequence alone is not a key. */
  id: string;
  sequence: number;
  at: string;
  agentId: string;
  /** Pinned version, where the record names one. */
  version: string | null;
  entryType: string;
  /** What it did, in words. Built from the entry's own fields. */
  headline: string;
  /** A second line where the entry carries one worth showing. */
  detail: string | null;
  /** Structured lines read off this one entry, in this agent's own terms. */
  facets: AgentFacet[];
  /**
   * `handoff` is the slow loop pointing an agent at something; `action` is an
   * agent doing something. They are drawn differently because they are
   * different claims -- one is knowledge arriving, the other is work done.
   */
  kind: 'action' | 'handoff';
  /** Handoff only: the one line the head composed for this agent. */
  handoffHeadline: string | null;
  /** Handoff only: what the slow loop pointed it at, highest priority first. */
  pointers: ActivityPointer[];
  /** Rules that selected this agent, from its handoff. */
  ruleIds: string[];
  /** Scopes the incident grant could not cover. Named, never hidden. */
  missingScopes: string[];
}


/** One line on a card: what this agent has produced, in its own terms. */
export interface AgentFacet {
  label: string;
  value: string;
}

// ------------------------------------------------------------ reading entries

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

/** `sensor-fusion@1.0.0` -> id and version. A ref without one keeps the whole. */
export function splitAgentRef(ref: string): { agentId: string; version: string | null } {
  const at = ref.lastIndexOf('@');
  if (at <= 0) return { agentId: ref, version: null };
  return { agentId: ref.slice(0, at), version: ref.slice(at + 1) };
}

/**
 * What an entry says, in words a commander reads rather than a type name.
 *
 * Defensive on purpose: this renders a record written by the backend, and a
 * field that is absent produces a shorter sentence rather than `undefined` on
 * screen. The entry type is always true even when nothing else can be read.
 */
export function describeEntry(entry: IncidentLogEntryFrame): {
  headline: string;
  detail: string | null;
} {
  const c = entry.content ?? {};
  switch (entry.entry_type) {
    case 'INTAKE_READ': {
      const keys = strList(c.canonical_keys ?? c.intake_keys);
      return {
        headline: 'read the dispatch narrative',
        detail: keys.length ? `reported ${keys.join(', ')}` : null,
      };
    }
    case 'FOCUS_COMPOSED': {
      const pointers = typeof c.pointer_count === 'number' ? c.pointer_count : null;
      const agents = strList(c.agent_ids);
      return {
        headline: 'composed the focus',
        detail:
          pointers === null
            ? null
            : `${pointers} pointer${pointers === 1 ? '' : 's'} across ${agents.length} agent${
                agents.length === 1 ? '' : 's'
              }`,
      };
    }
    case 'AGENT_ANALYSIS': {
      // The agent's own words about its own work. Rendered as written rather
      // than reassembled here, because the agent that did the analysis is the
      // one that knows what it concluded.
      const headline = str(c.headline);
      return { headline: headline ?? 'recorded an analysis', detail: str(c.detail) };
    }
    case 'AGENT_HANDOFF': {
      const started = c.started === true;
      const rules = strList(c.rule_ids);
      return {
        headline: started ? 'woken and running' : 'selected, not started',
        detail: rules.length ? `rule ${rules.join(', ')}` : null,
      };
    }
    case 'BRIEF_EMITTED': {
      const version = typeof c.version === 'number' ? c.version : null;
      const stage = str(c.stage);
      return {
        headline: version === null ? 'emitted a brief' : `emitted brief v${version}`,
        detail: stage ? stage.toLowerCase() : null,
      };
    }
    case 'FACT_OBSERVED': {
      const key = str(c.canonical_key);
      return { headline: 'observed a fact', detail: key ?? null };
    }
    case 'NOTIFICATION_SENT': {
      // `target` is what the recorder actually writes; the others are read
      // because a notification shaped by another producer names it differently
      // and a card with no subject is worse than one with a spare lookup.
      const target = str(c.target ?? c.partner_id ?? c.partner ?? c.recipient);
      const ref = str(c.external_ref);
      const autonomous = c.autonomous === true;
      return {
        headline: target ? `notified ${target}` : 'notified a partner',
        // Whether it needed a human is the distinction that matters here:
        // telling an agency is autonomous, committing them is not.
        detail: [autonomous ? 'autonomous' : 'approved', ref].filter(Boolean).join(' · ') || null,
      };
    }
    case 'IC_RESOLUTION': {
      const conflict = str(c.conflict_id);
      return { headline: 'IC resolved a disagreement', detail: conflict };
    }
    case 'APPROVAL_GRANTED':
      return { headline: 'approval granted', detail: str(c.approval_id) };
    case 'POLICY_DECISION':
      return { headline: 'policy decided', detail: str(c.rule_id) };
    case 'BENCHMARK':
      return { headline: 'benchmark recorded', detail: str(c.benchmark_type) };
    default:
      // A type this build does not know is shown as itself. Better a bare type
      // than a sentence invented for it.
      return { headline: entry.entry_type.toLowerCase().replace(/_/g, ' '), detail: null };
  }
}

/** The focus pointers for one agent, highest priority first. */
function pointersFor(focus: unknown, agentId: string): { headline: string | null; pointers: ActivityPointer[] } {
  if (!isRecord(focus)) return { headline: null, pointers: [] };
  const perAgent = Array.isArray(focus.per_agent) ? focus.per_agent : [];
  for (const raw of perAgent) {
    if (!isRecord(raw) || raw.agent_id !== agentId) continue;
    const pointers = (Array.isArray(raw.pointers) ? raw.pointers : [])
      .filter(isRecord)
      .map((p) => ({
        kind: String(p.kind ?? ''),
        ref: String(p.ref ?? ''),
        reason: String(p.reason ?? ''),
        priority: typeof p.priority === 'number' ? p.priority : 5,
      }))
      .filter((p) => p.ref !== '')
      // The focus records its own read order and it is stable for replay.
      .sort((a, b) => a.priority - b.priority || a.ref.localeCompare(b.ref));
    return { headline: str(raw.headline), pointers };
  }
  return { headline: null, pointers: [] };
}

/**
 * What one entry says in this agent's own terms, as structured lines.
 *
 * Per entry, not aggregated across the agent's history: a message is one
 * action, and a count of everything the agent has ever done belongs on the
 * stream's header rather than on each of its rows. An entry with nothing
 * structured to add renders as headline and detail alone, which is a complete
 * message -- better an unadorned row than a fabricated statistic.
 */
export function facetsForEntry(entry: IncidentLogEntryFrame, agentId: string): AgentFacet[] {
  const c = entry.content ?? {};
  const facets: AgentFacet[] = [];

  switch (entry.entry_type) {
    case 'AGENT_ANALYSIS': {
      // The faces a thermal frame was registered to. `UNSCANNED` is the word
      // the fusion engine uses and it is not "cool" -- a face with no frame is
      // unknown, and the distinction is the reason this agent exists.
      const refs = strList(c.refs);
      const faces = refs.filter((r) => /^(ALPHA|BRAVO|CHARLIE|DELTA)$/.test(r));
      if (faces.length) facets.push({ label: 'Face', value: faces.join(' · ') });
      else if (refs.length) facets.push({ label: 'Refs', value: refs.slice(0, 3).join(' · ') });
      const key = str(c.canonical_key);
      if (key) facets.push({ label: 'Key', value: key });
      break;
    }
    case 'NOTIFICATION_SENT': {
      const target = str(c.target ?? c.partner_id ?? c.partner ?? c.recipient);
      if (target) facets.push({ label: 'Agency', value: target });
      // Whether it needed a human is the distinction that matters here:
      // telling an agency is autonomous, committing them is not.
      facets.push({
        label: 'Authority',
        value: c.autonomous === true ? 'autonomous' : 'human approved',
      });
      const ref = str(c.external_ref);
      if (ref) facets.push({ label: 'Reference', value: ref });
      break;
    }
    case 'BRIEF_EMITTED': {
      const version = typeof c.version === 'number' ? c.version : null;
      if (version !== null) facets.push({ label: 'Version', value: `v${version}` });
      const stage = str(c.stage);
      if (stage) facets.push({ label: 'Stage', value: stage.toLowerCase() });
      break;
    }
    case 'INTAKE_READ': {
      const keys = strList(c.reported_keys ?? c.canonical_keys ?? c.intake_keys);
      if (keys.length) facets.push({ label: 'Caller reported', value: keys.join(' · ') });
      const unknowns = strList(c.unknowns);
      // What the call did *not* say, which is the half a commander forgets to
      // ask for. Stated rather than left to an absence.
      if (unknowns.length) facets.push({ label: 'Not stated', value: String(unknowns.length) });
      break;
    }
    case 'FOCUS_COMPOSED': {
      const pointers = typeof c.pointer_count === 'number' ? c.pointer_count : null;
      const agents = strList(c.agent_ids);
      if (pointers !== null) facets.push({ label: 'Pointers', value: String(pointers) });
      if (agents.length) facets.push({ label: 'To agents', value: String(agents.length) });
      break;
    }
    case 'BENCHMARK': {
      const type = str(c.benchmark_type);
      if (type) facets.push({ label: 'Benchmark', value: type });
      const ms = typeof c.elapsed_ms === 'number' ? c.elapsed_ms : null;
      if (ms !== null) facets.push({ label: 'Elapsed', value: `${Math.round(ms)} ms` });
      break;
    }
    case 'FACT_OBSERVED': {
      const key = str(c.canonical_key);
      if (key) facets.push({ label: 'Key', value: key });
      const source = str(c.source_type ?? c.source_id);
      if (source) facets.push({ label: 'Source', value: source });
      break;
    }
    default:
      break;
  }

  // The recorder is the one agent whose subject is the record itself, so its
  // messages say which kind of entry it just wrote. Without this every recorder
  // row read "wrote the log" and none of them said what.
  if (agentId === 'incident-recorder' && facets.length === 0) {
    facets.push({ label: 'Entry', value: entry.entry_type.toLowerCase().replace(/_/g, ' ') });
  }
  return facets;
}

/**
 * Which single agent a message belongs to.
 *
 * One entry, one actor. An analysis carries both the recorder and the analysing
 * agent in `agent_versions`, and crediting both would print the message twice
 * -- and since the recorder writes *everything*, half the stream would be
 * recorder rows duplicating other agents' work. The subject wins where the
 * entry names one; where it does not, the writer is the actor.
 */
export function actorFor(
  entry: IncidentLogEntryFrame,
): { agentId: string; version: string | null } {
  if (entry.entry_type === 'AGENT_ANALYSIS' || entry.entry_type === 'AGENT_HANDOFF') {
    const ref = str(entry.content?.agent_ref);
    if (ref) return splitAgentRef(ref);
  }
  if (entry.entry_type === 'NOTIFICATION_SENT') {
    // The notifier acted; the recorder wrote it down.
    return { agentId: 'agency-notifier', version: null };
  }

  const writers = Object.entries(entry.agent_versions ?? {});
  if (writers.length === 0) return { agentId: 'unattributed', version: null };
  // With more than one writer and no named subject, the recorder is the one
  // that is always there -- so the *other* writer is the one that did something
  // specific to this entry.
  const specific = writers.find(([id]) => id !== 'incident-recorder') ?? writers[0];
  if (!specific) return { agentId: 'unattributed', version: null };
  return { agentId: specific[0], version: specific[1] || null };
}

/**
 * Every action, newest first.
 *
 * Exported and pure so the attribution rules are testable without a stream: the
 * subtle one -- a handoff belongs to the agent it woke, not to the recorder
 * that wrote it down -- is exactly the kind of thing that looks right on screen
 * while being wrong.
 */
export function activityStreamFrom(
  entries: readonly IncidentLogEntryFrame[],
): ActivityMessage[] {
  const messages: ActivityMessage[] = [];
  const seen = new Set<string>();

  const push = (message: ActivityMessage) => {
    // A reconnect replays frames it already delivered. Keyed by id so a replayed
    // entry updates nothing and duplicates nothing.
    if (seen.has(message.id)) return;
    seen.add(message.id);
    messages.push(message);
  };

  for (const entry of entries) {
    const { agentId, version } = actorFor(entry);
    const { headline, detail } = describeEntry(entry);

    push({
      id: `${entry.sequence}:${agentId}`,
      sequence: entry.sequence,
      at: entry.occurred_at,
      agentId,
      version,
      entryType: entry.entry_type,
      headline,
      detail,
      facets: facetsForEntry(entry, agentId),
      kind: 'action',
      handoffHeadline: null,
      pointers: [],
      ruleIds:
        entry.entry_type === 'AGENT_HANDOFF' ? strList(entry.content?.rule_ids) : [],
      missingScopes:
        entry.entry_type === 'AGENT_HANDOFF' ? strList(entry.content?.missing_scopes) : [],
    });

    if (entry.entry_type === 'FOCUS_COMPOSED') {
      // One entry, several messages: this is the slow loop handing knowledge to
      // each incident agent, and it is per agent. Drawn as its own kind because
      // knowledge arriving is not the same claim as work done.
      const focus = entry.content?.focus;
      for (const target of strList(entry.content?.agent_ids)) {
        const { headline: composed, pointers } = pointersFor(focus, target);
        if (!composed && pointers.length === 0) continue;
        push({
          id: `${entry.sequence}:handoff:${target}`,
          sequence: entry.sequence,
          at: entry.occurred_at,
          agentId: target,
          version: null,
          entryType: entry.entry_type,
          headline: 'handed a focus by the slow loop',
          detail: null,
          facets: pointers.length
            ? [{ label: 'References', value: String(pointers.length) }]
            : [],
          kind: 'handoff',
          handoffHeadline: composed,
          pointers,
          ruleIds: [],
          missingScopes: [],
        });
      }
    }
  }

  // Newest first. Ties broken so a handoff sits under the composition that
  // produced it rather than above it, and so the order is stable for replay.
  return messages.sort(
    (a, b) => b.sequence - a.sequence || a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id),
  );
}


// ------------------------------------------------------------------ the panel

/** `2026-08-27T08:00:03Z` -> `08:00:03`. The clock, not the date. */
function clockOf(at: string): string {
  const parsed = new Date(at);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(11, 19);
}

export function AgentActivity({ entries }: { entries: readonly IncidentLogEntryFrame[] }) {
  const messages = useMemo(() => activityStreamFrom(entries), [entries]);
  const agentCount = useMemo(
    () => new Set(messages.map((m) => m.agentId)).size,
    [messages],
  );

  /**
   * Which messages are new since the last render, so only those animate.
   *
   * A ref rather than state: this is read during render to decide a class and
   * must not itself cause one. Without it every message re-animates whenever
   * any message arrives, and a stream of twenty rows flashes in full each time
   * an agent does anything.
   */
  const seenRef = useRef<Set<string>>(new Set());
  const arrived = new Set<string>();
  for (const message of messages) {
    if (!seenRef.current.has(message.id)) arrived.add(message.id);
  }
  useEffect(() => {
    for (const message of messages) seenRef.current.add(message.id);
  }, [messages]);

  return (
    <section
      aria-labelledby="agent-activity-heading"
      className="flex shrink-0 flex-col bg-ground px-4 py-3"
      data-testid="agent-activity"
    >
      <div className="flex shrink-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        {/* Not "Incident loop": that is the fleet column's name, and two
            landmarks with the same name are ambiguous to anyone navigating by
            one. This panel is what the agents are *doing*; that one is who they
            are. */}
        <h2
          id="agent-activity-heading"
          className="text-label uppercase tracking-widest text-muted"
        >
          Agent activity
        </h2>
        <span className="font-mono text-micro text-muted">
          {messages.length === 0
            ? 'waiting for the first entry'
            : `${messages.length} action${messages.length === 1 ? '' : 's'} · ${agentCount} agent${
                agentCount === 1 ? '' : 's'
              }`}
        </span>
      </div>

      {messages.length === 0 ? (
        <p className="mt-2 border border-dashed border-line px-3 py-2 text-micro text-muted">
          Nothing recorded yet. Messages appear as the log is written, not before —
          an empty panel here means no agent has acted, which is a fact rather
          than a gap.
        </p>
      ) : (
        /* Bounded height, and it scrolls inside itself.
           A viewport cap rather than a flex share: the column this sits in is
           itself `overflow-y-auto`, so its height is driven by its content and
           `flex-1` here would resolve against nothing and simply grow. Since
           the stream is unbounded -- every action is kept -- an unbounded panel
           would push the brief off the bottom of the screen by the end of a
           sweep, which is the failure the old one-card-per-agent design was
           avoiding the wrong way. 45vh holds five or six messages with the
           brief still visible under it. */
        <ol
          className="mt-2 max-h-[45vh] space-y-1.5 overflow-y-auto pr-1"
          aria-label="Agent actions, most recent first"
          data-testid="activity-stream"
        >
          {messages.map((message) => {
            const identity = identityFor(message.agentId);
            const isHandoff = message.kind === 'handoff';
            return (
              <li
                key={message.id}
                data-testid={`activity-message-${message.id}`}
                data-agent={message.agentId}
                data-kind={message.kind}
                className={`overflow-hidden rounded-md border bg-surface/60 ${
                  isHandoff ? 'border-dashed border-line' : 'border-line'
                } ${arrived.has(message.id) ? 'activity-message-arrived' : ''}`}
                // The agent's hue, as a left edge rather than a fill. A tinted
                // card would put a colour behind body text and force every
                // contrast decision to be made four times; an edge carries the
                // identity and leaves the text on `surface` where it was
                // designed to be read.
                style={{ borderLeftColor: identity.color, borderLeftWidth: 3 }}
              >
                <div className="p-2.5">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                    <span className="flex items-baseline gap-1.5">
                      {/* The glyph is the second channel. Deutan separation
                          between the purple and the blue is ΔE 5.0, so shape
                          and name do the work colour cannot. */}
                      <span aria-hidden="true" style={{ color: identity.color }}>
                        {identity.glyph}
                      </span>
                      <span className="font-mono text-body text-ink">{message.agentId}</span>
                      {message.version && (
                        <span className="font-mono text-micro text-muted">
                          @{message.version}
                        </span>
                      )}
                    </span>
                    <span className="font-mono text-micro text-muted">
                      {clockOf(message.at)}
                    </span>
                  </div>

                  <p className="mt-1 text-micro text-ink">
                    {isHandoff && (
                      // Said in words, not by the dashed border alone: this row
                      // is knowledge arriving from the slow loop, not this
                      // agent having done something.
                      <span className="text-muted">from the slow loop · </span>
                    )}
                    {message.headline}
                    {message.detail && (
                      <span className="text-muted"> · {message.detail}</span>
                    )}
                  </p>

                  {message.facets.length > 0 && (
                    <dl className="mt-1 space-y-0.5">
                      {message.facets.map((facet) => (
                        <div
                          key={facet.label}
                          className="flex items-baseline justify-between gap-3"
                        >
                          <dt className="text-micro text-muted">{facet.label}</dt>
                          <dd
                            className="text-right font-mono text-micro"
                            style={{ color: identity.color }}
                          >
                            {facet.value}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}

                  {message.ruleIds.length > 0 && (
                    <p className="mt-0.5 font-mono text-micro text-muted">
                      selected by {message.ruleIds.join(', ')}
                    </p>
                  )}

                  {message.missingScopes.length > 0 && (
                    <p className="mt-0.5 text-micro text-alarm">
                      not started — the incident grant does not cover{' '}
                      {message.missingScopes.join(', ')}
                    </p>
                  )}

                  {message.handoffHeadline && (
                    <p className="mt-1 text-micro text-ink">{message.handoffHeadline}</p>
                  )}

                  {message.pointers.length > 0 && (
                    /* Collapsed by default.
                     *
                     * The handoff is the most interesting thing in the stream
                     * and the longest -- five references with a sentence each
                     * ran one message past the height of the column. The
                     * headline above stays visible because it is one line and
                     * it is the point; the references are one click away. */
                    <details className="mt-1 border-t border-line pt-1">
                      <summary className="cursor-pointer text-micro uppercase tracking-widest text-muted hover:text-ink">
                        {message.pointers.length} reference
                        {message.pointers.length === 1 ? '' : 's'}
                      </summary>
                      <ul className="mt-1 space-y-0.5">
                        {message.pointers.slice(0, 4).map((pointer) => (
                          <li key={`${pointer.kind}:${pointer.ref}`} className="text-micro">
                            {/* The reference, then why it matters. Never a
                                value: the focus carries ids and keys by
                                construction. */}
                            <span className="font-mono text-disputed">{pointer.ref}</span>
                            <span className="text-muted"> — {pointer.reason}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
