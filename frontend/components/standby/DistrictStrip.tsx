/**
 * The district metric strip.
 *
 * The first thing a battalion chief sees. Every number counts something
 * recorded -- no derived scores, and a zero is an honest zero rather than a
 * hidden tile.
 */

import type { DistrictStatsView } from '@/lib/api/types';

function Metric({
  label,
  value,
  detail,
  tone = 'ink',
}: {
  label: string;
  value: number | string;
  detail?: string;
  tone?: 'ink' | 'disputed' | 'muted';
}) {
  const toneClass =
    tone === 'disputed' ? 'text-disputed' : tone === 'muted' ? 'text-muted' : 'text-ink';
  return (
    <div className="border border-line bg-surface px-3 py-2">
      <dt className="text-micro uppercase tracking-widest text-muted">{label}</dt>
      <dd className={`mt-1 font-mono text-xl ${toneClass}`}>{value}</dd>
      {detail && <p className="mt-0.5 text-micro text-muted">{detail}</p>}
    </div>
  );
}

export function DistrictStrip({ stats }: { stats: DistrictStatsView | null }) {
  if (!stats) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        No district statistics available. The backend did not report any; nothing
        is inferred here.
      </p>
    );
  }

  const unavailable = stats.sources.filter((s) => !s.available);
  const fixtures = stats.sources.filter((s) => s.mode === 'FIXTURE');

  return (
    <section aria-labelledby="district-heading">
      <h2 id="district-heading" className="sr-only">
        District readiness
      </h2>
      <dl className="grid grid-cols-2 gap-px sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="Structures" value={stats.profiles} detail="with a profile" />
        <Metric label="Facts" value={stats.facts} detail="provenanced" />
        <Metric
          label="Open conflicts"
          value={stats.open_conflicts}
          detail={`${stats.high_severity_conflicts} at severity 4+`}
          tone={stats.open_conflicts > 0 ? 'disputed' : 'muted'}
        />
        <Metric label="Queued" value={stats.queued_for_survey} detail="for survey" />
        <Metric label="Dispatched" value={stats.dispatched} detail="companies out" />
        <Metric
          label="Never surveyed"
          value={stats.profiles_never_surveyed}
          detail="nobody has been inside"
          tone={stats.profiles_never_surveyed > 0 ? 'disputed' : 'muted'}
        />
      </dl>

      <p className="mt-2 text-micro text-muted">
        {stats.sources.length} sources configured
        {fixtures.length > 0 && (
          <>
            {' · '}
            <span className="text-disputed">{fixtures.length} fixture-backed</span>
          </>
        )}
        {unavailable.length > 0 && (
          <>
            {' · '}
            <span className="text-alarm">
              {unavailable.length} UNAVAILABLE: {unavailable.map((s) => s.source_id).join(', ')}
            </span>
          </>
        )}
      </p>
    </section>
  );
}
