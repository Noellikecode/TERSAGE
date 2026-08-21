'use client';

/**
 * The streaming brief.
 *
 * Stage one is deterministic and lands first -- always, and visibly. Prose
 * arrives later or not at all, and the panel says which. A commander looking at
 * this screen can tell the difference between "the model has not answered yet"
 * and "the model is not answering", and neither one hides the facts.
 *
 * Stage changes and amendments are announced to screen readers through a polite
 * live region: an officer who cannot see the version tick still hears that the
 * brief changed.
 */

import { StatusPill } from '@/components/StatusPill';
import type { AssertionStatus, BriefEmissionView } from '@/lib/api/types';

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

export function BriefPanel({ emission }: { emission: BriefEmissionView | null }) {
  if (!emission) {
    return (
      <p className="border border-dashed border-line p-4 text-micro text-muted">
        No brief yet. It appears the moment an incident is opened.
      </p>
    );
  }

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
        <section key={`${section.key}-${emission.version}`} className="border border-line bg-surface p-3">
          <h3 className="text-micro uppercase tracking-widest text-muted">
            {section.key.replace(/_/g, ' ').toLowerCase()}
          </h3>
          <dl className="mt-2 space-y-1.5">
            {section.items.map((item) => (
              <div key={`${section.key}-${item.label}`} className="flex flex-wrap items-baseline gap-2">
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
                {item.withheld_note && (
                  <p className="w-full text-micro text-disputed">{item.withheld_note}</p>
                )}
              </div>
            ))}
          </dl>
        </section>
      ))}

      <section className="border border-line bg-surface p-3">
        <h3 className="text-micro uppercase tracking-widest text-muted">Narrative</h3>
        {emission.narrative_available && emission.narrative ? (
          <p className="mt-2 whitespace-pre-wrap text-ink">{emission.narrative}</p>
        ) : (
          <p className="mt-2 text-micro leading-5 text-muted">
            {emission.stage === 'INSTANT'
              ? 'The instant stage contains no model call. Everything above was read from stored state.'
              : 'UNAVAILABLE — the model did not return usable prose. The facts above are unchanged and complete.'}
          </p>
        )}
      </section>

      {(emission.unknowns.length > 0 ||
        emission.unavailable.length > 0 ||
        emission.withheld.length > 0) && (
        <section className="border border-unknown bg-surface p-3">
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
