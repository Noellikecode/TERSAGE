/**
 * The district bar: four numbers, read across a room.
 *
 * It sits full width directly under the header, above the fleet and above the
 * incident, because it is the one thing on screen that is true whatever mode
 * the console is in.
 *
 * **It used to carry six metrics, each with a caption.** `Structures / 8
 * surveyed`, `Facts / provenanced`, `Never surveyed / nobody has been inside`
 * -- twelve pieces of text to deliver six numbers, all of them at eleven
 * pixels, so the most important figure on the page was set at exactly the size
 * of its own footnote. Nothing could be scanned; the bar had to be read.
 *
 * What is here now is what an officer walking past the screen has to be able to
 * take in without stopping: how many structures, how many disagreements are
 * open, how many are queued for somebody to go and look, how many nobody has
 * ever been inside. `Facts` left because a count of records is a vanity number
 * to everyone except the person who built it, and `Dispatched` left because it
 * belongs to the incident view -- it is dead space in standby and it is already
 * unmissable when a company is out.
 *
 * Every number still counts something recorded. No derived scores, no invented
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

const TONE_BAR: Record<Tone, string> = {
  ink: 'bg-ink',
  disputed: 'bg-disputed',
  muted: 'bg-muted',
  live: 'bg-live',
  alarm: 'bg-alarm',
};

/**
 * The meter under the number.
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
        className="mt-3 h-1 w-full border-t border-dashed border-line"
        data-testid="meter-unscaled"
      />
    );
  }
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  return (
    <div aria-hidden="true" className="mt-3 h-1 w-full bg-line" data-testid="meter">
      <div className={`h-full ${TONE_BAR[tone]}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

/** A ring for the source read. Same arithmetic as a meter, different shape. */
function Ring({ fraction, tone }: { fraction: number; tone: Tone }) {
  const r = 7;
  const circumference = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(1, fraction)) * circumference;
  const stroke =
    tone === 'alarm'
      ? '#f87171'
      : tone === 'disputed'
        ? '#fbbf24'
        : tone === 'live'
          ? '#38bdf8'
          : '#e8edf4';
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
  fraction,
  tone = 'ink',
  live = false,
}: {
  label: string;
  value: number;
  fraction: number | null;
  tone?: Tone;
  live?: boolean;
}) {
  return (
    <div className="min-w-0 border border-line bg-surface px-4 py-3">
      <dt className="flex items-center gap-2 text-label uppercase text-muted">
        {label}
        {/* A dot, not a sentence: something is out right now. */}
        {live && <span aria-hidden="true" className="h-2 w-2 rounded-full bg-live pulse-live" />}
      </dt>
      <dd className="mt-2">
        <span className={`font-mono text-hero ${TONE_TEXT[tone]}`}>{value}</span>
        <Meter fraction={fraction} tone={tone} />
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
        <p className="border border-dashed border-line px-4 py-3 text-body text-muted">
          District statistics UNAVAILABLE — the backend reported none.
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
      <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Metric
          label="Structures"
          value={stats.profiles}
          fraction={share(stats.surveyed, stats.profiles)}
        />
        <Metric
          label="Open conflicts"
          value={stats.open_conflicts}
          fraction={share(stats.high_severity_conflicts, stats.open_conflicts)}
          tone={stats.open_conflicts > 0 ? 'disputed' : 'muted'}
        />
        <Metric
          label="Queued"
          value={stats.queued_for_survey}
          fraction={share(stats.queued_for_survey, stats.profiles)}
          tone={stats.queued_for_survey > 0 ? 'ink' : 'muted'}
          live={stats.dispatched > 0}
        />
        <Metric
          label="Never surveyed"
          value={stats.profiles_never_surveyed}
          fraction={share(stats.profiles_never_surveyed, stats.profiles)}
          tone={stats.profiles_never_surveyed > 0 ? 'disputed' : 'muted'}
        />
      </dl>

      {/* Source health. Counts and names only: which source is unreachable is
          the part an officer has to be able to act on -- an absent record from
          a dead source is not an absent record. */}
      <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-body text-muted">
        <Ring
          fraction={share(stats.sources.length - unavailable.length, stats.sources.length) ?? 0}
          tone={sourceTone}
        />
        <span>{stats.sources.length} sources</span>
        {fixtures.length > 0 && (
          <span className="text-disputed">{fixtures.length} simulated</span>
        )}
        {unavailable.length > 0 && (
          <span className="text-alarm">
            {unavailable.length} UNAVAILABLE: {unavailable.map((s) => s.source_id).join(', ')}
          </span>
        )}
      </p>
    </section>
  );
}
