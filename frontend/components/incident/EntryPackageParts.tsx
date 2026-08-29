'use client';

/**
 * The three things an entry package is, rendered one at a time.
 *
 * Split out of the modal because each is a different kind of reading and each
 * has to survive on its own: the verdict is checked criterion by criterion, the
 * route is checked leg by leg against what it avoided, and the brief is checked
 * claim by claim against its citations. A single component doing all three
 * would make the *layout* the thing under review.
 *
 * One rule runs through all of them: **nothing here computes a verdict.** The
 * backend sends `ready`, `failed_ids`, `summary`, `status` and
 * `outstanding_halves` on the wire precisely so a console cannot disagree with
 * the document it is showing, and every one of them is read, not derived.
 */

import { StatusPill } from '@/components/StatusPill';
import type {
  BriefClaimView,
  CrewBriefView,
  EntryPathPlanView,
  ReadinessAssessmentView,
  RouteLegView,
  RouteView,
} from '@/lib/api/types';

/** The order the backend prints sections in, mirrored so the page reads alike. */
export const SECTION_ORDER = [
  'READINESS',
  'STRUCTURE',
  'THERMAL',
  'ROUTE',
  'UNKNOWNS',
  'CAVEATS',
] as const;

/**
 * The verdict, and every criterion under it.
 *
 * **NOT READY is the loud state.** It is the whole reason this document exists:
 * a package whose gaps nobody stated is the one thing the system refuses to
 * allow, and a console that softened the verdict into a neutral badge would be
 * doing exactly that. So the banner is alarm-coloured, it says NOT READY in
 * words as well as colour, it names how many criteria failed, and the failed
 * ones sort to the top of the list with their reasons open.
 *
 * It still does not block anything. A commander may knowingly send a package
 * with three criteria outstanding, and the verdict travels with it onto the
 * printed sheet -- what this must never do is let the gap go unsaid.
 */
