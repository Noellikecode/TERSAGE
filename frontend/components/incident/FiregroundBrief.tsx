/**
 * The interceptor's brief, as a crew reads it at the kerb.
 *
 * This is the *only* thing on the approval card, deliberately. The package's
 * readiness table, its route legs and its citation list are all real and all
 * still reachable -- they are simply not what somebody about to go through a
 * door is reading, and putting them on the same surface is what buried the
 * brief under three screens of provenance.
 *
 * Three rules, and they are the whole file:
 *
 * **Plain words, never canonical keys.** The backend labels every line with the
 * key it came from -- `structure.lightweight_truss`, `hazard.tier_ii.present` --
 * because that is what makes a fact traceable. On a fireground card it is
 * noise: nobody standing at a building knows what `suppression.fdc_location`
 * is. Every key that can appear is translated here, and a key with no
 * translation is not shown at all rather than shown raw.
 *
 * **Only what is known.** An UNKNOWN line is a fact about the *record*, not
 * about the building, and a card of eleven UNKNOWNs teaches a crew to stop
 * reading. They are counted at the foot instead, so the absence is still
 * stated and still honest.
 *
 * **Nothing is invented.** No tactical priorities, no incident command, no
 * radio channel, no risk grade. Those would be operational instructions this
 * system holds no record for, and a crew might act on them.
 */

import type {
  BriefEmissionView,
  BriefItemView,
  EntryPathPlanView,
} from '@/lib/api/types';

/**
 * Canonical key to the words a firefighter uses, and what it means for entry.
 *
 * The second half is the point. "Lightweight truss: true" is a fact; "fails
 * early under fire" is the same fact with the consequence an officer needs
 * attached to it. Every consequence here is fixed text tied to a known key --
 * a property of trusses, not a judgement about this fire.
 */
const PLAIN: Readonly<Record<string, { label: string; consequence?: string }>> = {
  'structure.lightweight_truss': {
    label: 'Lightweight truss',
    consequence: 'fails early under fire — limit roof operations',
  },
  'hazard.tier_ii.present': {
    label: 'Tier II chemicals on site',
    consequence: 'reported to the county under EPCRA',
  },
  'hazard.solar_array': {
    label: 'Rooftop solar',
    consequence: 'DC stays live — cannot be killed at the panel',
  },
  'hazard.ev_charger': { label: 'EV charging', consequence: 'battery fire risk' },
  'egress.obstruction': { label: 'Egress obstructed' },
  'egress.stairwell_count': { label: 'Stairwells' },
  'suppression.sprinklered': { label: 'Sprinklers' },
  'suppression.standpipe': { label: 'Standpipe' },
  'suppression.fdc_location': { label: 'FDC' },
  'structure.construction_type': { label: 'Construction' },
  'structure.stories': { label: 'Storeys' },
  'structure.height_m': { label: 'Height' },
  'structure.year_built': { label: 'Built' },
  'occupancy.type': { label: 'Occupancy' },
  'occupancy.load': { label: 'Occupant load' },
};

/**
 * Which slow-loop agent files a fact from each source, so a line can say so.
 *
 * The brief carries `provenance` -- the *source type* a fact came from, like
 * `PERMIT` or `LIDAR_DSM` -- because that is what the fact records. Which
 * agent went and got it is a property of the fleet, and it is fixed: the
 * README's own division of labour, verified against each agent's source list.
 * Naming it is the point of having a fleet at all. A commander reading
 * "Rooftop solar - referenced from geometry-watcher" knows which of the nine
 * has been working this building for months, and can go and ask it.
 */
const FILED_BY: Readonly<Record<string, string>> = {
  PERMIT: 'records-watcher',
  ASSESSOR: 'records-watcher',
  INSPECTION: 'records-watcher',
  VIOLATION: 'records-watcher',
  PARCEL: 'records-watcher',
  LIDAR_DSM: 'geometry-watcher',
  SOLAR: 'geometry-watcher',
  USGS_3DEP: 'geometry-watcher',
  EPA_FRS: 'hazard-watcher',
  PHMSA: 'hazard-watcher',
  NREL: 'hazard-watcher',
  TIER_II: 'hazard-watcher',
  THERMAL: 'sensor-fusion',
  REPORTED: 'incident-interceptor',
};

