'use client';

/**
 * An agent asking a human for permission to send a crew somewhere.
 *
 * That sentence is the whole design. This is not a system dialog and it does
 * not read as one: it is titled with the agent that composed the package, in
 * that agent's own hue and glyph -- the same identity the activity stream gives
 * `incident-interceptor` two panels away, reused rather than reinvented, so a
 * commander recognises who is asking before reading a word of it.
 *
 * **One human approval, two records underneath.** The backend keeps the path
 * and the brief as separate approvals because they are separate judgements --
 * "is this a route I would send a crew down" against "is this an accurate
 * account of what we know" -- and this surface keeps both taps available for an
 * officer who wants to check the brief line by line against its citations
 * first. What it *asks* for is one decision covering both, because the thing
 * being decided is one thing: whether this package goes to live dispatch units.
 * So the combined approval is the primary control and the release is gated on
 * both records being granted, which is also the backend's own 422.
 *
 * **Nothing is described as sent until the send returned.** The approvals do
 * not send. The release does, and only the response saying so turns this into
 * "handed to the crew" -- a modal that congratulated itself on a request still
 * in flight would be reporting an outcome it does not have.
 *
 * **A NOT-READY package is marked as one, here and on the printed sheet.** It
 * does not block anything: a commander may knowingly send a package with three
 * criteria outstanding, and that is the correct distribution of authority. What
 * this must never do is let the gap go unstated -- see `ReadinessVerdict`.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { identityFor } from '@/components/incident/AgentActivity';
import { FiregroundBrief, FiregroundBriefPending } from '@/components/incident/FiregroundBrief';
import type { LegSelection } from '@/components/incident/EntryPackageParts';
import { StatusPill } from '@/components/StatusPill';
import {
  approveCrewBrief,
  approveEntryPath,
  dispatchEntryPackage,
  downloadEntryPackagePdf,
} from '@/lib/api/entry-packages';
import type { BriefEmissionView, EntryPackageView } from '@/lib/api/types';

/** The agent that composes packages. Named, not inferred from the log. */
export const COMPOSING_AGENT = 'incident-interceptor';

/** What each autonomy trigger claims, in the words an officer reads. */
const TRIGGER_WORDS: Readonly<Record<string, string>> = {
  ready: 'composed the moment all six criteria passed.',
  'sweep-terminated':
    'composed because the drone sweep stopped: the record is what it is going to be, whatever it says.',
  deadline:
    'composed because the compose deadline ran out. Nothing terminated the sweep — this is what the fleet had, not what it judged sufficient.',
};

/** Everything focusable inside the dialog, for the tab trap. */
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

export interface EntryPackageModalProps {
  incidentId: string;
  entryPackage: EntryPackageView;
  /** How the loop decided to compose this one, or '' when a human asked. */
  autonomyTrigger?: string;
  /** Fold the package a write returned back into the caller's state. */
  onUpdated: (updated: EntryPackageView) => void;
  /** Dismiss without deciding. The package stays awaiting approval. */
  onClose: () => void;
  /** Called only after the send returned ok, with the sent package. */
  onDispatched: (sent: EntryPackageView) => void;
  /**
   * Which leg the model should draw brighter than the rest.
   *
   * Accepted and unused. The route summary this was driven from is no longer
   * on the dialog -- the card is the brief alone -- and the route is still
   * drawn on the model behind it. Kept on the interface because the caller
   * passes it and a leg surface may come back; a dialog that quietly dropped
   * the prop would look like it still worked.
   */
  onSelectLeg?: (selection: LegSelection | null) => void;
  /**
   * The interceptor's latest brief emission, rendered as the size-up card.
   *
   * Optional because the package is the subject of this dialog and the card is
   * a better way of reading one part of it -- a package whose emission has not
   * reached the console yet still shows its brief, its path and its verdict.
   */
  emission?: BriefEmissionView | null;
  /** The street address the city gave, when it could place the id. */
  addressDisplay?: string | null;
}

