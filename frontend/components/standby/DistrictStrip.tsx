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

  /**
   * Sources that are unavailable **and were expected to answer**.
   *
   * Three of this catalogue's thirteen have no endpoint at all, and never
   * will: PHMSA restricts programmatic access to pipeline centrelines, San
   * Francisco publishes no hydrant feed, and Tier II filings are confidential
   * under EPCRA. Listing them in red beside a genuine outage said "3
   * UNAVAILABLE" on a district where nothing was wrong, every second of every
   * demo -- and taught anyone reading the screen to ignore the one line that
   * exists to tell them a feed has died.
   *
   * `UNCONFIGURED` is that permanent, documented state; the console renders
   * each source's own reason on the source panel, which is where a policy
   * belongs. What stays here is an outage: something that should have answered
   * and did not.
   */
  // Ranked, not surveyed: `structure-watch` puts every structure it has read
  // into the queue, so this climbs as the loop works through the district. A
  // survey is a person walking a building and is not something the fleet can
  // move on its own.
  const analysed = share(stats.queued_for_survey + stats.dispatched, stats.profiles) ?? 0;
  const unavailable = stats.sources.filter((s) => !s.available && s.mode !== 'UNCONFIGURED');
  const fixtures = stats.sources.filter((s) => s.mode === 'FIXTURE');
  const sourceTone: Tone = unavailable.length > 0 ? 'alarm' : fixtures.length > 0 ? 'disputed' : 'ink';

  return (
    <section aria-labelledby="district-heading" data-testid="district-bar">
      <h2 id="district-heading" className="sr-only">
        District readiness
      </h2>
      {/* One thin bar, and no numbers on it at all.
          The counters were the same figures all session -- a district's
          structure count does not move, and "never surveyed" equals it until a
          crew physically walks a building -- so the strip read as furniture on
          a screen whose whole claim is that work is happening. Reading them off
          again in words beside the bar was the same problem in smaller type.

          What is left is the one thing that genuinely changes: how much of the
          district the loop has ranked. It is a shape, not a readout. Anyone
          who wants the figures has the survey queue, the conflict panel and
          the profile, all of which carry them with their provenance attached;
          a masthead strip is for knowing at a glance that the fleet is moving.

          The fill is a gradient with a lit leading edge, and it eases over
          700ms so a pass landing is something you *see* rather than something
          you catch by comparing two numbers. */}
      <div className="flex items-center gap-3">
        <span className="shrink-0 text-label uppercase tracking-widest text-muted">
          District analysed
        </span>
        {/* A real progressbar, not a decorated div.
            Taking the figures off the strip took them off it for *everybody*,
            and a bar with no text conveys nothing to a screen reader. The
            proportion is announced through ARIA instead: sighted readers get
            the shape, everyone else gets the number, and neither is a
            second-class rendering of the other. */}
        <div
          role="progressbar"
          aria-label="District analysed"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(analysed * 100)}
          aria-valuetext={`${Math.round(analysed * 100)}% of the district ranked`}
          className="h-1.5 min-w-[8rem] flex-1 overflow-hidden rounded-full bg-raised"
        >
          <div
            className="h-full rounded-full bg-gradient-to-r from-live/40 via-live to-confirmed shadow-[0_0_8px_rgba(56,189,248,0.7)] transition-[width] duration-700 ease-out"
            style={{ width: `${Math.round(analysed * 100)}%` }}
            data-testid="district-progress"
          />
        </div>
      </div>

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
            {unavailable.length} unreachable: {unavailable.map((s) => s.source_id).join(', ')}
          </span>
        )}
      </p>
    </section>
  );
}
