'use client';

/**
 * The brief, filling in as the fleet works.
 *
 * It used to render one emission -- the latest -- and replace the whole
 * document each time a version landed. Every line therefore appeared to arrive
 * at the same instant, twice: once at v1 and once again at v2, which read as
 * two slabs rather than as a brief being written. That was a rendering choice,
 * not what was happening underneath.
 *
 * What is actually happening: v1 is a read of stored state, so its lines
 * genuinely are all known at once -- the slow loop established them over
 * months, and pretending they trickle in would be theatre. Everything after v1
 * is an *amendment*, and those are produced during the incident by agents doing
 * work: Sensor Fusion registering a face as the drone sweep flies it, the
 * interceptor reconciling what the caller said, an IC settling a conflict on
 * the 360. Those arrive one at a time, seconds apart.
 *
 * So the panel accumulates rather than replaces. Every line is keyed to the
 * version it **first appeared in**, lines from the newest version are marked as
 * just-arrived, and a line that was already there does not re-announce itself.
 * The result is a brief that visibly grows as the fleet reports, without a
 * single frame of invented progress.
 *
 * Prose is the one part that is genuinely written token by token, and it now
 * renders that way -- see `useNarrativeStream`. While it is being composed the
 * panel shows it arriving with a cursor and says it is provisional; the
 * persisted emission replaces it when the record has it.
 *
 * Stage changes and amendments are announced to screen readers through a polite
 * live region: an officer who cannot see the version tick still hears that the
 * brief changed.
 */

import { StatusPill } from '@/components/StatusPill';
import type { AssertionStatus, BriefEmissionView, BriefItemView } from '@/lib/api/types';

const TONE: Record<AssertionStatus, 'confirmed' | 'disputed' | 'unknown'> = {
  CONFIRMED: 'confirmed',
  DISPUTED: 'disputed',
  UNKNOWN: 'unknown',
};

const STAGE_LABEL: Record<string, string> = {
  INSTANT: 'instant · no model',
  ENRICHED: 'enriched',
  AMENDMENT: 'amendment',
};

/** Where a line came from, in the words an officer would use. */
const ORIGIN_NOTE: Record<string, string> = {
  INSTANT: 'from the record',
  ENRICHED: 'composed',
  AMENDMENT: 'reported during this incident',
};

/**
 * A brief line, plus when it first appeared and what brought it.
 *
 * The identity is `section + label + value` rather than the label alone. A
 * label whose *value* changed is a new reading -- a face that was UNSCANNED and
 * is now 166 C is the drone sweep having flown it -- and treating it as the
 * same line would let exactly the change a commander is waiting for arrive
 * silently.
 */
export interface TrackedItem {
  item: BriefItemView;
  /** The emission version this line first appeared in. */
  firstSeen: number;
  /** The stage of that emission. */
  stage: string;
}

export function itemKey(sectionKey: string, item: BriefItemView): string {
  return `${sectionKey}::${item.label}::${item.value_render}`;
}

/**
 * Fold every version into one document, remembering when each line arrived.
 *
 * Walks versions in order and records the first emission each distinct line was
 * seen in. Order within a section follows the latest emission, so the brief
 * reads in the size-up sequence the reconciler chose rather than in arrival
 * order -- a commander reads COAL WAS WEALTH, not a changelog.
 */
export function trackItems(emissions: BriefEmissionView[]): Map<string, TrackedItem> {
  const seen = new Map<string, TrackedItem>();
  for (const emission of [...emissions].sort((a, b) => a.version - b.version)) {
    for (const section of emission.sections) {
      for (const item of section.items) {
        const key = itemKey(section.key, item);
        if (seen.has(key)) continue;
        seen.set(key, { item, firstSeen: emission.version, stage: emission.stage });
      }
    }
  }
  return seen;
}

/** What a screen reader hears when the brief advances. */
export function announcementFor(emission: BriefEmissionView): string {
  if (emission.stage === 'INSTANT') {
    return `Instant brief ready, version ${emission.version}. Structural facts only, no model was invoked.`;
  }
  if (emission.stage === 'ENRICHED') {
    return emission.narrative_available
      ? `Enriched brief, version ${emission.version}. Narrative added.`
      : `Enriched brief, version ${emission.version}. The narrative is unavailable; the facts are unchanged.`;
  }
  return `Amendment, version ${emission.version}. New information has been added to the brief.`;
}

