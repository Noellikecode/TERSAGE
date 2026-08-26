/**
 * One agent, as a single selectable line.
 *
 * The fleet used to draw nine cards at once, each fifteen lines deep, and the
 * standby page was five screens tall before anything happened. What an officer
 * reads at rest is which agents exist and which are working; everything else --
 * the publisher, the pinned version, the glyph, the terminal -- is a question
 * asked about *one* agent, and it is answered in the detail pane.
 *
 * A button rather than a hoverable div, because hover is not an input method
 * everyone has. Pointer, keyboard focus and click all reach the same pane, and
 * the row that is showing is the row that says so.
 *
 * It is sized to be pressed. The row used to be six-point type in a 320px
 * rail -- a target under a centimetre tall on the display this runs on, which
 * is a fine size for a log line and the wrong size for the primary control of
 * the left third of the screen. The id reads at body size, the glyph is drawn
 * large enough to carry state across a room, and the whole row clears the
 * 44px touch target a gloved hand on a tablet needs.
 */

import type { AgentDescriptorView } from '@/lib/api/types';

/** The three states the console can tell apart. Named here so the row, the
 *  pane and the tests all use one vocabulary. */
export type FleetState = 'running' | 'active' | 'idle';

const GLYPH: Record<FleetState, string> = {
  running: '◆',
  active: '●',
  idle: '·',
};

const TONE: Record<FleetState, string> = {
  running: 'text-live',
  active: 'text-confirmed',
  idle: 'text-muted',
};

export function FleetRow({
  agent,
  state,
  metric,
  selected,
  onSelect,
  onPreview,
}: {
  agent: AgentDescriptorView;
  state: FleetState;
  /** The one number that moves. Already formatted by the panel. */
  metric: string;
  selected: boolean;
  /** Click, or keyboard activation: pins this agent in the pane. */
  onSelect: () => void;
  /** Pointer or focus: previews without pinning. */
  onPreview: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        onMouseEnter={onPreview}
        onFocus={onPreview}
        aria-current={selected ? 'true' : undefined}
        data-testid={`fleet-row-${agent.agent_id}`}
        data-state={state}
        className={`flex w-full items-baseline gap-3 border-l-4 px-3 py-2.5 text-left text-body transition-colors ${
          selected
            ? 'border-live bg-raised text-ink'
            : 'border-transparent text-muted hover:bg-raised hover:text-ink'
        }`}
      >
        {/* Never colour alone: the glyph carries the state too. */}
        <span aria-hidden="true" className={`text-title leading-none ${TONE[state]}`}>
          {GLYPH[state]}
        </span>
        <span className="flex-1 truncate font-mono">{agent.agent_id}</span>
        <span className="shrink-0 tabular-nums text-label text-muted">{metric}</span>
        {/* The state as a word, for anyone who cannot see the glyph. */}
        <span className="sr-only">{state}</span>
      </button>
    </li>
  );
}
