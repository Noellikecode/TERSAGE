'use client';

/**
 * What the loop is doing about the entry package, while there is not one yet.
 *
 * **This exists because silence was indistinguishable from failure.** The
 * approval card is composed autonomously and appears on its own; until it does,
 * the console showed nothing at all — and a loop that was working, a loop whose
 * composition had been cancelled mid-run by a model timeout, and a loop with
 * autonomy switched off all looked exactly the same from the fireground: an
 * empty screen. That ambiguity survived several rounds of investigation because
 * there was nothing on the screen to read.
 *
 * So while an incident is open and no package has arrived, this states the
 * backend's own account of it: whether autonomy is on, whether this process is
 * even tracking the incident, the countdown to the fallback composition, and —
 * when an attempt has failed — the error and the criteria that were outstanding.
 *
 * **Nothing here is inferred.** Every line is a field the diagnostics endpoint
 * returned. When the endpoint cannot be read, that is what it says, rather than
 * a guess about what the loop is doing.
 */

import { useEffect, useState } from 'react';

import {
  readAutonomyDiagnostics,
  type AutonomyDiagnosticsView,
} from '@/lib/api/entry-packages';

/**
 * How long to let the loop work before asking after it.
 *
 * The composition is not expected early — the fallback deadline is nearly two
 * minutes out and the ready path still needs the sweep — so asking immediately
 * would put a status line on screen during the seconds an officer is reading
 * the brief. Late enough not to be noise, early enough that a loop which will
 * never compose is named long before the deadline it is not going to meet.
 */
const WATCH_AFTER_MS = 15_000;

/** How often to re-ask once it is watching. Slower than the package poll: this
 *  is a question about the loop, and the loop's own state changes slowly. */
const WATCH_INTERVAL_MS = 5000;

/** What the backend said, in one line an officer can act on. */
export function describeAutonomy(view: AutonomyDiagnosticsView): {
  text: string;
  tone: 'muted' | 'disputed' | 'alarm';
} {
  if (!view.autonomy_enabled) {
    return {
      tone: 'disputed',
      text: 'The loop will not compose an entry package on its own: autonomy is switched off for this deployment. Composing one is still a button.',
    };
  }
  if (!view.tracked) {
    return {
      tone: 'alarm',
      text: 'This backend is not tracking the incident, so no package is scheduled. It did not open this incident — a restart, or a second process answering.',
    };
  }
  if (view.failures > 0) {
    const why = view.failed_error_message || view.failed_error_type || 'no reason recorded';
    const outstanding = view.failed_criteria.length
      ? ` Outstanding when it tried: ${view.failed_criteria.join(', ')}.`
      : '';
    return {
      tone: 'alarm',
      text: `An attempt to compose the entry package did not produce one — ${why}.${outstanding}`,
    };
  }
  if (view.composing) {
    return { tone: 'muted', text: 'The interceptor is composing the entry package now.' };
  }
  const outstanding = view.outstanding_criteria.length
    ? ` Waiting on: ${view.outstanding_criteria.join(', ')}.`
    : '';
  if (view.deadline_armed && view.deadline_in_s !== null) {
    const left = Math.max(0, Math.round(view.deadline_in_s));
    return {
      tone: 'muted',
      text: `The interceptor composes the entry package in ${left}s, or sooner if the record supports it.${outstanding}`,
    };
  }
  return {
    tone: 'muted',
    text: `No composition is scheduled on this incident.${outstanding}`,
  };
}

const TONE_CLASS: Record<'muted' | 'disputed' | 'alarm', string> = {
  muted: 'text-muted',
  disputed: 'text-disputed',
  alarm: 'text-alarm',
};

export function EntryPackageWatch({
  incidentId,
  hasPackage,
}: {
  incidentId: string | null;
  /** Once one exists the card owns the screen and this has nothing to add. */
  hasPackage: boolean;
}) {
  const [view, setView] = useState<AutonomyDiagnosticsView | null>(null);
  const [unreadable, setUnreadable] = useState<string | null>(null);

  useEffect(() => {
    setView(null);
    setUnreadable(null);
    if (!incidentId || hasPackage) return;

    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | undefined;

    const ask = async () => {
      const result = await readAutonomyDiagnostics(incidentId);
      if (cancelled) return;
      if (result.ok) {
        setView(result.data);
        setUnreadable(null);
        return;
      }
      // Said, not swallowed. A console that cannot reach the backend is a
      // different fact from a loop that has decided not to compose, and the
      // whole point of this panel is to stop those reading alike.
      setUnreadable(result.error.message);
    };

    const start = setTimeout(() => {
      void ask();
      interval = setInterval(() => void ask(), WATCH_INTERVAL_MS);
    }, WATCH_AFTER_MS);

    return () => {
      cancelled = true;
      clearTimeout(start);
      if (interval !== undefined) clearInterval(interval);
    };
  }, [incidentId, hasPackage]);

  if (!incidentId || hasPackage) return null;
  if (unreadable) {
    return (
      <p className="mt-2 border border-alarm/40 bg-alarm/10 px-3 py-2 text-micro text-alarm"
         data-testid="entry-package-watch">
        The console could not ask the loop about the entry package: {unreadable}
      </p>
    );
  }
  if (!view) return null;

  const { text, tone } = describeAutonomy(view);
  return (
    <p
      className={`mt-2 border border-line px-3 py-2 text-micro ${TONE_CLASS[tone]}`}
      data-testid="entry-package-watch"
      role="status"
    >
      {text}
    </p>
  );
}
