/**
 * The district bar: the whole district's vital signs, read at a glance.
 *
 * It sits full width directly under the header, above the fleet and above the
 * incident, because it is the one thing on screen that is true whatever mode
 * the console is in. It used to be a block of tiles in the middle column that
 * a commander had to read as a paragraph of numbers.
 *
 * Every number counts something recorded. No derived scores, no invented
 * denominators: a meter is drawn only where the backend reports both halves of
 * the ratio, and a tile with no honest denominator gets a dashed track rather
 * than a fill against a made-up scale. A zero is an honest zero, never a
 * hidden tile.
 */

import type { DistrictStatsView } from '@/lib/api/types';

type Tone = 'ink' | 'disputed' | 'muted' | 'live' | 'alarm';

const TONE_TEXT: Record<Tone, string> = {
  ink: 'text-ink',
  disputed: 'text-disputed',
  muted: 'text-muted',
  live: 'text-live',
  alarm: 'text-alarm',
};

/**
 * A two-pixel meter under the number.
 *
 * `fraction === null` means the backend reports a count with nothing to divide
 * it by. That draws a dashed, unfilled track: the tile keeps the rhythm of the
 * bar without asserting a proportion nobody measured.
 */
function Meter({ fraction, tone }: { fraction: number | null; tone: Tone }) {
  if (fraction === null) {
    return (
      <div
        aria-hidden="true"
        className="mt-1.5 h-0.5 w-full border-t border-dashed border-line"
        data-testid="meter-unscaled"
      />
    );
  }
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  return (
    <div aria-hidden="true" className="mt-1.5 h-0.5 w-full bg-line" data-testid="meter">
      <div
        className={`h-full ${tone === 'disputed' ? 'bg-disputed' : tone === 'live' ? 'bg-live' : tone === 'alarm' ? 'bg-alarm' : 'bg-ink'}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/** A ring for the source read. Same arithmetic as a meter, different shape. */
function Ring({ fraction, tone }: { fraction: number; tone: Tone }) {
  const r = 7;
  const circumference = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(1, fraction)) * circumference;
  const stroke =
    tone === 'alarm' ? '#f87171' : tone === 'disputed' ? '#fbbf24' : tone === 'live' ? '#38bdf8' : '#e8edf4';
  return (
    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18" data-testid="source-ring">
      <circle cx="9" cy="9" r={r} fill="none" stroke="#2a323d" strokeWidth="2" />
      <circle
        cx="9"
        cy="9"
        r={r}
        fill="none"
        stroke={stroke}
        strokeWidth="2"
        strokeDasharray={`${filled} ${circumference - filled}`}
        transform="rotate(-90 9 9)"
      />
    </svg>
  );
}

function Metric({
  label,
  value,
  caption,
  fraction,
  tone = 'ink',
  live = false,
}: {
  label: string;
  value: number;
  caption: string;
  fraction: number | null;
  tone?: Tone;
  live?: boolean;
}) {
  return (
    <div className="min-w-0 border border-line bg-surface px-3 py-1.5">
      <dt className="flex items-center gap-1.5 text-micro uppercase tracking-widest text-muted">
        {label}
        {/* A dot, not a sentence: something is out right now. */}
        {live && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-live pulse-live" />}
      </dt>
      <dd className="mt-0.5">
        <span className={`font-mono text-2xl leading-none ${TONE_TEXT[tone]}`}>{value}</span>
        <Meter fraction={fraction} tone={tone} />
        <span className="mt-1 block truncate text-micro text-muted" title={caption}>
          {caption}
        </span>
      </dd>
    </div>
  );
}

/** Guard against 0/0: a district with no profiles has no proportion to draw. */
function share(part: number, whole: number): number | null {
  return whole > 0 ? part / whole : null;
}

export function DistrictStrip({ stats }: { stats: DistrictStatsView | null }) {
  if (!stats) {
    return (
      <section aria-labelledby="district-heading">
        <h2 id="district-heading" className="sr-only">
          District readiness
        </h2>
        <p className="border border-dashed border-line px-3 py-2 text-micro text-muted">
          District statistics UNAVAILABLE — the backend reported none. Nothing is inferred here.
        </p>
      </section>
    );
  }

  const unavailable = stats.sources.filter((s) => !s.available);
  const fixtures = stats.sources.filter((s) => s.mode === 'FIXTURE');
  const sourceTone: Tone = unavailable.length > 0 ? 'alarm' : fixtures.length > 0 ? 'disputed' : 'ink';

  return (
    <section aria-labelledby="district-heading" data-testid="district-bar">
      <h2 id="district-heading" className="sr-only">
        District readiness
      </h2>
      <dl className="grid grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-6">
        <Metric
          label="Structures"
          value={stats.profiles}
          caption={`${stats.surveyed} surveyed`}
          fraction={share(stats.surveyed, stats.profiles)}
        />
        <Metric
          label="Facts"
          value={stats.facts}
          caption="provenanced"
          // A count of records with nothing to divide it by.
          fraction={null}
        />
        <Metric
          label="Open conflicts"
          value={stats.open_conflicts}
          caption={`${stats.high_severity_conflicts} at severity 4+`}
          fraction={share(stats.high_severity_conflicts, stats.open_conflicts)}
          tone={stats.open_conflicts > 0 ? 'disputed' : 'muted'}
        />
        <Metric
          label="Queued"
          value={stats.queued_for_survey}
          caption={`of ${stats.profiles} on file`}
          fraction={share(stats.queued_for_survey, stats.profiles)}
        />
        <Metric
          label="Dispatched"
          value={stats.dispatched}
          caption="companies out"
          fraction={null}
          tone={stats.dispatched > 0 ? 'live' : 'muted'}
          live={stats.dispatched > 0}
        />
        <Metric
          label="Never surveyed"
          value={stats.profiles_never_surveyed}
          caption="nobody has been inside"
          fraction={share(stats.profiles_never_surveyed, stats.profiles)}
          tone={stats.profiles_never_surveyed > 0 ? 'disputed' : 'muted'}
        />
      </dl>

      {/* Source health. Trimmed to the counts and the names, because which
          source is unreachable is the part an officer has to be able to act
          on -- an absent record from a dead source is not an absent record. */}
      <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-micro text-muted">
        <Ring
          fraction={share(stats.sources.length - unavailable.length, stats.sources.length) ?? 0}
          tone={sourceTone}
        />
        <span>{stats.sources.length} sources</span>
        {fixtures.length > 0 && (
          <span className="text-disputed">· {fixtures.length} fixture-backed</span>
        )}
        {unavailable.length > 0 && (
          <span className="text-alarm">
            · {unavailable.length} UNAVAILABLE: {unavailable.map((s) => s.source_id).join(', ')}
          </span>
        )}
      </p>
    </section>
  );
}
