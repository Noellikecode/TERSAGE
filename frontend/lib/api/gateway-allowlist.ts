/**
 * What the console is allowed to ask the backend for.
 *
 * The gateway route attaches a privileged credential to whatever it forwards,
 * and the console Cloud Run service is `allow_unauthenticated = true`. Without
 * this file the proxy is an open relay: anyone who can load the console can
 * drive any backend endpoint with console authority.
 *
 * So the rule is inverted. The proxy forwards **only** the paths the console
 * itself calls, enumerated here by hand and derived from the real call sites in
 * `components/CommandCenter.tsx`, `lib/api/stream.ts`, `components/BackendStatus.tsx`
 * and `lib/api/client.ts`. Everything else is a 404 -- not a 403, because a 403
 * confirms that something is there.
 *
 * Two properties this file is responsible for:
 *
 * - **Method-scoped.** A path allowed for GET is not thereby allowed for POST.
 *   `/api/v1/incidents/{id}/log` is readable; nothing writes to it.
 * - **`/api/v1/internal/` is denied by default**, checked as a final gate after
 *   the allowlist rather than as an absence from it. The internal namespace
 *   contains the fleet's control plane -- Pub/Sub push, signed callbacks, the
 *   scheduler tick -- and none of it is reachable here at any time, by any
 *   method. See `INTERNAL_READ_ONLY` for the three read-only audit views that
 *   are the sole, explicitly enumerated exception.
 */

export type GatewayMethod = 'GET' | 'POST';

/**
 * Longest legitimate path is `api/v1/internal/audit/incidents/{id}/replay` at
 * seven. Eight leaves one segment of slack and still bounds the work done on a
 * hostile request.
 */
export const MAX_PATH_SEGMENTS = 8;

/** Everything under here is denied unless `INTERNAL_READ_ONLY` says otherwise. */
export const INTERNAL_PREFIX = '/api/v1/internal/';

/**
 * The only paths under `/api/v1/internal/` the proxy will ever reach, and only
 * by GET.
 *
 * These are read-only audit views the console genuinely renders: the activity
 * stream, the gateway policy decisions, and the incident replay. They carry no
 * side effects and the console's own role holds `read:audit`. Emptying this set
 * seals the internal namespace completely, at the cost of the audit console and
 * the replay panel rendering empty.
 *
 * The internal endpoints that *do* have side effects -- `/internal/events/push`,
 * `/internal/callbacks/write`, `/internal/scheduler/tick` -- are deliberately
 * absent, as are `/internal/metrics` and `/internal/events/dead-letters`.
 */
export const INTERNAL_READ_ONLY: ReadonlySet<string> = new Set([
  '/api/v1/internal/audit/events',
  '/api/v1/internal/audit/decisions',
]);

/** The one id-bearing member of `INTERNAL_READ_ONLY`. */
const INTERNAL_REPLAY = /^\/api\/v1\/internal\/audit\/incidents\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/replay$/;

/**
 * An id segment: an opaque identifier, not a path.
 *
 * Anchored, bounded, and free of `/`, `\`, `:`, `%` and `.` runs by
 * construction, so a matched id cannot smuggle a traversal or an absolute URL
 * through the join below.
 */
const ID = '[A-Za-z0-9][A-Za-z0-9._-]{0,127}';

/** Exact paths, by method. No pattern matching, no surprises. */
const EXACT: Readonly<Record<GatewayMethod, ReadonlySet<string>>> = {
  GET: new Set([
    // Backend liveness/readiness. Unauthenticated on the backend and already
    // rendered into the public page; polled from the browser by BackendStatus.
    '/healthz',
    '/readyz',
    '/api/v1/system/status',
    // Agent registry: discovery and this department's subscriptions.
    '/api/v1/registry/agents',
    '/api/v1/registry/subscriptions',
    // Read-only audit views -- see INTERNAL_READ_ONLY.
    '/api/v1/internal/audit/events',
    '/api/v1/internal/audit/decisions',
  ]),
  POST: new Set([
    // Open an incident.
    '/api/v1/incidents',
  ]),
};

