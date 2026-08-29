'use client';

/**
 * The entry package, as the console reaches it.
 *
 * Six endpoints and a diagnostic, and the shape of them says what the document
 * is: a package is composed in one pass, read whole, approved in **two separate
 * taps**, sent once, and printed at any point. The two approvals are not a formality split
 * in half -- one asks "is this a route I would send a crew down" and the other
 * "is this an accurate account of what we know" -- so they are two functions
 * here rather than one taking a parameter. A single call with a `half` argument
 * would read as one decision with a flag on it, and it is not.
 *
 * **The stream is the fast path. It is not the only one.** Every state change
 * appends an `ENTRY_PACKAGE` entry carrying the whole document, so a package
 * composed by the loop normally reaches `useEntryPackages` on the log stream
 * the console is already consuming, with no second request.
 *
 * It is not, however, a path that can be relied on alone. `GET /log/stream` is
 * snapshot-and-close -- the backend reads the log once, yields what it has and
 * ends the response -- so an entry appended *after* the connect reaches the
 * browser only when `EventSource` reconnects with `Last-Event-ID`. That
 * reconnect is a browser default (measured: ~3 s on a spec implementation,
 * 5 s on Firefox) and, worse, it is **fail-permanent**: one answer on that URL
 * that is not a 2xx `text/event-stream` -- the gateway's own 503 envelope when
 * the credential is unavailable, its 404 for anything off the allowlist, a
 * backend 401 on a grant that has not landed yet -- sets `readyState` to
 * CLOSED and the browser never tries again. Nothing else in the console is
 * watching for the package, so that one bad answer used to mean the approval
 * card simply never appeared, silently, for the rest of the incident.
 *
 * So the list endpoint is read on a timer for as long as an incident is open.
 * Not a degradation and not a fallback: a second, independent path to the one
 * moment in this product that must not be missed. The two cannot double-raise
 * -- both fold into the same `package_id`-keyed state, and the poll is
 * forbidden from moving a package backwards -- see `foldPolled` below.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { browserGet, browserPost, gatewayPath } from './client';
import type { IncidentLogEntryFrame } from './stream';
import type {
  ApiResult,
  EntryPackageView,
  PackageListView,
  PackageSummaryView,
} from './types';

/** The log entry type the interceptor appends on every package state change. */
export const ENTRY_PACKAGE_ENTRY_TYPE = 'ENTRY_PACKAGE';

/**
 * What the diagnostics read is allowed to take.
 *
 * It looks like a status ping and is not one. `describe_autonomy` runs the
 * silent readiness assessment to answer "what is still outstanding", and that
 * is a full profile snapshot, its coverage reports and its conflicts read out
 * of Firestore -- the same work the composer does, minus the model call. On a
 * laptop against real Firestore it lands between one and three seconds, and it
 * lands slowest at exactly the moment this panel is on screen, because the
 * panel only appears while the incident loop is busy competing for the same
 * reads.
 *
 * It inherited the 15 s default and was the only diagnostic call that did not
 * name a budget, so at the two-minute mark it aborted and the card that exists
 * to explain a missing package instead reported that the backend was
 * unreachable. 30 s is the assessment's worst observed cost with the loop
 * running, not a target -- nothing waits for it, and a slow answer still beats
 * a confident wrong one.
 */
const DIAGNOSTICS_TIMEOUT_MS = 30_000;

