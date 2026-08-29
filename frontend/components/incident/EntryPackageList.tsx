'use client';

/**
 * Every entry package this incident produced, and the sheet each one prints to.
 *
 * A list rather than "the current one", because a package is never overwritten:
 * the log is the store, each state change appends, and a package composed
 * twenty minutes ago that a commander declined is still a thing that happened
 * and still has to be accountable for. So the rows are in composed order and
 * every one of them is downloadable, including the ones nobody approved --
 * a sheet nobody signed says so on its own face, which is the whole reason the
 * PDF endpoint is reachable at any state.
 *
 * The download is a fetch rather than a link. The bytes sit behind a credential
 * the browser never holds, so the request goes through the console's gateway,
 * and a link would render an allowlist 404 as a blank tab with nothing to say.
 * Fetched, checked for `application/pdf`, and only then saved.
 */

import { useState } from 'react';

import { StatusPill } from '@/components/StatusPill';
import { downloadEntryPackagePdf } from '@/lib/api/entry-packages';
import type { EntryPackageView, PackageStatus } from '@/lib/api/types';

const STATUS_TONE: Readonly<Record<PackageStatus, 'confirmed' | 'live' | 'disputed'>> = {
  AWAITING_APPROVAL: 'disputed',
  READY_TO_SEND: 'live',
  SENT: 'confirmed',
};

export function EntryPackageList({
  incidentId,
  packages,
  recoveredFromList = false,
  onReview,
}: {
  incidentId: string;
  packages: EntryPackageView[];
  /** True when a log frame could not be decoded and the list was read instead. */
  recoveredFromList?: boolean;
  onReview: (entryPackage: EntryPackageView) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  return (
    <section aria-labelledby="entry-packages-heading" className="space-y-2">
      <h3
        id="entry-packages-heading"
        className="text-micro uppercase tracking-widest text-muted"
      >
        Entry packages
      </h3>

      {recoveredFromList && (
        <p className="border border-disputed bg-surface p-2 text-micro leading-5 text-disputed">
          A package entry arrived on the incident log that this console could not read, so these
          were re-read from the packages endpoint instead of from the stream. They are correct as
          of that read and they will not update in place until the next one.
        </p>
      )}

      {packages.length === 0 ? (
        <p className="border border-dashed border-line p-3 text-micro leading-5 text-muted">
          No entry package yet. The interceptor composes one when the record is good enough to
          compute a route over, or when the sweep stops and what is on file is what there is going
          to be. Nothing is staged in the meantime.
        </p>
      ) : (
        <ul className="space-y-1.5" data-testid="entry-package-list">
          {packages.map((held) => (
            <li
              key={held.package_id}
              data-testid={`entry-package-row-${held.package_id}`}
              className="border border-line bg-surface p-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill
                  tone={STATUS_TONE[held.status]}
                  label={held.status.toLowerCase().replace(/_/g, ' ')}
                />
                {/* The verdict travels with the row. A list that showed only
                    the approval status would let a NOT READY package look
                    identical to a ready one at a glance, which is the reading
                    the assessment exists to prevent. */}
                <StatusPill
                  tone={held.assessment.ready ? 'confirmed' : 'alarm'}
                  label={held.assessment.ready ? 'ready' : 'not ready'}
                />
                {held.path.refused && <StatusPill tone="alarm" label="no route" />}
                <span className="font-mono text-micro text-ink">{held.package_id}</span>
              </div>
              <p className="mt-1 font-mono text-micro text-muted">
                composed {held.created_at}
                {held.sent_at
                  ? ` · sent ${held.sent_at}`
                  : held.outstanding_halves.length > 0
                    ? ` · outstanding: ${held.outstanding_halves.join(', ')}`
                    : ' · both halves signed, not sent'}
              </p>
              <div className="mt-1.5 flex flex-wrap gap-2">
                <button
                  type="button"
                  data-testid={`review-${held.package_id}`}
                  onClick={() => onReview(held)}
                  className="border border-line px-2 py-0.5 text-micro text-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  {held.status === 'AWAITING_APPROVAL' ? 'Review and approve' : 'Open'}
                </button>
                <button
                  type="button"
                  data-testid={`download-${held.package_id}`}
                  disabled={busyId !== null}
                  onClick={() => {
                    void (async () => {
                      setBusyId(held.package_id);
                      setNotice(null);
                      const result = await downloadEntryPackagePdf(incidentId, held.package_id);
                      setBusyId(null);
                      if (!result.ok) {
                        setNotice(`${held.package_id} did not download: ${result.message}`);
                      }
                    })();
                  }}
                  className="border border-line px-2 py-0.5 text-micro text-muted hover:text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  {busyId === held.package_id ? 'Fetching…' : 'PDF'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {notice && (
        <p role="alert" className="text-micro leading-5 text-alarm">
          {notice}
        </p>
      )}
    </section>
  );
}
