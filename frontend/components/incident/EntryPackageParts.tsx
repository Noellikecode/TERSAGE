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
      {/* Amber, not red.
          `alarm` is this palette's colour for something that has gone wrong,
          and an incomplete record has not gone wrong -- the sentence below has
          always said so ("a statement about the record, not a permission").
          Red made the strongest visual claim on the screen the one claim the
          copy spends three lines walking back, and it was the first thing a
          commander saw. `disputed` is the token for contested or incomplete,
          which is exactly what this is. The words are unchanged: nothing here
          is softened, the count is still stated, and every failed criterion
          still carries its reason. */}
      <div
        className={`border-l-4 p-3 ${
          assessment.ready ? 'border-confirmed bg-surface' : 'border-disputed bg-surface'
        }`}
        data-testid="readiness-banner"
        data-ready={assessment.ready ? 'true' : 'false'}
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <p
            className={`font-mono text-title uppercase tracking-wide ${
              assessment.ready ? 'text-confirmed' : 'text-disputed'
            }`}
          >
            {assessment.ready ? 'Ready' : 'Not ready'}
          </p>
          <p className="font-mono text-micro tabular-nums text-muted">
            {passed.length}/{assessment.criteria.length} checks pass
          </p>
        </div>
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
            className={`border p-2 ${criterion.passed ? 'border-line bg-surface' : 'border-disputed bg-surface'}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill
                tone={criterion.passed ? 'confirmed' : 'disputed'}
                label={criterion.passed ? 'pass' : 'open'}
              />
              <span className="text-body text-ink">{criterion.title}</span>
              <span className="font-mono text-micro text-muted">{criterion.criterion_id}</span>
            </div>
            <p className={`mt-1 text-micro leading-5 ${criterion.passed ? 'text-muted' : 'text-ink'}`}>
              {criterion.reason}
            </p>
            {/* The ids this criterion cites are the audit trail and they are
                still on the package and still in the log. They are not what a
                commander reads to decide, and a column of
                `snap_e9d43b378c0d…` under every line is what made this card
                unreadable. */}
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
        <p className="text-title font-semibold text-alarm">The solver would not build a route</p>
        <p className="mt-1 text-body leading-6 text-ink">{path.refusal_reason}</p>

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
      {/* The prose leads; its provenance follows it.
          The source line used to sit above the brief, so the first thing on
          the most-read block on the screen was `prose source: deterministic`.
          That line matters -- "a model wrote this" and "the template wrote
          this" are different claims and both are kept -- but it is a footnote
          to the text, and a footnote printed first is a barrier to reading.
          Set at `body` on a measure, because unlike everything around it this
          is genuinely read start to finish rather than scanned. */}
      <div className="border-l-2 border-line bg-surface px-4 py-3">
        {brief.prose
          .split('\n')
          .filter((line) => line.trim().length > 0)
          .map((line, index) => (
            <p key={index} className="mb-2.5 max-w-prose text-body leading-7 text-ink last:mb-0">
              {line}
            </p>
          ))}
        {/* Who wrote the wording, in words.
            This used to read `prose source: deterministic · composition
            refused: UPSTREAM_TIMEOUT · projects/.../gemini-3.5-flash`, which
            is three machine facts in a row and answers a question nobody at a
            fire is asking. The distinction it protects is real and stays --
            a model wrote this, or the template did -- but a reader should not
            have to know what UPSTREAM_TIMEOUT means to learn it. */}
        <p className="mt-3 border-t border-line pt-2 text-micro leading-5 text-muted">
          {brief.prose_source === 'model'
            ? 'Wording composed by the model from the facts above.'
            : 'Wording assembled from the record. The model was not used for it.'}
        </p>
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
                  {/* Ids only when there are ids. "no reference" was a line of
                      text saying nothing, printed under a claim that already
                      said everything it had. */}
                  {claim.refs.length > 0 && (
                    <p className="mt-1 font-mono text-micro text-muted">
                      {claim.refs.join(', ')}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