/**
 * How often the packages endpoint is asked whether one exists yet.
 *
 * Three seconds, and the number is taken from the thing it is standing in for
 * rather than picked. A spec-compliant `EventSource` re-opens a closed stream
 * after ~3 s (Chrome, Safari and undici; Firefox 5 s), so on the *good* days
 * the log stream's own effective cadence is already a three-second poll -- this
 * matches it, which means adding it can only ever make the card earlier, never
 * later, and on the bad days it is the only clock left running.
 *
 * The budget is what fixes it there rather than somewhere slower -- and it is
 * now fixed there in both directions, because this interval is a *term* in it.
 * `COMPOSE_DEADLINE` is solved backwards from the two-minute ceiling as
 * `120 - 6 (the composition cap) - 3 (this)`, on the reasoning that a package
 * staged just after a tick waits one whole period to be seen. Lengthening this
 * shortens the deadline by the same amount; it is not a number that can be
 * changed here alone.
 *
 * Three rather than ten because the officer still has to watch a route draw and
 * read six criteria before signing twice. A card ten seconds behind its package
 * would be a visible failure against that clock; three is inside the time the
 * route takes to draw itself.
 *
 * It sits between the console's two other loops -- `FLEET_POLL_MS` at 2500 and
 * `STANDBY_POLL_MS` at 7000 -- and costs less than either: one summary read per
 * tick, and a full document only when the summary says the console is holding
 * something older than what the backend has.
 */
export const ENTRY_PACKAGE_POLL_MS = 3000;

/**
 * Compose a package, for a storey somebody named or the one the call reported.
 *
 * `targetLevel` is left out by default and the field is then omitted from the
 * body entirely, which is not the same as sending `0`. Omitted, the backend
 * routes to the floor the 911 call reported -- it binds
 * `intake.reported_floor_of_origin` to the span that supports it, and a caller
 * saying the third floor is alight should not have a crew routed to the lobby.
 * Sending `0` says "the ground storey, whatever the call said", which is a
 * decision, and only a commander gets to make it.
 */
export function composeEntryPackage(
  incidentId: string,
  targetLevel?: number,
): Promise<ApiResult<EntryPackageView>> {
  return browserPost<EntryPackageView>(
    `/api/v1/incidents/${incidentId}/entry-packages`,
    targetLevel === undefined ? {} : { target_level: targetLevel },
  );
}

export function listEntryPackages(incidentId: string): Promise<ApiResult<PackageListView>> {
  return browserGet<PackageListView>(`/api/v1/incidents/${incidentId}/entry-packages`);
}

export function readEntryPackage(
  incidentId: string,
  packageId: string,
): Promise<ApiResult<EntryPackageView>> {
  return browserGet<EntryPackageView>(
    `/api/v1/incidents/${incidentId}/entry-packages/${packageId}`,
  );
}

/**
 * What the loop has decided about an incident, when it has decided nothing.
 *
 * The read behind `GET /entry-packages/diagnostics`, and the shape is
 * deliberately all switches, counts and ids: it answers "why is there no card"
 * without being a second, weaker copy of the card. `autonomy_enabled` false
 * means only the compose button will ever produce one; `tracked` false means
 * the backend serving this request never opened the incident and holds no
 * deadline for it; `failures` above zero means a composition was attempted and
 * died, and `failed_error_type` says how.
 */
export interface AutonomyDiagnosticsView {
  incident_id: string;
  autonomy_enabled: boolean;
  tracked: boolean;
  opened_at: string | null;
  age_s: number | null;
  deadline_armed: boolean;
  deadline_at: string | null;
  deadline_in_s: number | null;
  composing: boolean;
  attempts: number;
  failures: number;
  composed_package_id: string;
  composed_trigger: string;
  failed_at: string | null;
  failed_trigger: string;
  failed_error_type: string;
  failed_error_message: string;
  failed_criteria: string[];
  outstanding_criteria: string[];
  assessment_error: string;
  packages: number;
}

/**
 * Ask the backend why there is no card yet.
 *
 * Nothing on the screen calls this on the happy path and it is not a fallback
 * for the poll -- it answers a different question. The poll asks "is there a
 * package"; this asks "is there ever going to be one, and if not, what
 * stopped it". Those had the same answer from outside -- an empty list -- and
 * that is precisely how the same live failure went undiagnosed three times.
 */
export function readAutonomyDiagnostics(
  incidentId: string,
): Promise<ApiResult<AutonomyDiagnosticsView>> {
  return browserGet<AutonomyDiagnosticsView>(
    `/api/v1/incidents/${incidentId}/entry-packages/diagnostics`,
    { timeoutMs: DIAGNOSTICS_TIMEOUT_MS },
  );
}

