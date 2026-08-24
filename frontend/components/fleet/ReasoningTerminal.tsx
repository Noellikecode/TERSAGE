/**
 * One agent's reasoning terminal: a tail, not a transcript.
 *
 * It prints decisions and outcomes -- what the policy engine allowed, what was
 * blocked, what left the department -- and never the contents of a record. The
 * audit views are redacted before they leave the backend, and `detailText`
 * fences the values again on the way in, because this is the only surface on
 * the screen that prints backend strings verbatim. A record that never held a
 * document cannot leak one, and that only stays true if nothing here prints a
 * field it does not recognise.
 *
 * Newest-last, like a console somebody left running. When an agent has done
 * nothing this session it says so, rather than filling the box.
 *
 * The newest line types itself out and the box scrolls to follow it, which is
 * what a tail looks like. Two things that typing is not allowed to be: it
 * reveals a line that already exists in full and never invents a character, and
 * it stops when the line is done. A terminal still typing is a terminal
 * claiming work, and nothing here may claim work that is not happening.
 */

import { useEffect, useRef, useState } from 'react';

import type { TerminalLine } from '@/components/fleet/derive';
import { clock } from '@/components/fleet/derive';

/** Milliseconds per character. Fast enough to read, slow enough to see. */
const TYPE_MS = 12;

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

const TONE_CLASS: Record<string, string> = {
  confirmed: 'text-confirmed',
  disputed: 'text-disputed',
  unknown: 'text-unknown',
  live: 'text-live',
  alarm: 'text-alarm',
  muted: 'text-muted',
};

/** Never colour alone: the same glyph vocabulary the status pills use. */
const TONE_GLYPH: Record<string, string> = {
  confirmed: '●',
  disputed: '▲',
  unknown: '○',
  live: '◆',
  alarm: '■',
  muted: '·',
};

export function ReasoningTerminal({
  agentId,
  lines,
}: {
  agentId: string;
  lines: TerminalLine[];
}) {
  const newest = lines[lines.length - 1];
  const box = useRef<HTMLOListElement | null>(null);
  //: How much of the newest line has been revealed. `Infinity` means all of it,
  //: which is the state every line that is not the newest one is in.
  const [shown, setShown] = useState(Number.POSITIVE_INFINITY);

  const typed = newest ? visibleLength(newest) : 0;

  useEffect(() => {
    if (!newest) return;
    if (prefersReducedMotion()) {
      setShown(Number.POSITIVE_INFINITY);
      return;
    }
    setShown(0);
    let at = 0;
    const timer = setInterval(() => {
      at += 1;
      if (at >= typed) {
        // Done is done: the interval clears and the line is just text.
        setShown(Number.POSITIVE_INFINITY);
        clearInterval(timer);
        return;
      }
      setShown(at);
    }, TYPE_MS);
    return () => clearInterval(timer);
    // Keyed on the line's identity, so a re-render with the same newest line
    // does not retype it.
  }, [newest?.id, typed, newest]);

  useEffect(() => {
    // Follow the tail. `scrollTop` rather than `scrollIntoView`, which would
    // scroll the page and can move focus away from whatever a keyboard user
    // was on.
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, shown]);

  return (
    <ol
      ref={box}
      aria-label={`${agentId} reasoning terminal`}
      data-testid={`fleet-terminal-${agentId}`}
      className="mt-2 max-h-28 overflow-y-auto border border-line bg-ground p-2 font-mono text-micro leading-4"
    >
      {lines.length === 0 ? (
        <li className="text-muted">no activity this session</li>
      ) : (
        lines.map((line, index) => (
          <li
            key={line.id}
            // The newest line flashes once as it arrives and then stops. The
            // class rides on a keyed element, so the highlight replays only
            // when a genuinely new line lands -- a re-render with nothing new
            // reuses the element and nothing moves.
            className={`whitespace-pre-wrap break-words ${
              index === lines.length - 1 ? 'fleet-fresh' : ''
            }`}
          >
            <span className="text-muted">{clock(line.at)} </span>
            <span className={TONE_CLASS[line.tone] ?? 'text-muted'}>
              <span aria-hidden="true">{TONE_GLYPH[line.tone] ?? '·'}</span> {line.label}
            </span>
            {line.actor && <span className="text-muted"> [{line.actor}]</span>}
            {line.body &&
              (index === lines.length - 1 && shown !== Number.POSITIVE_INFINITY ? (
                /* Mid-type. The reveal is *visual only*: the clipped text is
                   hidden from assistive tech and the whole line is exposed to
                   it, because a screen reader announcing half a sentence would
                   be a worse terminal than one that does not animate at all.
                   It also means a test, or a reader arriving mid-type, sees the
                   real line rather than a fragment. */
                <span className="text-ink">
                  {' '}
                  <span aria-hidden="true">{clip(line.body, shown)}</span>
                  <span className="sr-only">{line.body}</span>
                </span>
              ) : (
                <span className="text-ink"> {line.body}</span>
              ))}
            {line.note && <span className="text-muted"> — {line.note}</span>}
          </li>
        ))
      )}
    </ol>
  );
}

/** How many characters the newest line reveals as it types. */
function visibleLength(line: TerminalLine): number {
  return (line.body ?? '').length;
}

/**
 * The first `shown` characters of text that already exists.
 *
 * Never generates, never pads. `Infinity` is the finished state, which is also
 * what a reduced-motion reader gets on the first frame.
 */
function clip(text: string, shown: number): string {
  return shown === Number.POSITIVE_INFINITY ? text : text.slice(0, Math.max(0, shown));
}
