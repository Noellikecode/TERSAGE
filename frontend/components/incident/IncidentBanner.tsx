'use client';

/**
 * The incident banner and elapsed clock.
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

export function IncidentBanner({
  incidentId,
  addressId,
  alarmLevel,
  dispatchedAt,
  coldStart,
  onClose,
  closing = false,
  reducedMotion = false,
}: {
  incidentId: string;
  addressId: string;
  alarmLevel: number;
  dispatchedAt: string;
  coldStart: boolean;
  onClose?: () => void;
  closing?: boolean;
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
      <span className="font-mono text-lg text-ink">{addressId}</span>
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
          disabled={closing}
          className="border border-line px-3 py-1 text-micro uppercase tracking-wide text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
        >
          {closing ? 'Closing…' : 'Close incident'}
        </button>
      )}
    </div>
  );
}