export function ReadinessVerdict({ assessment }: { assessment: ReadinessAssessmentView }) {
  const failed = assessment.criteria.filter((criterion) => !criterion.passed);
  const passed = assessment.criteria.filter((criterion) => criterion.passed);

  return (
    <div className="space-y-2" data-testid="readiness-verdict">
      <div
        className={`border-l-4 p-3 ${
          assessment.ready ? 'border-confirmed bg-surface' : 'border-alarm bg-surface'
        }`}
        data-testid="readiness-banner"
        data-ready={assessment.ready ? 'true' : 'false'}
      >
        <p
          className={`font-mono text-title uppercase tracking-wide ${
            assessment.ready ? 'text-confirmed' : 'text-alarm'
          }`}
        >
          {assessment.ready ? 'Ready' : 'Not ready'}
        </p>
        <p className="mt-1 text-body leading-6 text-ink">{assessment.summary}</p>
        {!assessment.ready && (
          <p className="mt-1 text-micro leading-5 text-muted">
            {failed.length} of {assessment.criteria.length} criteria did not pass. Readiness is a
            statement about the record, not a permission — this does not block a send, and it is
            printed on the package whichever way the commander goes.
          </p>
        )}
      </div>

      <ul className="space-y-1.5">
        {/* Failures first. They are what an officer is deciding about, and a
            list in evaluation order buries them among five passes. */}
        {[...failed, ...passed].map((criterion) => (
          <li
            key={criterion.criterion_id}
            data-testid={`criterion-${criterion.criterion_id}`}
            className={`border p-2 ${criterion.passed ? 'border-line bg-surface' : 'border-alarm bg-surface'}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill
                tone={criterion.passed ? 'confirmed' : 'alarm'}
                label={criterion.passed ? 'pass' : 'fail'}
              />
              <span className="text-body text-ink">{criterion.title}</span>
              <span className="font-mono text-micro text-muted">{criterion.criterion_id}</span>
            </div>
            <p className={`mt-1 text-micro leading-5 ${criterion.passed ? 'text-muted' : 'text-ink'}`}>
              {criterion.reason}
            </p>
            {criterion.refs.length > 0 && (
              <p className="mt-1 font-mono text-micro text-muted">
                checked: {criterion.refs.join(', ')}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Whether the city could place this parcel, said in the words of the answer. */
function placementNote(route: RouteView | null): string {
  if (!route || route.waypoints.length === 0) return '';
  const placed = route.waypoints.filter(
    (waypoint) => waypoint.longitude !== null && waypoint.latitude !== null,
  ).length;
  if (placed === route.waypoints.length) {
    return 'Every waypoint carries WGS-84 coordinates, so this route can be put on a map as well as on the model.';
  }
  if (placed === 0) {
    return 'No waypoint carries WGS-84 coordinates: the city could not place this address, so the route exists in footprint-local metres only. It is drawn on the measured model and it cannot be put on a map — nothing here supplies an origin it was not given.';
  }
  return `${placed} of ${route.waypoints.length} waypoints carry WGS-84 coordinates. The rest were not placed, so the route is drawn in footprint-local metres and only part of it could be put on a map.`;
}

/**
 * One leg, with the sentence that says why it was taken.
 *
 * `chose_because` is the point of the whole feature -- a route nobody can
 * interrogate is a route nobody should follow -- so it is on the row rather
 * than behind a disclosure. `avoided` sits beside it because "what did this
 * beat" is the other half of the same question.
 */
function LegRow({
  leg,
  index,
  selected,
  onSelect,
  onHover,
}: {
  leg: RouteLegView;
  index: number;
  selected: boolean;
  onSelect: () => void;
  onHover: (over: boolean) => void;
}) {
  return (
    <li>
      <button
        type="button"
        // A button, not a hover target with a tooltip: the reasoning has to be
        // reachable by keyboard and by a finger on a tablet, and focus does the
        // same job as the cursor on both.
        onClick={onSelect}
        onMouseEnter={() => onHover(true)}
        onMouseLeave={() => onHover(false)}
        onFocus={() => onHover(true)}
        onBlur={() => onHover(false)}
        aria-pressed={selected}
        data-testid={`route-leg-${index}`}
        className={`w-full border p-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-live ${
          selected ? 'border-live bg-raised' : 'border-line bg-surface'
        }`}
      >
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-mono text-micro text-muted">leg {index + 1}</span>
          <span className="font-mono text-micro text-ink">
            {leg.from_id} → {leg.to_id}
          </span>
          <span className="font-mono text-micro text-muted">
            {leg.distance_m.toFixed(1)} m · cost {leg.cost.toFixed(2)} · ×
            {leg.multiplier.toFixed(2)}
          </span>
        </div>
        <p className="mt-1 text-micro leading-5 text-ink">{leg.chose_because}</p>
        {leg.avoided.length > 0 && (
          <p className="mt-1 text-micro leading-5 text-disputed">
            avoided: {leg.avoided.join('; ')}
          </p>
        )}
        {leg.terms.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {leg.terms.map((term) => (
              <li key={term.term_id} className="font-mono text-micro text-muted">
                {term.term_id} +{term.weight} — {term.detail}
              </li>
            ))}
          </ul>
        )}
      </button>
    </li>
  );
}

export interface LegSelection {
  route: 'entry' | 'egress';
  leg: number;
}

/**
 * The path: a route in, a route out, or a stated refusal.
 *
 * A refusal renders **instead of** a route, never beside a partial one. There
 * is no fallback route and no straight line in the backend, so there is nothing
 * here to draw when it refuses -- the reason is the finding, and drawing
 * anything at all would invent the thing the refusal exists to withhold.
 */
export function EntryPathSummary({
  path,
  selection,
  onSelect,
}: {
  path: EntryPathPlanView;
  selection: LegSelection | null;
  onSelect: (selection: LegSelection | null) => void;
}) {
  if (path.refused) {
    return (
      <div className="border-l-4 border-alarm bg-surface p-3" data-testid="path-refused">
        <p className="font-mono text-title uppercase tracking-wide text-alarm">No route</p>
        <p className="mt-1 text-body leading-6 text-ink">{path.refusal_reason}</p>
        {path.refusal_refs.length > 0 && (
          <p className="mt-1 font-mono text-micro text-muted">
            checked: {path.refusal_refs.join(', ')}
          </p>
        )}
        <p className="mt-2 text-micro leading-5 text-muted">
          Nothing is drawn on the model for this package. There is no fallback route and no
          straight line — a refusal withholds the route, and a picture of one would be an
          invention.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="path-summary">
      <div className="border border-line bg-surface p-3">
        <p className="text-body leading-6 text-ink">
          {path.algorithm} with a {path.heuristic} heuristic over {path.node_count} node(s) and{' '}
          {path.edge_count} edge(s).{' '}
          {path.entry
            ? `Enters on ${path.entry_face || 'an unlabelled face'} and reaches storey ${
                path.target_level + 1
              }: ${path.entry.total_distance_m.toFixed(1)} m over ${
                path.entry.legs.length
              } leg(s) at a weighted cost of ${path.entry.total_cost.toFixed(2)}.`
            : 'No entry route is carried on this plan.'}
        </p>
        <p className="mt-1 text-micro leading-5 text-muted">{placementNote(path.entry)}</p>
        {path.unscanned_faces.length > 0 && (
          <p className="mt-1 text-micro leading-5 text-unknown">
            Unscanned when this was computed: {path.unscanned_faces.join(', ')}. UNSCANNED is
            unknown, never clear — an unflown wall is priced above one measured merely warm.
          </p>
        )}
      </div>

      {path.entry && (
        <div>
          <h4 className="text-micro uppercase tracking-widest text-live">
            Entry — {path.entry.legs.length} leg(s), why each one
          </h4>
          <ul className="mt-1.5 space-y-1.5">
            {path.entry.legs.map((leg, index) => (
              <LegRow
                key={`${leg.from_id}-${leg.to_id}-${index}`}
                leg={leg}
                index={index}
                selected={selection?.route === 'entry' && selection.leg === index}
                onSelect={() =>
                  onSelect(
                    selection?.route === 'entry' && selection.leg === index
                      ? null
                      : { route: 'entry', leg: index },
                  )
                }
                onHover={(over) => onSelect(over ? { route: 'entry', leg: index } : null)}
              />
            ))}
          </ul>
        </div>
      )}

      {path.egress ? (
        <div>
          <h4 className="text-micro uppercase tracking-widest text-confirmed">
            Second way out — {path.egress.legs.length} leg(s)
          </h4>
          <ul className="mt-1.5 space-y-1.5">
            {path.egress.legs.map((leg, index) => (
              <LegRow
                key={`${leg.from_id}-${leg.to_id}-${index}`}
                leg={leg}
                index={index}
                selected={selection?.route === 'egress' && selection.leg === index}
                onSelect={() =>
                  onSelect(
                    selection?.route === 'egress' && selection.leg === index
                      ? null
                      : { route: 'egress', leg: index },
                  )
                }
                onHover={(over) => onSelect(over ? { route: 'egress', leg: index } : null)}
              />
            ))}
          </ul>
        </div>
      ) : (
        path.egress_note && (
          <p className="border border-disputed bg-surface p-2 text-micro leading-5 text-disputed">
            {path.egress_note}
          </p>
        )
      )}

      {path.barriers.length > 0 && (
        <div>
          <h4 className="text-micro uppercase tracking-widest text-alarm">
            Legs the cost model refused to build
          </h4>
          <ul className="mt-1.5 space-y-1">
            {path.barriers.map((barrier) => (
              <li
                key={`${barrier.from_id}-${barrier.to_id}`}
                className="border border-alarm bg-surface p-2 text-micro leading-5 text-ink"
              >
                <span className="font-mono text-muted">
                  {barrier.from_id} → {barrier.to_id}
                </span>{' '}
                {barrier.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * The prose a crew reads, and every claim it rests on.
 *
 * Both on one surface, in that order, because the backend prints them that way
 * and for the same reason: prose whose sources are on another page is prose
 * nobody checks. `prose_source` is stated, because "the model wrote this" and
 * "the record rendered this" are different claims and only one of them was
 * screened for numbers that appear in the claims.
 */
export function CrewBriefBody({ brief }: { brief: CrewBriefView }) {
  const bySection = new Map<string, BriefClaimView[]>();
  for (const claim of brief.claims) {
    const held = bySection.get(claim.section);
    if (held) held.push(claim);
    else bySection.set(claim.section, [claim]);
  }

  return (
    <div className="space-y-3" data-testid="crew-brief">
      <p className="font-mono text-micro text-muted">
        prose source: {brief.prose_source}
        {brief.prose_rejection && ` · composition refused: ${brief.prose_rejection}`}
        {brief.model_ref && ` · ${brief.model_ref}`}
      </p>
      <div className="border border-line bg-surface p-3">
        {brief.prose
          .split('\n')
          .filter((line) => line.trim().length > 0)
          .map((line, index) => (
            <p key={index} className="mb-2 text-body leading-6 text-ink last:mb-0">
              {line}
            </p>
          ))}
      </div>

      {brief.unknowns.length > 0 && (
        <p className="border border-unknown bg-surface p-2 text-micro leading-5 text-unknown">
          Carried as unknown rather than elided: {brief.unknowns.join(', ')}.
        </p>
      )}

      <div>
        <h4 className="text-micro uppercase tracking-widest text-muted">
          Every claim, and what it rests on
        </h4>
        {SECTION_ORDER.filter((section) => bySection.has(section)).map((section) => (
          <div key={section} className="mt-2">
            <p className="font-mono text-micro uppercase tracking-widest text-muted">{section}</p>
            <ul className="mt-1 space-y-1">
              {(bySection.get(section) ?? []).map((claim) => (
                <li key={claim.claim_id} className="border border-line bg-surface p-2">
                  <p className="text-micro leading-5 text-ink">{claim.text}</p>
                  <p className="mt-1 font-mono text-micro text-muted">
                    {claim.refs.length > 0 ? claim.refs.join(', ') : 'no reference'}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
