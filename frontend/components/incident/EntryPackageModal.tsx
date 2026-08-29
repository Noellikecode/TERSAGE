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
import {
  CrewBriefBody,
  EntryPathSummary,
  ReadinessVerdict,
  type LegSelection,
} from '@/components/incident/EntryPackageParts';
import { StatusPill } from '@/components/StatusPill';
import {
  approveCrewBrief,
  approveEntryPath,
  dispatchEntryPackage,
  downloadEntryPackagePdf,
} from '@/lib/api/entry-packages';
import type { EntryPackageView } from '@/lib/api/types';

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
  /** Which leg the model should be drawing brighter than the rest. */
  onSelectLeg?: (selection: LegSelection | null) => void;
}

export function EntryPackageModal({
  incidentId,
  entryPackage,
  autonomyTrigger = '',
  onUpdated,
  onClose,
  onDispatched,
  onSelectLeg,
}: EntryPackageModalProps) {
  const dialog = useRef<HTMLDivElement | null>(null);
  /** Where focus was before this took it, so it goes back there on close. */
  const returnFocus = useRef<Element | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selection, setSelection] = useState<LegSelection | null>(null);

  const outstanding = entryPackage.outstanding_halves;
  const sent = entryPackage.status === 'SENT';
  const identity = identityFor(COMPOSING_AGENT);

  const select = useCallback(
    (next: LegSelection | null) => {
      setSelection(next);
      onSelectLeg?.(next);
    },
    [onSelectLeg],
  );

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
          {/* Kicker, headline, lede -- in that order, and sized so they read
              as three different things. The agent's name is *attribution*: it
              belongs above the headline in the small voice a byline uses, not
              beside it competing at the same size. What the dialog is asking
              is the headline, because that is the sentence a commander has to
              answer. Both stay inside the `h2` so the accessible name still
              carries the agent and the ask together. */}
          <h2 id="entry-package-title">
            <span
              className="flex items-center gap-2 font-mono text-label uppercase tracking-widest"
              style={{ color: identity.color }}
            >
              <span aria-hidden="true">{identity.glyph}</span>
              {COMPOSING_AGENT}
              <span className="text-muted">@{entryPackage.brief.composed_by_version}</span>
            </span>
            <span className="mt-1 block text-display text-ink">
              Asks for approval to send an entry package
            </span>
          </h2>
          <p id="entry-package-lede" className="mt-2 max-w-prose text-body leading-6 text-muted">
            {identity.role}. It composed this package and{' '}
            {TRIGGER_WORDS[autonomyTrigger] ?? 'is holding it for a human decision.'} Approving it
            releases the crew brief <em className="not-italic text-ink">and</em> the entry path to
            live dispatch units. Nothing has been sent.
          </p>
          {/* The readiness pill is deliberately *not* here any more.
              It used to be the first coloured thing on the dialog, in alarm
              red, above the plan -- so the screen opened by announcing a
              problem before it had said what the plan was. The verdict has not
              been softened or moved out of sight: it is stated in full, in its
              own section, under the plan it qualifies. See `ReadinessVerdict`. */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <StatusPill
              tone={sent ? 'confirmed' : outstanding.length === 0 ? 'live' : 'disputed'}
              label={entryPackage.status.toLowerCase().replace(/_/g, ' ')}
            />
            <span className="font-mono text-micro text-muted">
              {entryPackage.address_id} · {entryPackage.package_id} · composed{' '}
              {entryPackage.created_at}
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
        <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">
          <div>
            <SectionHeading
              step={1}
              title="Crew brief"
              note="what the record says about this building"
            />
            <CrewBriefBody brief={entryPackage.brief} />
            <HalfSignature
              testId="approve-crew-brief"
              label="Sign the crew brief"
              question="This is an accurate account of what we know."
              approved={entryPackage.brief_approved}
              approvedBy={entryPackage.brief_approved_by}
              approvedAt={entryPackage.brief_approved_at}
              approvalId={entryPackage.brief_approval_id}
              busy={busy !== null}
              onSign={() => void grant('crew-brief')}
            />
          </div>

          <div>
            <SectionHeading
              step={2}
              title="Entry path"
              note="the route, and what each leg was weighed against"
            />
            <EntryPathSummary
              path={entryPackage.path}
              selection={selection}
              onSelect={select}
            />
            <HalfSignature
              testId="approve-entry-path"
              label="Sign the entry path"
              question="This is a route I would send a crew down."
              approved={entryPackage.path_approved}
              approvedBy={entryPackage.path_approved_by}
              approvedAt={entryPackage.path_approved_at}
              approvalId={entryPackage.path_approval_id}
              busy={busy !== null}
              onSign={() => void grant('entry-path')}
            />
          </div>

          <div>
            <SectionHeading
              step={3}
              title="What the record could not confirm"
              note="six checks, and how each one answered"
            />
            <ReadinessVerdict assessment={entryPackage.assessment} />
          </div>

          <p className="border border-line bg-surface p-3 text-micro leading-5 text-muted">
            {entryPackage.disclaimer}
          </p>
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
          <div className="flex flex-wrap items-center gap-2">
            {!sent && outstanding.length > 0 && (
              <button
                type="button"
                data-testid="approve-both"
                disabled={busy !== null}
                onClick={() => void approveBoth()}
                className="border border-confirmed px-3 py-1.5 text-body uppercase tracking-wide text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                Approve the path and the brief
              </button>
            )}
            <button
              type="button"
              data-testid="entry-package-release"
              disabled={busy !== null || sent || outstanding.length > 0}
              onClick={() => void release()}
              className="border border-live px-3 py-1.5 text-body uppercase tracking-wide text-live disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              {busy === 'dispatch' ? 'Sending…' : 'Release to live dispatch units'}
            </button>
            <button
              type="button"
              data-testid="modal-download-pdf"
              disabled={busy !== null}
              onClick={() => void download()}
              className="border border-line px-3 py-1.5 text-body text-muted hover:text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              Download PDF
            </button>
            <button
              type="button"
              data-testid="entry-package-dismiss"
              onClick={onClose}
              className="ml-auto border border-line px-3 py-1.5 text-body text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
            >
              Not now
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * A numbered section heading, so the dialog reads as a document with an order.
 *
 * The three sections used to be introduced by identical small grey all-caps
 * lines, which made them look like three captions rather than three parts of
 * one thing an officer works through. The number is the whole trick: it says
 * there are three, says which one this is, and says they are meant to be taken
 * in order -- brief, route, caveats -- without a word of instruction.
 */
function SectionHeading({ step, title, note }: { step: number; title: string; note: string }) {
  return (
    <div className="mb-3 flex items-baseline gap-3 border-b border-line pb-2">
      <span
        aria-hidden="true"
        className="font-mono text-label tabular-nums text-muted"
      >
        {String(step).padStart(2, '0')}
      </span>
      <h3 className="text-title text-ink">{title}</h3>
      <span className="text-micro leading-5 text-muted">{note}</span>
    </div>
  );
}

/**
 * One half's signature block: the question it answers, and who answered it.
 *
 * The question is printed on the control rather than in a heading above it,
 * because what is being signed is a sentence and an officer should be reading
 * that sentence at the moment they tap.
 */
function HalfSignature({
  testId,
  label,
  question,
  approved,
  approvedBy,
  approvedAt,
  approvalId,
  busy,
  onSign,
}: {
  testId: string;
  label: string;
  question: string;
  approved: boolean;
  approvedBy: string | null;
  approvedAt: string | null;
  approvalId: string;
  busy: boolean;
  onSign: () => void;
}) {
  return (
    <div
      className={`mt-3 border p-3 ${approved ? 'border-confirmed bg-surface' : 'border-disputed bg-surface'}`}
      data-testid={`${testId}-block`}
    >
      <p className="text-body leading-6 text-ink">“{question}”</p>
      <p className="mt-1 font-mono text-micro text-muted">{approvalId}</p>
      {approved ? (
        <p className="mt-1.5 text-body text-confirmed" data-testid={`${testId}-granted`}>
          Signed by {approvedBy ?? 'an unnamed caller'} at {approvedAt ?? 'an unrecorded time'}.
        </p>
      ) : (
        <button
          type="button"
          data-testid={testId}
          disabled={busy}
          onClick={onSign}
          className="mt-1.5 border border-confirmed px-3 py-1 text-body uppercase tracking-wide text-confirmed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
        >
          {label}
        </button>
      )}
    </div>
  );
}