/**
 * How far a package has got, as a number that only ever goes up.
 *
 * A package moves one way: composed, one half signed, the other half signed,
 * sent. Nothing un-signs a half and nothing un-sends. That is what makes a
 * single integer an honest comparison and it is the whole reason this exists --
 * with two sources feeding one piece of state, "which of these two readings is
 * the later one" has to be answerable without a clock, because the poll's read
 * and the stream's frame carry no ordering between them.
 *
 * Deliberately not `created_at` or a timestamp: both sources describe the same
 * composition and would carry the same one. Deliberately not `status` either --
 * `AWAITING_APPROVAL` covers both "nothing signed" and "one half signed", so it
 * cannot tell the officer's first tap from the state before it.
 *
 * The send is weighted above both halves so a sent package outranks any
 * combination beneath it even if a stale read disagrees about the halves.
 */
function progress(sent: boolean, outstandingHalves: number): number {
  return (sent ? 4 : 0) + (2 - outstandingHalves);
}

export function packageProgress(held: EntryPackageView): number {
  return progress(held.sent_at !== null, held.outstanding_halves.length);
}

/** The same reading off a list row, which carries counts and no document. */
export function summaryProgress(row: PackageSummaryView): number {
  return progress(row.sent_at !== null, row.outstanding.length);
}

/**
 * Fold in a package the *poll* read, and only if it moves that package forward.
 *
 * The guard is the whole point. The officer taps "approve entry path", the
 * modal folds the write's answer in immediately, and a poll already in flight
 * comes back a moment later holding the state from before the tap. Folding that
 * unconditionally would un-tick a half an officer just signed, under their
 * hand, and re-enable a release button they had already earned -- so a reading
 * that is not strictly ahead of what is held is discarded.
 *
 * Returns `current` by identity when there is nothing to do, so a tick that
 * learned nothing does not re-render the console or restart a route draw.
 */
export function foldPolled(
  current: readonly EntryPackageView[],
  incoming: EntryPackageView,
): EntryPackageView[] {
  const held = current.find((one) => one.package_id === incoming.package_id);
  if (held && packageProgress(incoming) <= packageProgress(held)) {
    return current as EntryPackageView[];
  }
  return foldPackages(current, incoming);
}

/**
 * "This is a route I would send a crew down."
 *
 * Spelled out rather than parameterised so the path segment is a literal at the
 * call site: the gateway allowlist admits exactly these two halves and nothing
 * else, and a template variable there would have widened it to any id.
 */
export function approveEntryPath(
  incidentId: string,
  packageId: string,
): Promise<ApiResult<EntryPackageView>> {
  return browserPost<EntryPackageView>(
    `/api/v1/incidents/${incidentId}/entry-packages/${packageId}/approvals/entry-path`,
  );
}

/** "This is an accurate account of what we know." The other judgement. */
export function approveCrewBrief(
  incidentId: string,
  packageId: string,
): Promise<ApiResult<EntryPackageView>> {
  return browserPost<EntryPackageView>(
    `/api/v1/incidents/${incidentId}/entry-packages/${packageId}/approvals/crew-brief`,
  );
}

/** Hand it to the crew. The backend refuses with 422 unless both are granted. */
export function dispatchEntryPackage(
  incidentId: string,
  packageId: string,
): Promise<ApiResult<EntryPackageView>> {
  return browserPost<EntryPackageView>(
    `/api/v1/incidents/${incidentId}/entry-packages/${packageId}/dispatch`,
  );
}

export function entryPackagePdfPath(incidentId: string, packageId: string): string {
  return gatewayPath(`/api/v1/incidents/${incidentId}/entry-packages/${packageId}/pdf`);
}

export interface PdfDownload {
  ok: boolean;
  /** Stated on failure. Never empty when `ok` is false. */
  message: string;
}