/**
 * Which size-up sections land under which heading, and what a crew calls them.
 *
 * Driven off the emission's own sections rather than a whitelist of canonical
 * keys, which is what kept this card nearly empty. The backend emits COAL WAS
 * WEALTH, and a good half of what it puts there is *derived* -- the collapse
 * zone, the lightweight-truss time window, the thermal read per face -- and
 * derived items carry no `canonical_key` at all. Filtering on that key threw
 * every one of them away, so the richest lines the fleet produces were the
 * ones that never reached the screen.
 */
const GROUPS: ReadonlyArray<{ heading: string; note: string; keys: readonly string[] }> = [
  {
    heading: 'Will hurt you',
    note: 'hazards and the clock on them',
    keys: ['HAZARDS', 'TIME'],
  },
  {
    heading: 'People and ways out',
    note: 'who is inside and how they get out',
    keys: ['OCCUPANCY', 'LIFE_HAZARD'],
  },
  {
    heading: 'The building',
    note: 'what it is made of and how big',
    keys: ['CONSTRUCTION', 'HEIGHT', 'AREA'],
  },
  {
    heading: 'Water and access',
    note: 'what you can fight it with',
    keys: ['AUXILIARY_APPLIANCES', 'WATER_SUPPLY', 'STREET_CONDITIONS', 'APPARATUS'],
  },
  {
    heading: 'Measured on scene',
    note: 'what the incident loop read during this call',
    keys: ['LOCATION_EXTENT', 'EXPOSURES', 'WEATHER'],
  },
];

/**
 * A line worth printing, and the words to print it in.
 *
 * Two kinds of item arrive here. A *fact-derived* one is labelled with its
 * canonical key -- `structure.stories` -- which is right for tracing and wrong
 * for reading, so it is translated or dropped. A *derived* one is labelled by
 * the agent that computed it -- "collapse zone", "lightweight truss time
 * window" -- and that label is already the sentence a crew wants, so it is
 * used as it stands. The dot is what tells them apart, and it is reliable:
 * every canonical key in this system is dotted and no human label is.
 */
function readable(item: BriefItemView): { label: string; consequence?: string } | null {
  if (item.canonical_key) {
    return PLAIN[item.canonical_key] ?? null;
  }
  if (item.label.includes('.')) return null;
  return { label: item.label.charAt(0).toUpperCase() + item.label.slice(1) };
}

/** Everything the emission asserts under these section keys. UNKNOWN is not an assertion. */
function itemsUnder(emission: BriefEmissionView, keys: readonly string[]): BriefItemView[] {
  const wanted = new Set(keys);
  return emission.sections
    .filter((section) => wanted.has(section.key))
    .flatMap((section) => section.items)
    .filter((item) => item.status !== 'UNKNOWN' && readable(item) !== null);
}

function unknownCount(emission: BriefEmissionView): number {
  return emission.sections
    .flatMap((section) => section.items)
    .filter((item) => item.status === 'UNKNOWN').length;
}

/**
 * The line across the bottom, when the record earns one.
 *
 * Only a DISPUTED value qualifies. Two official sources contradicting each
 * other about a load-bearing attribute is the one thing on this card a crew
 * cannot settle from the street, and it is the reason the slow loop exists.
 * Absence of data is not a warning; disagreement is.
 */
function contradiction(emission: BriefEmissionView): BriefItemView | null {
  return (
    emission.sections
      .flatMap((section) => section.items)
      .find((item) => item.status === 'DISPUTED') ?? null
  );
}

