# Fleet panel: rows plus one detail pane

**Date:** 2026-08-24
**Status:** approved, not yet implemented

## Problem

The standby console renders nine `AgentCard`s at once, each carrying fifteen or
more lines. The page is 4,713px tall at 1440px wide — five screens of scrolling
before anything happens. Measured, not estimated.

Three avoidable costs, on every card:

- **Labels doubled with values.** `Publisher → by fire`, `Pinned version → pin
  @1.0.0`. Two lines spent on one fact.
- **Two renderings of idle.** A per-agent sentence ("No partner contacted this
  session.") stacked on the generic `no activity this session`.
- **The superseded explanation renders once per column.** The same forty words
  appear twice on screen.

The names are not the problem. They are one line in fifteen, and "which agent
produced this" is the product's whole claim — a NIOSH investigator reads it two
years after a fatal fire. Replacing them with glyphs behind a hover would cut
2% of the words and hide the answer the system exists to give.

## What this does not change

`visuals.tsx` (655 lines of per-agent glyphs) and `ReasoningTerminal` stay
exactly as they are. They are the fleet's most distinctive output. The defect is
that they are drawn nine times simultaneously, not that they exist.

## Design

### The row

Nine rows, one line each: status dot, agent id, state, one live number.

The number keeps `AgentCard`'s existing rule exactly: `${throughput} runs` when
a caller supplied live activity, `${recorded} recorded` otherwise. The console
does not invent a run count it was not given.

```
● records-watcher      ACTIVE    16 recorded
· geometry-watcher     IDLE       0 recorded
```

Dropped from the row: role summary, the four-item provenance list, the glyph,
the reasoning terminal, the write-target line.

Each row is a `<button>`, so hover, keyboard focus and click all reach the same
target. The three states already computed in `AgentCard` — `running`, `active`,
`idle` — carry over unchanged; the distinction is load-bearing and the console
must not collapse it.

### The detail pane

One fixed region beside the rows. Nothing floats and nothing reflows.

For the selected agent: the full role summary, a single provenance line
(`Publisher fire · Pinned @1.0.0 · 5s budget · 16 recorded`), the glyph, the
reasoning terminal, and the write target with its approval level when it has
one. Version drift keeps its `disputed` styling — a pin that no longer matches
the catalog is a finding, not a detail.

Labels lose their doubled words: `Publisher fire`, not `Publisher → by fire`.

### Selection

- **Hover** previews.
- **Click** pins, so the pane holds while the mouse moves away. This is what
  survives being narrated over on video.
- **Arrow keys** move between rows; focus previews the same as hover.
- **On load**, the first agent in **catalog order** whose state is `running` or
  `active` is selected, so the pane is never empty and never opens on something
  idle. Catalog order, not activity order, for the reason the console already
  gives for column placement: a row that moved when it wrote a fact would be
  unreadable at the moment it was worth reading.

### Layout

Standby: rows in a narrow left column, pane to the right. During an incident the
fleet is already a 400px column, so rows stack above the pane. Same components,
one breakpoint. The existing `auto-fit` grid is replaced, not parameterised.

**`CommandCenter` stops splitting the fleet in half.** It currently computes
`slowLeft`/`slowRight` and `incidentLeft`/`incidentRight` and renders
`FleetPanel` twice per loop — which is what puts "4 OF 7" and "3 OF 7" on
screen. Two panels would mean two detail panes, each with its own selection.
One panel per loop, handed the whole loop.

This also closes a gap the layout documents and leaves open: `FleetPanel` builds
its `fleetIds` attribution set from the agents it was handed, so a column given
half the fleet knows half the fleet, and one agent's work on a shared write
target can surface in another's reasoning box. Handing it the whole loop makes
`fleetRoster` redundant for this call site rather than merely correct.

`AgentRail` is a one-line alias for `FleetPanel` and needs no change.

### The superseded group

Collapses to one line: `4 superseded · still catalogued`. Selecting it shows the
existing explanation in the detail pane — rendered **once**, which fixes the
duplicate.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `FleetRow` | one agent as a single selectable line | descriptor, activity, selected state |
| `FleetDetail` | everything about the selected agent | descriptor, `FleetContext`, glyph, terminal |
| `FleetPanel` | owns selection, lays out rows and pane | the two above |
| `AgentCard` | **removed** — its body moves into `FleetDetail` | — |

`VisualFor` moves from `AgentCard` to `FleetDetail` unchanged, including its
`default` branch. `FleetContext` is untouched; the attribution rule that needs
the full roster (`fleetIds`) is unaffected by where the glyph is drawn.

## Error and empty states

- **No agents published** — the existing empty message stands, unchanged.
- **Every agent idle** — selection falls back to the first row rather than
  leaving the pane blank.
- **No subscription for an agent** — the pane keeps rendering `not subscribed`
  as the card does now. Absent is not zero.
- **An agent with no glyph** — `GenericTicks`, as today.

## Testing

`tests/fleet.test.tsx` holds 34 references to card internals and is the only
file affected. Rewritten to assert:

1. Nine rows render, one line each, for a nine-agent fleet.
2. Selecting a row shows that agent's detail and no other's.
3. Every row is reachable by keyboard, and focus previews.
4. Click holds selection after the pointer leaves.
5. The pane opens on an active agent when one exists.
6. The superseded explanation appears exactly once in the document.
7. A drifted pin still renders as disputed.

Accessibility: rows are buttons in a list; the pane is an `aria-live` region so
a screen reader announces the change on selection.

## Out of scope

- The 135 survey-queue chips. A separate wall of text, separate fix.
- The interceptor card's repeated `grant_id kind alarm_level …` line, which
  reads like field names rendering without values. Unverified; needs its own
  look.