/**
 * Fetch the printed sheet and hand it to the browser.
 *
 * Not an `<a href>` straight at the gateway: the response is bytes behind a
 * credential the browser never holds, and a link would open the proxy path in a
 * tab with no way to report a 404 from the allowlist as anything but a blank
 * page. Fetched, checked, and only then turned into a download.
 *
 * The content type is verified rather than assumed. The gateway answers every
 * refusal with a JSON error envelope and a 200-shaped body is not the only way
 * this can go wrong -- saving an error envelope under a `.pdf` name would put a
 * file in a records system that nothing can open and nothing explains.
 */
export async function downloadEntryPackagePdf(
  incidentId: string,
  packageId: string,
  options: { fetchImpl?: typeof fetch } = {},
): Promise<PdfDownload> {
  const doFetch = options.fetchImpl ?? fetch;
  try {
    const response = await doFetch(entryPackagePdfPath(incidentId, packageId), {
      headers: { Accept: 'application/pdf' },
      cache: 'no-store',
    });
    if (!response.ok) {
      return { ok: false, message: `the printed brief came back HTTP ${response.status}` };
    }
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.startsWith('application/pdf')) {
      return {
        ok: false,
        message: `the gateway returned ${contentType || 'no content type'}, not a PDF`,
      };
    }
    const blob = await response.blob();
    if (typeof document === 'undefined' || typeof URL.createObjectURL !== 'function') {
      // A runtime that cannot save a file says so rather than reporting success.
      return { ok: false, message: 'this browser cannot save a file' };
    }
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.download = `crew-brief-${packageId}.pdf`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(href);
    return { ok: true, message: '' };
  } catch (caught) {
    return { ok: false, message: caught instanceof Error ? caught.message : 'request failed' };
  }
}

/**
 * The document inside one `ENTRY_PACKAGE` log entry, or `null`.
 *
 * `package_content` writes the whole package under `package` verbatim, so the
 * stream carries everything the detail endpoint would. `null` means the frame
 * did not -- a shape change on the writing side -- and the caller falls back to
 * the list rather than rendering a half-decoded package.
 */
export function packageFromLogEntry(frame: IncidentLogEntryFrame): EntryPackageView | null {
  if (frame.entry_type !== ENTRY_PACKAGE_ENTRY_TYPE) return null;
  const payload = frame.content?.package;
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return null;
  const candidate = payload as Partial<EntryPackageView>;
  // Three fields, because they are the three the console cannot render without
  // and the three a truncated frame would be missing. Not a schema check: the
  // contract is typed, and this is the boundary where the type stops being true.
  if (typeof candidate.package_id !== 'string') return null;
  if (typeof candidate.status !== 'string') return null;
  if (typeof candidate.assessment !== 'object' || candidate.assessment === null) return null;
  return candidate as EntryPackageView;
}

/**
 * Why the loop composed this one without being asked, or `''` if a human did.
 *
 * Flattened onto the entry rather than carried inside the document, and read
 * from there: "all six criteria passed" and "the clock ran out and the fleet
 * staged what it had" are different claims about the same package, and a
 * console that could not tell them apart would render the second with the
 * confidence of the first.
 */
export function autonomyTriggerFromLogEntry(frame: IncidentLogEntryFrame): string {
  const trigger = frame.content?.autonomy_trigger;
  return typeof trigger === 'string' ? trigger : '';
}

/** Latest state of each package, in first-seen order -- the log's own order. */
export function foldPackages(
  current: readonly EntryPackageView[],
  incoming: EntryPackageView,
): EntryPackageView[] {
  const index = current.findIndex((held) => held.package_id === incoming.package_id);
  if (index < 0) return [...current, incoming];
  const next = [...current];
  next[index] = incoming;
  return next;
}

export interface EntryPackages {
  /** Latest state of each package this incident produced, in composed order. */
  packages: EntryPackageView[];
  /** The one still waiting on a human, oldest first. `null` when none is. */
  awaiting: EntryPackageView | null;
  /** Package id to the autonomy trigger its latest frame reported. */
  triggers: Readonly<Record<string, string>>;
  /**
   * True once an `ENTRY_PACKAGE` frame arrived carrying no readable document
   * and the list endpoint had to be read instead. Surfaced, not swallowed:
   * it means the stream contract moved and the console is a fetch behind.
   */
  recoveredFromList: boolean;
  /** Fold in the package a write returned, without waiting for its frame. */
  apply: (updated: EntryPackageView) => void;
  /** Re-read every package from the API. */
  refresh: () => void;
}