function Bullet({ item }: { item: BriefItemView }) {
  const plain = readable(item);
  if (!plain) return null;
  const disputed = item.status === 'DISPUTED';
  const filedBy = item.provenance ? FILED_BY[item.provenance] : null;
  return (
    <li className="flex gap-3 py-2">
      <span
        aria-hidden="true"
        className={`mt-2.5 h-2 w-2 shrink-0 rounded-full ${disputed ? 'bg-disputed' : 'bg-live'}`}
      />
      <p className="text-title leading-7">
        <span className="font-semibold text-ink">{plain.label}</span>{' '}
        <span className={disputed ? 'font-semibold text-disputed' : 'text-ink'}>
          {item.value_render}
        </span>
        {plain.consequence && (
          <span className="block text-body leading-6 text-muted">{plain.consequence}</span>
        )}
        {filedBy && (
          <span className="block text-micro leading-5 text-muted">
            referenced from {filedBy}
          </span>
        )}
      </p>
    </li>
  );
}

/** What a waypoint is, in the words a crew would use walking it. */
const STEP: Readonly<Record<string, string>> = {
  staging: 'Stage here',
  approach: 'Approach the',
  door: 'In through the',
  core: 'Interior, level',
  stair: 'Up the stair',
};

/**
 * The computed route, as directions.
 *
 * The path is the one thing on this card the fleet *solved* rather than looked
 * up -- an A* over a graph priced by what the sweep measured and what the
 * records say -- and it was not on the card at all. A crew given hazards and
 * no way in has been told the hard half and not the useful half.
 *
 * Rendered as numbered steps from the waypoints, because that is what a route
 * is to somebody walking it. The cost terms and the per-leg `chose_because`
 * stay on the package for whoever audits the solve; a doorway does not need
 * its multiplier printed beside it.
 */
function RouteSteps({ path }: { path: EntryPathPlanView }) {
  if (path.refused || !path.entry || path.entry.waypoints.length === 0) return null;
  const steps = path.entry.waypoints.map((waypoint) => {
    const verb = STEP[waypoint.kind] ?? waypoint.kind;
    if (waypoint.kind === 'staging') return verb;
    if (waypoint.kind === 'core') {
      return `${verb} ${(waypoint.level ?? 0) + 1}`;
    }
    if (waypoint.kind === 'stair') return verb;
    return `${verb} ${waypoint.face} side`;
  });
  return (
    <section className="border-t border-line px-6 py-4">
      <h4 className="flex flex-wrap items-baseline gap-2">
        <span className="text-label font-semibold uppercase tracking-widest text-ink">
          Way in
        </span>
        <span className="text-micro text-muted">
          solved by incident-interceptor over the measured footprint
        </span>
      </h4>
      <ol className="mt-2 space-y-1.5">
        {steps.map((step, index) => (
          <li key={`${step}:${index}`} className="flex gap-3">
            <span
              aria-hidden="true"
              className="mt-0.5 w-5 shrink-0 text-right font-mono text-body text-muted"
            >
              {index + 1}
            </span>
            <span className="text-title leading-7 text-ink">{step}</span>
          </li>
        ))}
      </ol>
      <p className="mt-2 text-micro leading-5 text-muted">
        {path.entry.total_distance_m.toFixed(0)} m of travel
        {path.entry_face ? ` · entry on ${path.entry_face}` : ''}
        {path.egress ? ' · a second way out was found' : ' · no second way out was found'}
        {path.unscanned_faces.length > 0
          ? ` · ${path.unscanned_faces.join(', ')} not flown, priced as unknown`
          : ''}
      </p>
    </section>
  );
}

/** Until the interceptor's brief lands. It is composing, and says so. */
export function FiregroundBriefPending() {
  return (
    <div
      className="flex flex-col items-center justify-center gap-4 border border-line bg-ground px-6 py-16"
      role="status"
      data-testid="fireground-brief-pending"
    >
      <span aria-hidden="true" className="flex gap-2">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="h-3 w-3 animate-pulse rounded-full bg-live"
            style={{ animationDelay: `${index * 160}ms`, animationDuration: '1.1s' }}
          />
        ))}
      </span>
      <p className="text-title text-ink">incident-interceptor is composing the brief</p>
      <p className="text-body text-muted">Reading the record for this address.</p>
    </div>
  );
}