/** Id-bearing routes, anchored end to end. */
const PATTERNS: Readonly<Record<GatewayMethod, readonly RegExp[]>> = {
  GET: [
    new RegExp(`^/api/v1/districts/${ID}/stats$`),
    new RegExp(`^/api/v1/districts/${ID}/queue$`),
    // NASA FIRMS regional fire activity plus NASA POWER fire weather.
    // Read-only; both provider keys stay on the backend.
    new RegExp(`^/api/v1/districts/${ID}/fire-activity$`),
    // Why the fleet panel is drawing what it is drawing: when the slow loop
    // last ran, under which correlation id, which agents recorded it and how
    // much, and what the newest audit event in the store is. Read-only, counts
    // and ids only, and reachable from a browser on purpose -- the moment
    // somebody needs it is the moment the console is the thing that looks
    // broken, and telling them to go and curl the backend directly is telling
    // them to reproduce a live-mode credential first.
    new RegExp(`^/api/v1/districts/${ID}/slow-loop/diagnostics$`),
    new RegExp(`^/api/v1/buildings/${ID}$`),
    new RegExp(`^/api/v1/buildings/${ID}/timeline$`),
    new RegExp(`^/api/v1/buildings/${ID}/geometry$`),
    // The photograph shown beside the massing model. Read-only, and the
    // Maps key never leaves the backend -- the response carries a data:
    // URI, so the browser never talks to the provider.
    new RegExp(`^/api/v1/buildings/${ID}/imagery$`),
    // The ground plane under the regional fire map. One cached image, proxied
    // so the browser never talks to the map provider and the Maps key stays on
    // the server -- the same reason building imagery arrives as a data URI.
    new RegExp(`^/api/v1/districts/${ID}/fire-activity/basemap(\\?style=(terrain|satellite))?$`),
    // One square of the terrain mesh. Bounded here as well as at the backend:
    // the path shape admits only the two grids the mesh is built from and only
    // integer tile coordinates, so this cannot be widened into a general proxy
    // by a query string. Not under `/districts` because the region is a
    // property of the process, not of a district -- and a nine-segment path
    // would not fit `MAX_PATH_SEGMENTS` anyway.
    new RegExp(`^/api/v1/terrain/(elevation|imagery)/\\d{1,2}/\\d{1,7}/\\d{1,7}$`),
    new RegExp(`^/api/v1/incidents/${ID}/stream$`),
    // The prose being composed, token by token. Read-only, and provisional:
    // every frame it carries says so, and the persisted emission still arrives
    // on the brief stream above.
    new RegExp(`^/api/v1/incidents/${ID}/brief/stream-enriched$`),
    new RegExp(`^/api/v1/incidents/${ID}/log$`),
    // Entry packages: the list a commander reviews, one package in full, and
    // the printed sheet. All three are reads of the incident log, which is
    // where a package lives -- there is no separate store to widen.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages$`),
    // Why the loop has not composed one yet: autonomy on or off, whether this
    // backend holds the incident, when the fallback deadline fires, and the
    // type and message of the last composition that failed. Listed explicitly
    // even though the id-shaped pattern below already admits the literal
    // `diagnostics` -- the day that pattern is tightened to real package ids
    // this must not silently stop being reachable, because the moment somebody
    // needs it is the moment nothing else is working.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages/diagnostics$`),
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages/${ID}$`),
    // The PDF. Seven segments, inside `MAX_PATH_SEGMENTS`, and the only route
    // here whose response is bytes rather than JSON -- see the gateway's own
    // `application/pdf` branch, which pipes it instead of decoding it as text.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages/${ID}/pdf$`),
    // The log as it is written, one frame per entry. Read-only and resumable
    // by sequence; it carries the same ids, keys and reasons the document does.
    new RegExp(`^/api/v1/incidents/${ID}/log/stream$`),
    new RegExp(`^/api/v1/internal/audit/incidents/${ID}/replay$`),
  ],
  POST: [
    // Runs one slow-loop pass: sources polled, facts written, conflicts
    // detected, queue re-ranked. It writes, so it is deliberately listed
    // rather than inherited -- but it is idempotent, re-deriving what
    // exists and writing none of it again, which is why a console button
    // may reach it at all.
    new RegExp(`^/api/v1/districts/${ID}/poll$`),
    new RegExp(`^/api/v1/incidents/${ID}/brief/enrich$`),
    // The 911 transcript, read after the incident is already open.
    //
    // The console used to send the narrative on the open itself, so this route
    // was never reached from the browser and was never listed. Splitting them
    // -- so the instant brief is not stuck behind a Gemini call -- meant the
    // console started calling a route the gateway does not know, and every
    // dispatch ended in `The call was not read: no such route`.
    new RegExp(`^/api/v1/incidents/${ID}/intake$`),
    new RegExp(`^/api/v1/incidents/${ID}/resolutions$`),
    new RegExp(`^/api/v1/incidents/${ID}/resources$`),
    new RegExp(`^/api/v1/incidents/${ID}/thermal$`),
    // The autonomous sweep. Advances one face per call, so the console drives
    // the cadence and the agent does every reading.
    new RegExp(`^/api/v1/incidents/${ID}/drone-sweep$`),
    new RegExp(`^/api/v1/incidents/${ID}/close$`),
    new RegExp(`^/api/v1/incidents/${ID}/approvals/${ID}$`),
    // Compose a package: readiness, path and brief in one pass. A write, and
    // listed as one -- it stages two approval cards and sends nothing.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages$`),
    // One human tap on one half. The trailing segment is `entry-path` or
    // `crew-brief` and is bounded to those two here rather than left as a
    // generic id: this proxy has no business forwarding a half the backend
    // does not have, and the backend's own 422 is not a reason to relay it.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages/${ID}/approvals/(entry-path|crew-brief)$`),
    // The send. Eight segments would not fit, and this is seven; the backend
    // refuses with 422 unless both halves are granted, which is the check that
    // matters and it is not this file's.
    new RegExp(`^/api/v1/incidents/${ID}/entry-packages/${ID}/dispatch$`),
    new RegExp(`^/api/v1/conflicts/${ID}/referral$`),
    new RegExp(`^/api/v1/referrals/${ID}/approve$`),
  ],
};

