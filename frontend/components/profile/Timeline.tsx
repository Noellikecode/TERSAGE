/**
 * The profile timeline: append-only, gapless, in order.
 *
 * Nothing here was edited or backdated. Each entry names who wrote it and at
 * which version, which is what makes "what did we know, and when" answerable
 * two years later.
 */

import type { TimelineEventView } from '@/lib/api/types';

const MARKER: Record<string, string> = {
  FACT_WRITTEN: '·',
  CONFLICT_DETECTED: '▲',
  CONFLICT_RESOLVED: '●',
  GEOMETRY_UPDATED: '◆',
  SURVEY_COMPLETED: '●',
  REFERRAL_DRAFTED: '·',
  REFERRAL_FILED: '■',
  WORK_ORDER_DISPATCHED: '■',
  INCIDENT_OPENED: '■',
  INCIDENT_CLOSED: '■',
};

export function Timeline({ events, limit = 40 }: { events: TimelineEventView[]; limit?: number }) {
  if (events.length === 0) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        Nothing has happened to this profile yet.
      </p>
    );
  }

  // Newest first: the last thing that happened is what somebody is asking about.
  const shown = [...events].sort((a, b) => b.sequence - a.sequence).slice(0, limit);

  return (
    <ol className="space-y-1.5" aria-label="Profile timeline, newest first">
      {shown.map((event) => (
        <li key={event.sequence} className="flex gap-2 border-b border-line/60 pb-1.5">
          <span aria-hidden="true" className="text-muted">
            {MARKER[event.type] ?? '·'}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <time className="font-mono text-micro text-muted" dateTime={event.occurred_at}>
                {event.occurred_at.slice(0, 10)}
              </time>
              <span className="text-micro uppercase tracking-wide text-muted">
                {event.type.replace(/_/g, ' ').toLowerCase()}
              </span>
              <span className="font-mono text-micro text-muted">
                {event.actor}
                {event.actor_version && `@${event.actor_version}`}
              </span>
            </div>
            <p className="text-micro leading-5 text-ink">{event.summary}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}
