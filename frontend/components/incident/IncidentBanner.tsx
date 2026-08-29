'use client';

/**
 * The incident banner and elapsed clock.
 *
 * **The street address is the largest thing on it.** It used to be the
 * `address_id` -- `sf-0450-hayes` -- which is the right key for every event,
 * grant and log entry this incident produces and the wrong thing to put in
 * front of a commander at the moment a call lands. Nobody rolls to a slug.
 * The prose address leads at display size; the id follows in parentheses,
 * quieter but still on screen, so what is said aloud and what the record is
 * keyed by are both readable and visibly the same place.
 *
 * The clock counts from CAD dispatch, not from when this tab loaded -- the
 * difference is queue time the commander already spent, and it is the number
 * they are actually tracking.
 *
 * Reduced motion stops the second-by-second tick and updates once a minute
 * instead. The clock is information, not animation.
 */

import { useEffect, useState } from 'react';

import { StatusPill } from '@/components/StatusPill';

function format(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

export function useElapsed(dispatchedAt: string, reducedMotion = false): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), reducedMotion ? 60_000 : 1000);
    return () => clearInterval(interval);
  }, [reducedMotion]);
  const started = Date.parse(dispatchedAt);
  return Number.isNaN(started) ? 0 : (now - started) / 1000;
}

/**
 * The street part of a postal address.
 *
 * The city adapter publishes `450 Hayes St, San Francisco, CA 94102`. The city
 * and the ZIP are constant across a district and cost the line its size, so the
 * banner shows the part that identifies the building and nothing else. Split on
 * the first comma only, so an address that happens to contain no comma is
 * returned whole rather than truncated to nothing.
 */
export function streetPart(display: string): string {
  const [street] = display.split(',');
  return (street ?? display).trim() || display;
}

export function IncidentBanner({
  incidentId,
  addressId,
  addressDisplay = '',
  alarmLevel,
  dispatchedAt,
  coldStart,
  onClose,
  closing = false,
  busy = false,
  reducedMotion = false,
}: {
  incidentId: string;
  addressId: string;
  /** Empty when the city could not place the id; the banner then shows the id
      at display size rather than printing nothing. */
  addressDisplay?: string;
  alarmLevel: number;
  dispatchedAt: string;
  coldStart: boolean;
  onClose?: () => void;
  closing?: boolean;
  /**
   * Some other write is in flight.
   *
   * Separate from `closing` because the two answer different questions, and
   * conflating them is what put `Closing…` at the top of a live incident every
   * time an officer notified an agency: one flag drove both the disabled state
   * and the word, so any of eight writes announced a close. `closing` now means
   * only "this button's own action is running" and owns the label; this one
   * means "a write is running" and owns nothing but the disabled state -- which
   * still has to be honoured, because closing an incident while another write
   * is in flight is the race that flag was there to prevent.
   */
  busy?: boolean;
  reducedMotion?: boolean;
}) {
  const elapsed = useElapsed(dispatchedAt, reducedMotion);

  return (
    <div
      className="flex flex-wrap items-center gap-3 border-b-2 border-alarm bg-raised px-4 py-2"
      role="region"
      aria-label="Active incident"
    >
      <StatusPill tone="alarm" label={`alarm ${alarmLevel}`} />
      <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span className="font-mono text-hero leading-tight text-ink">
          {addressDisplay ? streetPart(addressDisplay) : addressId}
        </span>
        {addressDisplay && (
          <span className="font-mono text-body text-muted">({addressId})</span>
        )}
      </span>
      <span className="font-mono text-micro text-muted">{incidentId}</span>

      <span className="ml-auto flex items-baseline gap-2">
        <span className="text-micro uppercase tracking-widest text-muted">Elapsed</span>
        <output
          className="font-mono text-2xl tabular-nums text-ink"
          aria-label={`Elapsed since dispatch: ${format(elapsed)}`}
        >
          {format(elapsed)}
        </output>
      </span>

      {coldStart && (
        <StatusPill
          tone="unknown"
          label="cold start"
          title="No pre-incident profile existed for this structure."
        />
      )}

      {onClose && (
        <button
          type="button"
          onClick={onClose}
          disabled={closing || busy}
          className="border border-line px-3 py-1 text-micro uppercase tracking-wide text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
        >
          {closing ? 'Closing…' : 'Close incident'}
        </button>
      )}
    </div>
  );
}