/**
 * A path segment safe to concatenate onto the backend's base URL.
 *
 * The charset excludes `/`, `\`, `:` and `%`, so a segment can neither escape
 * the path nor re-introduce one through a second decoding pass. Next.js hands
 * these already URL-decoded; `%2e%2e` therefore arrives as `..` and is caught
 * by the explicit dot-run check rather than by the charset alone.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._~-]{1,128}$/;

export function isSafeSegment(segment: string): boolean {
  if (!SAFE_SEGMENT.test(segment)) return false;
  // `.` and `..` are traversal even though every character in them is safe.
  if (/^\.+$/.test(segment)) return false;
  if (segment.includes('..')) return false;
  return true;
}

/**
 * Resolve the incoming segments to a backend path, or `null` if the console has
 * no business asking for it.
 *
 * `null` is the only failure mode: the caller answers every `null` with the
 * same 404, so a rejected traversal and a rejected-but-real endpoint are
 * indistinguishable from outside.
 */
export function gatewayTargetPath(segments: readonly string[], method: GatewayMethod): string | null {
  if (segments.length === 0 || segments.length > MAX_PATH_SEGMENTS) return null;
  for (const segment of segments) {
    if (!isSafeSegment(segment)) return null;
  }

  const path = `/${segments.join('/')}`;

  // The internal namespace is gated here, after the allowlist and independently
  // of it, so that adding a pattern above can never widen it by accident.
  if (path.startsWith(INTERNAL_PREFIX)) {
    const internalReadAllowed =
      method === 'GET' && (INTERNAL_READ_ONLY.has(path) || INTERNAL_REPLAY.test(path));
    if (!internalReadAllowed) return null;
  }

  if (EXACT[method].has(path)) return path;
  if (PATTERNS[method].some((pattern) => pattern.test(path))) return path;
  return null;
}