export function FiregroundBrief({
  emission,
  addressDisplay,
  agentVersion = null,
  path = null,
}: {
  emission: BriefEmissionView;
  /** The solved entry path, when the package carries one. */
  path?: EntryPathPlanView | null;
  /** What the city called this address, when it could place it. */
  addressDisplay?: string | null;
  /** The pinned interceptor version that composed it, when known. */
  agentVersion?: string | null;
}) {
  const unknowns = unknownCount(emission);
  const disputed = contradiction(emission);
  const groups = GROUPS.map((group) => ({
    heading: group.heading,
    note: group.note,
    items: itemsUnder(emission, group.keys),
  })).filter((group) => group.items.length > 0);

  return (
    <div className="border border-line bg-ground" data-testid="fireground-brief">
      <header className="px-6 pb-5 pt-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-hero font-bold uppercase leading-none tracking-tight text-ink">
            Fire incident brief
          </h3>
          <span
            className="rounded border border-alarm bg-alarm/15 px-2.5 py-1 text-label font-semibold uppercase tracking-widest text-alarm"
            data-testid="brief-alarm-pill"
          >
            Active
          </span>
        </div>
        <p className="mt-2 text-display font-semibold leading-tight text-ink">
          {addressDisplay || emission.incident_id}
        </p>
        {/* The composer, named. An account has an author; an unattributed brief
            is the thing this project exists not to produce. */}
        <p className="mt-1 font-mono text-micro text-muted">
          incident-interceptor{agentVersion ? `@${agentVersion}` : ''} · v{emission.version}
        </p>
      </header>

      {groups.map((group) => (
        <section key={group.heading} className="border-t border-line px-6 py-4">
          <h4 className="flex flex-wrap items-baseline gap-2">
            <span className="text-label font-semibold uppercase tracking-widest text-ink">
              {group.heading}
            </span>
            <span className="text-micro text-muted">{group.note}</span>
          </h4>
          <ul className="mt-1">
            {group.items.map((item) => (
              <Bullet key={item.canonical_key} item={item} />
            ))}
          </ul>
        </section>
      ))}

      {path && <RouteSteps path={path} />}

      {groups.length === 0 && (
        <p className="border-t border-line px-6 py-8 text-center text-title text-unknown">
          The record holds nothing confirmed about this building.
        </p>
      )}

      {/* A disagreement is information, not an alarm.
          It read "SOURCES DISAGREE ON STOREYS - 2" in red block capitals,
          which tells a crew something is wrong without telling them what to do
          about it. Two official records differing about a storey count is a
          normal and useful thing for the slow loop to have found: it means
          nobody has stood in the building since the filings diverged, and the
          action it implies is to check on arrival. So it is phrased as the
          instruction it actually is, in the colour this palette uses for
          contested rather than the one it reserves for faults. */}
      {disputed && (
        <p
          className="m-4 rounded border border-disputed bg-disputed/10 px-4 py-4 text-title leading-7 text-disputed"
          data-testid="brief-critical"
        >
          <span className="font-semibold">
            Confirm {PLAIN[disputed.canonical_key ?? '']?.label?.toLowerCase() ?? disputed.label} on
            arrival
          </span>{' '}
          — the record holds {disputed.value_render}, and two filings differ. Worth an eye before
          committing to a floor.
        </p>
      )}

      {/* The absence, counted rather than listed. */}
      {unknowns > 0 && (
        <p className="border-t border-line px-6 py-3 text-body text-unknown">
          {unknowns} further {unknowns === 1 ? 'attribute has' : 'attributes have'} no record. Treat
          as unknown, never as clear.
        </p>
      )}
    </div>
  );
}
