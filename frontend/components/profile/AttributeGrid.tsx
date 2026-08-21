/**
 * The provenance-rich attribute grid.
 *
 * Three states, and they are never distinguished by colour alone: each row
 * carries a glyph, a word, and a colour. On a washed-out tablet in daylight the
 * word is what survives.
 *
 * | State | Means |
 * |---|---|
 * | CONFIRMED | filed or measured, and the sources agree |
 * | DISPUTED | two sources disagree; both facts are stored |
 * | UNKNOWN | nothing settled it -- never "none present" |
 *
 * Every row shows where the value came from and when it was observed, because
 * a value an officer cannot trace is a claim.
 */

import { StatusPill } from '@/components/StatusPill';
import type { AssertionStatus, FactView } from '@/lib/api/types';

const TONE: Record<AssertionStatus, 'confirmed' | 'disputed' | 'unknown'> = {
  CONFIRMED: 'confirmed',
  DISPUTED: 'disputed',
  UNKNOWN: 'unknown',
};

function age(observedAt: string): string {
  const then = Date.parse(observedAt);
  if (Number.isNaN(then)) return observedAt;
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days < 1) return 'today';
  if (days < 60) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

export function AttributeGrid({
  facts,
  unknownKeys = [],
}: {
  facts: FactView[];
  /** Attributes the profile has, but which nothing settled. */
  unknownKeys?: string[];
}) {
  if (facts.length === 0) {
    return (
      <div className="border border-dashed border-line p-6 text-muted">
        <p className="text-ink">No structural attributes on record</p>
        <p className="mt-1 max-w-prose text-micro leading-5">
          Nothing has been filed or measured for this structure. That is an
          absence of records, not a finding that the building is unremarkable.
        </p>
      </div>
    );
  }

  return (
    <table className="w-full border-collapse text-left">
      <caption className="sr-only">
        Structural attributes, with how each one is known and where it came from
      </caption>
      <thead>
        <tr className="border-b border-line text-micro uppercase tracking-widest text-muted">
          <th scope="col" className="py-1 pr-2 font-normal">
            Attribute
          </th>
          <th scope="col" className="py-1 pr-2 font-normal">
            Value
          </th>
          <th scope="col" className="py-1 pr-2 font-normal">
            How known
          </th>
          <th scope="col" className="py-1 font-normal">
            Provenance
          </th>
        </tr>
      </thead>
      <tbody className="align-top">
        {facts.map((fact) => (
          <tr key={fact.canonical_key} className="border-b border-line/60">
            <th scope="row" className="py-2 pr-2 font-mono text-micro font-normal text-muted">
              {fact.canonical_key}
            </th>
            <td className="py-2 pr-2 font-mono text-ink">{fact.value}</td>
            <td className="py-2 pr-2">
              <StatusPill tone={TONE[fact.status]} label={fact.status.toLowerCase()} />
              {fact.human_verified && (
                <span className="ml-1 text-micro text-confirmed">· surveyed</span>
              )}
            </td>
            <td className="py-2 text-micro text-muted">
              <span className="text-ink">{fact.source_type}</span>
              <span className="mx-1">·</span>
              <span title={fact.observed_at}>{age(fact.observed_at)}</span>
              {fact.decayed_confidence !== null && (
                <>
                  <span className="mx-1">·</span>
                  <span
                    title="Confidence after decay for age, source authority, and intervening filings"
                    className={fact.decayed_confidence < 0.4 ? 'text-disputed' : undefined}
                  >
                    {(fact.decayed_confidence * 100).toFixed(0)}% confidence
                  </span>
                </>
              )}
              {fact.all_fact_ids.length > 1 && (
                <>
                  <span className="mx-1">·</span>
                  <span>{fact.all_fact_ids.length} facts on record</span>
                </>
              )}
            </td>
          </tr>
        ))}
        {unknownKeys
          .filter((key) => !facts.some((fact) => fact.canonical_key === key))
          .map((key) => (
            <tr key={key} className="border-b border-line/60">
              <th scope="row" className="py-2 pr-2 font-mono text-micro font-normal text-muted">
                {key}
              </th>
              <td className="py-2 pr-2 font-mono text-unknown">UNKNOWN</td>
              <td className="py-2 pr-2">
                <StatusPill tone="unknown" label="unknown" />
              </td>
              <td className="py-2 text-micro text-muted">no record found</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}