export function EntryPackageModal({
  incidentId,
  entryPackage,
  autonomyTrigger = '',
  onUpdated,
  onClose,
  onDispatched,
  emission = null,
  addressDisplay = null,
}: EntryPackageModalProps) {
  const dialog = useRef<HTMLDivElement | null>(null);
  /** Where focus was before this took it, so it goes back there on close. */
  const returnFocus = useRef<Element | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const outstanding = entryPackage.outstanding_halves;
  const openChecks = entryPackage.assessment.criteria.filter((c) => !c.passed).length;
  const sent = entryPackage.status === 'SENT';
  const identity = identityFor(COMPOSING_AGENT);

  // Focus in, focus back out. A modal that leaves focus on the page behind it
  // is a modal a keyboard user can tab straight past without ever knowing an
  // agent asked them something.
  useEffect(() => {
    returnFocus.current = document.activeElement;
    const first = dialog.current?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? dialog.current)?.focus();
    return () => {
      const previous = returnFocus.current;
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);

  // Escape is the escape route, and Tab is trapped so the only way out of the
  // dialog is a deliberate one.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab' || !dialog.current) return;
      const focusable = Array.from(dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === dialog.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onClose]);

  /** One half, one POST, and the returned document is what goes on screen. */
  const grant = useCallback(
    async (half: 'entry-path' | 'crew-brief'): Promise<EntryPackageView | null> => {
      setBusy(half);
      setNotice(null);
      const result =
        half === 'entry-path'
          ? await approveEntryPath(incidentId, entryPackage.package_id)
          : await approveCrewBrief(incidentId, entryPackage.package_id);
      setBusy(null);
      if (!result.ok) {
        setNotice(`That approval was not recorded: ${result.error.message}`);
        return null;
      }
      onUpdated(result.data);
      return result.data;
    },
    [incidentId, entryPackage.package_id, onUpdated],
  );

  /**
   * The one decision, as two records.
   *
   * Sequential rather than parallel, and it stops on the first refusal: the two
   * approvals are written to the same log and a second POST fired against a
   * package whose first one was rejected would be signing something the record
   * did not accept the other half of.
   */
  const approveBoth = useCallback(async () => {
    for (const half of outstanding) {
      const granted = await grant(half);
      if (!granted) return;
    }
  }, [outstanding, grant]);

  const release = useCallback(async () => {
    setBusy('dispatch');
    setNotice(null);
    const result = await dispatchEntryPackage(incidentId, entryPackage.package_id);
    setBusy(null);
    if (!result.ok) {
      setNotice(`Nothing was sent: ${result.error.message}`);
      return;
    }
    onUpdated(result.data);
    onDispatched(result.data);
  }, [incidentId, entryPackage.package_id, onUpdated, onDispatched]);

  const download = useCallback(async () => {
    setBusy('pdf');
    const result = await downloadEntryPackagePdf(incidentId, entryPackage.package_id);
    setBusy(null);
    if (!result.ok) setNotice(`The printed brief did not download: ${result.message}`);
  }, [incidentId, entryPackage.package_id]);

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ground/90 p-2 sm:p-6"
      data-testid="entry-package-backdrop"
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="entry-package-title"
        aria-describedby="entry-package-lede"
        tabIndex={-1}
        data-testid="entry-package-modal"
        className="flex max-h-full w-full max-w-4xl flex-col border bg-ground shadow-2xl"
        style={{ borderColor: identity.color }}
      >
        <div
          className="shrink-0 border-b border-line p-4"
          // The agent's hue as a top edge, the way the activity stream uses it
          // as a left edge: identity carried by an edge, never as a fill behind
          // text that then has to be contrast-checked against four hues.
          style={{ borderTopColor: identity.color, borderTopWidth: 3 }}
        >
          {/* One line, because the brief is the masthead now.
              This used to be a display-sized headline over a paragraph of
              lede, and it competed with the card underneath it -- two things
              claiming to be the top of the same dialog. What the dialog is
              *asking* still has to be unmissable and still has to carry the
              agent's name for the accessible title, so it stays; it is simply
              no longer the biggest thing on screen. The brief is. */}
          <h2
            id="entry-package-title"
            className="flex flex-wrap items-center gap-x-2 gap-y-1 text-title text-ink"
          >
            <span aria-hidden="true" style={{ color: identity.color }}>
              {identity.glyph}
            </span>
            <span className="font-mono" style={{ color: identity.color }}>
              {COMPOSING_AGENT}
            </span>
            <span className="font-mono text-micro text-muted">
              @{entryPackage.brief.composed_by_version}
            </span>
            <span>asks for approval to send this to the crew.</span>
          </h2>
          <p id="entry-package-lede" className="mt-1 max-w-prose text-micro leading-5 text-muted">
            {TRIGGER_WORDS[autonomyTrigger] ?? 'It is holding this for a human decision.'} Approving
            releases the brief <em className="not-italic text-ink">and</em> the entry path to live
            dispatch units. Nothing has been sent.
          </p>
          {/* The readiness pill is deliberately *not* here any more.
              It used to be the first coloured thing on the dialog, in alarm
              red, above the plan -- so the screen opened by announcing a
              problem before it had said what the plan was. The verdict has not
              been softened or moved out of sight: it is stated in full, in its
              own section, under the plan it qualifies. See `ReadinessVerdict`. */}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusPill
              tone={sent ? 'confirmed' : outstanding.length === 0 ? 'live' : 'disputed'}
              label={entryPackage.status.toLowerCase().replace(/_/g, ' ')}
            />
            <span className="font-mono text-micro text-muted">
              {entryPackage.package_id}
            </span>
          </div>
        </div>

        {/* Order is the argument this dialog makes.
            It used to open on the readiness verdict, so a commander met "NOT
            READY" in red before reading a single line of the plan -- and the
            verdict is not a blocker, it is a caveat. A caveat printed before
            the thing it qualifies reads as a refusal. So: the brief, then the
            route, then what the record could not confirm about either. Nothing
            is hidden and nothing is reordered inside a section; only the three
            sections are ranked the way they are actually used. */}
        {/* The brief, and nothing else.
            The readiness table, the route legs and the citation list were all
            on this surface and all pushed the brief off the top of it. They
            are not gone: the verdict rides on the package and on the printed
            sheet, the route is drawn on the model behind this dialog, and the
            claims are in the incident log. What a crew reads before going
            through a door is the size-up, so that is what the card is. */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {emission ? (
            <FiregroundBrief
              emission={emission}
              addressDisplay={addressDisplay}
              agentVersion={entryPackage.brief.composed_by_version}
              path={entryPackage.path}
            />
          ) : (
            <FiregroundBriefPending />
          )}
        </div>

        <div className="shrink-0 space-y-2 border-t border-line p-4">
          {notice && (
            <p role="alert" className="text-body leading-6 text-alarm">
              {notice}
            </p>
          )}
          <p className="text-micro leading-5 text-muted" data-testid="outstanding-line">
            {sent
              ? `Sent to live dispatch units by ${entryPackage.sent_by ?? 'an unnamed caller'} at ${entryPackage.sent_at ?? 'an unrecorded time'}, under gateway decision ${entryPackage.dispatch_decision_id || 'none recorded'}.`
              : outstanding.length === 0
                ? 'Both halves are signed. Releasing hands this package to live dispatch units.'
                : `Outstanding: ${outstanding.join(', ')}. One approval covers both — they stay two records because they are two judgements, and the send is refused until both are granted.`}
          </p>
          {/* A refused solve, said outright.
              "No route" is not a quieter version of a route -- it means the
              cost model would not build one, and an officer approving this
              package is approving a document with no path in it. That cannot
              be inferred from the brief above, so it is stated here. */}
          {/* Said as a finding, not as a failure.
              The raw reason is written for whoever is debugging the solver --
              "no leg of the navigable graph connects the start to the goal;
              the cost model refused every route that would have..." -- and it
              is neither readable nor actionable at a door. What a commander
              needs is the fact and what follows from it: there is no computed
              path, so the crew picks the way in. The full reason stays on the
              package and on the printed sheet. */}
          {entryPackage.path.refused && (
            <p className="text-body font-semibold text-disputed" data-testid="path-refused">
              No route computed — the graph could not connect the street to the target floor. Crew
              chooses the way in.
            </p>
          )}

          {/* The verdict, at the point of decision.
              The full six-criterion table is off this dialog -- it is on the
              package and on the printed sheet, and it is not what a crew reads
              at a door. The *verdict* is different: this is the moment a human
              authorises a send, and a commander approving a NOT READY package
              has to be told so here, in one line, without opening anything.
              Leaving it off would have turned "the gaps are always stated"
              into "the gaps are stated somewhere else". */}
          {/* The verdict in words, not in criterion ids.
              It read "Not ready — NOT READY - 3 of 6 criteria pass;
              outstanding: hazard.resolved, conflicts.load-bearing,
              intake.access-bound" -- the verdict twice, then three internal
              identifiers, at the moment somebody is deciding whether to send a
              crew. The count is the part that means anything to a human, and
              the criteria themselves are on the package and the printed sheet
              where an officer can read them at leisure. Nothing is hidden by
              saying it shorter. */}
          <p
            className={`text-body font-semibold ${
              entryPackage.assessment.ready ? 'text-confirmed' : 'text-disputed'
            }`}
            data-testid="readiness-banner"
            data-ready={entryPackage.assessment.ready ? 'true' : 'false'}
          >
            {entryPackage.assessment.ready
              ? 'Every check passed against the record.'
              : `${openChecks} of ${entryPackage.assessment.criteria.length} checks could not be ` +
                'confirmed from the record. Sending is still yours.'}
          </p>

          {/* What has been signed, and by whom. Each half says so on its own
              line: an officer who signed the path and not the brief has to be
              able to see that from the dialog, and "one of two" is not a state
              a single combined message can express honestly. */}
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {entryPackage.path_approved && (
              <p className="text-micro text-confirmed" data-testid="approve-entry-path-granted">
                Entry path signed by {entryPackage.path_approved_by ?? 'an unnamed caller'} at{' '}
                {entryPackage.path_approved_at ?? 'an unrecorded time'}.
              </p>
            )}
            {entryPackage.brief_approved && (
              <p className="text-micro text-confirmed" data-testid="approve-crew-brief-granted">
                Crew brief signed by {entryPackage.brief_approved_by ?? 'an unnamed caller'} at{' '}
                {entryPackage.brief_approved_at ?? 'an unrecorded time'}.
              </p>
            )}
          </div>

          {/* The two judgements, kept as two taps -- in the footer, not on the
              card.
              The card is the brief and only the brief, which is what an
              officer reads. Signing is a different act, and it is still two
              acts: "this is a route I would send a crew down" and "this is an
              accurate account of what we know" are separate questions with
              separate records, and the backend refuses the send until both are
              granted. `Approve brief` covers both in one tap for the ordinary
              case; these stay for the officer who wants to sign one and not
              the other. Removing them would have collapsed a safety control
              into a layout decision. */}
          {!sent && outstanding.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {outstanding.includes('entry-path') && (
                <button
                  type="button"
                  data-testid="approve-entry-path"
                  disabled={busy !== null}
                  onClick={() => void grant('entry-path')}
                  className="rounded border border-confirmed px-3 py-1.5 text-micro text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  Sign the entry path only
                </button>
              )}
              {outstanding.includes('crew-brief') && (
                <button
                  type="button"
                  data-testid="approve-crew-brief"
                  disabled={busy !== null}
                  onClick={() => void grant('crew-brief')}
                  className="rounded border border-confirmed px-3 py-1.5 text-micro text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  Sign the crew brief only
                </button>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {!sent && outstanding.length > 0 && (
              <button
                type="button"
                data-testid="approve-both"
                disabled={busy !== null}
                onClick={() => void approveBoth()}
                className="rounded bg-alarm px-5 py-2.5 text-body font-semibold text-ground disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                Approve brief
              </button>
            )}
            <button
              type="button"
              data-testid="entry-package-release"
              disabled={busy !== null || sent || outstanding.length > 0}
              onClick={() => void release()}
              className="rounded border border-live px-5 py-2.5 text-body font-semibold text-live disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              {busy === 'dispatch' ? 'Sending…' : 'Release to dispatch'}
            </button>
            <button
              type="button"
              data-testid="modal-download-pdf"
              disabled={busy !== null}
              onClick={() => void download()}
              className="rounded border border-line px-4 py-2.5 text-body text-muted hover:text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              Download PDF
            </button>
            <button
              type="button"
              data-testid="entry-package-dismiss"
              onClick={onClose}
              className="ml-auto rounded border border-line px-4 py-2.5 text-body text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              Not now
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