export function BriefPanel({
  emission,
  emissions = [],
  draftNarrative = '',
  writing = false,
}: {
  emission: BriefEmissionView | null;
  /** Every version so far, so a line can be dated to the one that brought it.
      Defaults to the latest alone, which degrades to the old behaviour. */
  emissions?: BriefEmissionView[];
  /** Prose arriving token by token. Provisional: never stored, never merged. */
  draftNarrative?: string;
  writing?: boolean;
}) {
  if (!emission) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        No brief yet. It appears the moment an incident is opened.
      </p>
    );
  }

  // Fold every version, so each line knows the pass that produced it. Falls
  // back to the latest emission alone when no history was handed in.
  const tracked_ = trackItems(emissions.length > 0 ? emissions : [emission]);

  return (
    <article aria-labelledby="brief-heading" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="brief-heading" className="text-micro uppercase tracking-widest text-muted">
          Brief
        </h2>
        <StatusPill
          tone={emission.stage === 'INSTANT' ? 'confirmed' : 'live'}
          label={`v${emission.version} ${STAGE_LABEL[emission.stage] ?? emission.stage}`}
        />
        {emission.persisted_at && (
          <span
            className="text-micro text-muted"
            title="Written to the incident log before it was transmitted"
          >
            logged {emission.content_hash.slice(0, 8)}
          </span>
        )}
      </div>

      {emission.sections.map((section) => (
        <section key={section.key} className="border border-line bg-raised p-3">
          <h3 className="text-micro uppercase tracking-widest text-muted">
            {section.key.replace(/_/g, ' ').toLowerCase()}
          </h3>
          <dl className="mt-2 space-y-1.5">
            {section.items.map((item, index) => {
              const tracked = tracked_.get(itemKey(section.key, item));
              // A line is new if this version is the one that first carried it,
              // and there was a version before it. Everything in v1 arrives
              // together because it was read from stored state together; only
              // an amendment is something that just happened.
              const justArrived =
                tracked !== undefined &&
                tracked.firstSeen === emission.version &&
                emission.version > 1;
              return (
              // Position as well as label. A section may carry the same label
              // more than once -- LOCATION_EXTENT reports a thermal delta per
              // face and repeats "thermal delta ALPHA" -- and keying on the
              // label alone gave React duplicate keys, which it warns may leave
              // a child duplicated or omitted. A reading silently missing from
              // a brief is the failure this project refuses everywhere else.
              <div
                key={`${section.key}-${index}-${item.label}`}
                data-arrived={justArrived ? 'true' : undefined}
                className={`flex flex-wrap items-baseline gap-2 ${
                  justArrived ? 'brief-line-arrived border-l-2 border-live pl-2' : ''
                }`}
              >
                <dt className="font-mono text-micro text-muted">{item.label}</dt>
                <dd className="flex flex-wrap items-center gap-2">
                  <span
                    className={`font-mono ${
                      item.status === 'UNKNOWN' ? 'text-unknown' : 'text-ink'
                    }`}
                  >
                    {item.value_render}
                  </span>
                  <StatusPill tone={TONE[item.status]} label={item.status.toLowerCase()} />
                  {item.provenance && (
                    <span className="text-micro text-muted">{item.provenance}</span>
                  )}
                </dd>
                {item.derivation_note && (
                  <p className="w-full text-micro text-muted">{item.derivation_note}</p>
                )}
                {item.reported_note && (
                  // Never colour alone: the glyph and the word REPORTED carry
                  // it as well. A caller under stress at 03:00 is not a filed
                  // record, and a line that reads like one is the failure this
                  // whole distinction exists to prevent.
                  <p className="w-full text-micro text-disputed">
                    <span aria-hidden="true">▲ </span>
                    <span className="uppercase tracking-wide">Reported</span>{' '}
                    {item.reported_note}
                  </p>
                )}
                {item.withheld_note && (
                  <p className="w-full text-micro text-disputed">{item.withheld_note}</p>
                )}
                {justArrived && (
                  // Which pass brought it, so a commander can tell a line the
                  // fleet just produced from one that was on file all along.
                  <p className="w-full text-micro text-live">
                    <span aria-hidden="true">▸ </span>
                    New in v{tracked?.firstSeen} —{' '}
                    {ORIGIN_NOTE[tracked?.stage ?? ''] ?? 'reported during this incident'}
                  </p>
                )}
              </div>
              );
            })}
          </dl>
        </section>
      ))}

      {/* The one part of the brief that is *written* rather than read.
          Everything above it is a field with a fact behind it; this is prose a
          model composed, arriving a token at a time. So it is the one block
          that looks like a terminal -- rounded hard, translucent over whatever
          is behind it, and set in mono. The glass is not decoration: it is the
          signal that this pane is a different kind of claim from the columns
          above, and the caret at the end of it is a live one. */}
      <section className="rounded-3xl border border-line/60 bg-raised/40 px-5 py-4 shadow-lg backdrop-blur-md">
        <h3 className="text-micro uppercase tracking-widest text-muted">Narrative</h3>
        {/* Set like the reasoning terminals under each agent, and for the same
            reason they are: this is a machine writing, live, and the reader
            should be able to tell that at a glance. It was rendered as a plain
            paragraph, which made model-composed prose look like the fixed
            fields above it -- the one distinction on this panel that has to
            survive. Mono on a dark ground, scrolled rather than grown, with
            the caret at the end of what has arrived so far. */}
        {emission.narrative_available && emission.narrative ? (
          // The persisted prose. Once this exists it wins: it is what the
          // record holds, and the draft above it was only ever a preview.
          <p className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-lg border border-line bg-ground/70 p-3 font-mono text-micro leading-5 text-ink">
            {emission.narrative}
          </p>
        ) : draftNarrative ? (
          <>
            <p
              className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-lg border border-line bg-ground/70 p-3 font-mono text-micro leading-5 text-ink"
              data-testid="brief-narrative-draft"
            >
              {draftNarrative}
              {writing && (
                <span aria-hidden="true" className="brief-caret ml-0.5 text-live">
                  ▍
                </span>
              )}
            </p>
            {/* Provisional, and it has to say so. This prose is not in the
                incident log yet and can still be withdrawn -- a commander must
                not quote from something the record may never contain. */}
            <p className="mt-2 text-micro leading-5 text-disputed">
              {writing
                ? 'Composing — provisional, not yet in the incident log.'
                : 'Provisional. Waiting for the persisted version.'}
            </p>
          </>
        ) : (
          <p className="mt-2 rounded-lg border border-line bg-ground/70 p-3 font-mono text-micro leading-5 text-muted">
            {writing
              ? 'composing…'
              : emission.stage === 'INSTANT'
                ? 'The instant stage contains no model call. Everything above was read from stored state.'
                : 'UNAVAILABLE — the model did not return usable prose. The facts above are unchanged and complete.'}
          </p>
        )}
      </section>

      {(emission.unknowns.length > 0 ||
        emission.unavailable.length > 0 ||
        emission.withheld.length > 0) && (
        <section className="border border-unknown bg-raised p-3">
          <h3 className="text-micro uppercase tracking-widest text-muted">Gaps</h3>
          {emission.unknowns.length > 0 && (
            <p className="mt-1 text-micro leading-5 text-unknown">
              <span className="font-mono">UNKNOWN</span> — no record found:{' '}
              {emission.unknowns.join(', ')}
            </p>
          )}
          {emission.unavailable.length > 0 && (
            <p className="mt-1 text-micro leading-5 text-alarm">
              <span className="font-mono">UNAVAILABLE</span> — source unreachable:{' '}
              {emission.unavailable.join(', ')}
            </p>
          )}
          {emission.withheld.length > 0 && (
            <p className="mt-1 text-micro leading-5 text-disputed">
              <span className="font-mono">WITHHELD</span> — {emission.withheld.join(', ')}
            </p>
          )}
          <p className="mt-2 text-micro leading-5 text-muted">
            These are not findings that nothing is there. They are the questions
            nobody has answered.
          </p>
        </section>
      )}
    </article>
  );
}