/**
 * Packages, from the log stream first and from the endpoint regardless.
 *
 * The stream is already open for the agent cards and every package state change
 * appends to it, so on a healthy connection a composed package, an approved
 * half and a send all arrive here with no second request -- that is the fast
 * path and it stays the fast path.
 *
 * The poll underneath it exists because that connection has one silent way to
 * die and nothing downstream of it can tell. See the module comment: the log
 * stream is snapshot-and-close, so it is really the browser's reconnect that
 * delivers everything after the first frame, and a single non-SSE answer on
 * that URL closes it permanently. When that happened the console went on
 * looking exactly like an incident where the loop had not composed anything
 * yet. It had; nobody was ever going to be asked.
 */
export function useEntryPackages(
  incidentId: string | null,
  entries: readonly IncidentLogEntryFrame[],
): EntryPackages {
  const [packages, setPackages] = useState<EntryPackageView[]>([]);
  const [triggers, setTriggers] = useState<Record<string, string>>({});
  const [recoveredFromList, setRecoveredFromList] = useState(false);
  /** Sequences already folded in, so a stream replay is not re-applied. */
  const seen = useRef<Set<number>>(new Set());
  /** Set when a frame could not be decoded; cleared once the list has answered. */
  const [needsList, setNeedsList] = useState(0);
  /**
   * What is held, readable from inside the poll without re-arming its timer.
   *
   * `packages` cannot be a dependency of the poll effect: every fold changes
   * its identity, so the interval would be torn down and rebuilt on each one
   * and the next tick would be pushed a full period into the future by the
   * console's own progress. A ticking clock must not be restarted by the thing
   * it is timing.
   */
  const held = useRef<EntryPackageView[]>([]);
  useEffect(() => {
    held.current = packages;
  }, [packages]);

  useEffect(() => {
    // A new incident is a new log. Carrying packages across would show the
    // previous fire's route under this fire's address.
    seen.current = new Set();
    held.current = [];
    setPackages([]);
    setTriggers({});
    setRecoveredFromList(false);
    setNeedsList(0);
  }, [incidentId]);

  useEffect(() => {
    let undecodable = false;
    const decoded: EntryPackageView[] = [];
    const decodedTriggers: Record<string, string> = {};
    for (const frame of entries) {
      if (frame.entry_type !== ENTRY_PACKAGE_ENTRY_TYPE) continue;
      if (seen.current.has(frame.sequence)) continue;
      seen.current.add(frame.sequence);
      const parsed = packageFromLogEntry(frame);
      if (!parsed) {
        undecodable = true;
        continue;
      }
      decoded.push(parsed);
      const trigger = autonomyTriggerFromLogEntry(frame);
      // Only the composing frame carries one; the approval frames that follow
      // leave it empty, and overwriting with '' would lose how it was decided.
      if (trigger) decodedTriggers[parsed.package_id] = trigger;
    }
    if (decoded.length > 0) {
      setPackages((current) => decoded.reduce(foldPackages, current));
    }
    if (Object.keys(decodedTriggers).length > 0) {
      setTriggers((current) => ({ ...current, ...decodedTriggers }));
    }
    if (undecodable) setNeedsList((count) => count + 1);
  }, [entries]);

  const apply = useCallback((updated: EntryPackageView) => {
    setPackages((current) => foldPackages(current, updated));
  }, []);

  const refresh = useCallback(() => setNeedsList((count) => count + 1), []);

  // The fallback, and the refresh, are one path: list the ids, then read each
  // in full. The list carries statuses and counts only -- it is deliberately
  // not a claim -- so it cannot itself feed a modal that shows six criteria.
  useEffect(() => {
    if (!incidentId || needsList === 0) return;
    let cancelled = false;
    void (async () => {
      const listed = await listEntryPackages(incidentId);
      if (cancelled || !listed.ok) return;
      const full = await Promise.all(
        listed.data.packages.map((row) => readEntryPackage(incidentId, row.package_id)),
      );
      if (cancelled) return;
      setPackages((current) =>
        full.reduce(
          (carried, result) => (result.ok ? foldPackages(carried, result.data) : carried),
          current,
        ),
      );
      setRecoveredFromList(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [incidentId, needsList]);

  /**
   * Whether the loop this poll exists to catch has finished.
   *
   * A send is the end of it: the officer signed both halves, the crew has the
   * package, and `CommandCenter` closes the incident behind the resolve sheet.
   * There is nothing left for another card to interrupt, so the timer stops
   * rather than asking a backend about a fire that is out.
   */
  const sent = packages.some((one) => one.sent_at !== null);

  /**
   * The guarantee: ask the endpoint, on a timer, for as long as it can matter.
   *
   * Bounded on all three sides the console can be sure of -- it does not start
   * without an open incident, it is torn down when the incident closes or the
   * console unmounts, and it stops for good once a package has been sent.
   *
   * Bounded in what one tick costs, too, which is the part that lets it run at
   * three seconds without being a second load on the backend. The summary is
   * one small read; a full document is fetched only for a row the console does
   * not hold, or a row the summary says has moved past what it holds. In the
   * ordinary case -- a card up, an officer reading it -- every tick after the
   * first is a single list read that changes nothing.
   *
   * Unlike the fleet poll this does **not** skip while the tab is hidden. The
   * fleet panel is a display and there is nobody to display it to; this is the
   * one thing on the screen that is a question addressed to a person, and a
   * console that quietly stopped looking for it while backgrounded would fail
   * in exactly the way this whole poll was added to stop.
   */
  useEffect(() => {
    if (!incidentId || sent) return;
    let cancelled = false;
    let inFlight = false;

    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const listed = await listEntryPackages(incidentId);
        // A refused or unreachable list is not evidence of anything. Nothing is
        // rendered from it and nothing is cleared: a package the API did not
        // return does not exist, and one it failed to mention has not vanished.
        if (cancelled || !listed.ok) return;

        // Only the rows worth a second request. `summaryProgress` and
        // `packageProgress` read the same monotone scale off the two shapes,
        // so "the backend is ahead of us on this one" is the same question in
        // both directions and a stale read asks for nothing.
        // A body without a list is not an empty list.
        //
        // `ok` says the request completed, not that it answered with the shape
        // the type claims: this reads through a gateway and a proxy, and either
        // can return an envelope on a bad day. Indexing into it threw out of
        // the poll task, where an unhandled rejection is the quietest possible
        // way for the one clock still watching for a package to stop ticking.
        const rows = Array.isArray(listed.data?.packages) ? listed.data.packages : [];
        const wanted = rows.filter((row) => {
          const mine = held.current.find((one) => one.package_id === row.package_id);
          return !mine || summaryProgress(row) > packageProgress(mine);
        });
        if (wanted.length === 0) return;

        const full = await Promise.all(
          wanted.map((row) => readEntryPackage(incidentId, row.package_id)),
        );
        if (cancelled) return;
        setPackages((current) =>
          full.reduce(
            // `foldPolled`, never `foldPackages`: this is the source that can
            // arrive holding a state the officer has already moved past.
            (carried, result) => (result.ok ? foldPolled(carried, result.data) : carried),
            current,
          ),
        );
      } finally {
        inFlight = false;
      }
    };

    // One interval from now, not immediately -- the opposite of the fleet
    // poll, and for the opposite reason. The fleet panel is on screen empty
    // from the first frame and has to be filled; there is nothing to find here
    // until the loop composes, which is the better part of a minute after the
    // open, and a read fired into the middle of the open would be a request
    // against the busiest moment of the incident to be told "none yet".
    const timer = setInterval(() => void tick(), ENTRY_PACKAGE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [incidentId, sent]);

  const awaiting = packages.find((one) => one.status === 'AWAITING_APPROVAL') ?? null;

  return { packages, awaiting, triggers, recoveredFromList, apply, refresh };
}
