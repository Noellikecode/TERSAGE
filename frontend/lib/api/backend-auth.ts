/**
 * How the console proves who it is to the backend.
 *
 * Server-only. Nothing here is imported by a client component, and the token
 * itself is never logged and never put in a response body -- the gateway route
 * attaches it to the upstream request and that is the whole of its travel.
 *
 * Two modes, chosen by whether `FIRSTDUE_API_AUDIENCE` is set:
 *
 * - **Live.** Cloud Run's metadata server mints a Google-issued OIDC identity
 *   token for the console's service account, scoped to that audience. The
 *   backend verifies it with `google-auth` and maps the identity to a role. A
 *   static shared secret would be the weaker door and live mode does not accept
 *   one.
 * - **Local / fake.** No audience is configured, so the static
 *   `FIRSTDUE_CONSOLE_TOKEN` is used exactly as before. `make demo` derives it
 *   from `DEMO_SEED`, and this path is unchanged.
 *
 * The token is cached in module scope and refreshed ahead of its `exp`, because
 * a metadata round trip on every console call would put a second network hop in
 * front of every panel on the screen.
 */

const METADATA_IDENTITY_URL =
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity';

/** Refresh this far ahead of `exp`, so no in-flight request carries a token that expires mid-hop. */
const REFRESH_MARGIN_MS = 5 * 60 * 1000;

/** Used when the token carries no parsable `exp`. Metadata tokens live an hour; this is deliberately pessimistic. */
const FALLBACK_TTL_MS = 10 * 60 * 1000;

/** The metadata server is on-host. If it has not answered by now it is not going to. */
const METADATA_TIMEOUT_MS = 3000;

/**
 * What the gateway should put on the upstream request.
 *
 * `none` is not a failure: it is the local case where no credential is
 * configured at all, and it preserves the pre-existing behaviour of forwarding
 * without an `Authorization` header. `unavailable` *is* a failure, and the
 * caller must answer it with the 503 envelope rather than calling upstream
 * unauthenticated.
 */
export type BackendCredential =
  | { kind: 'bearer'; header: string }
  | { kind: 'none' }
  | { kind: 'unavailable'; message: string };

interface CachedToken {
  audience: string;
  token: string;
  /** Absolute epoch ms. */
  expiresAtMs: number;
}

let cached: CachedToken | null = null;
let inflight: Promise<CachedToken | null> | null = null;

/** Tests only: the cache is module scope, so it outlives a single test. */
export function resetBackendCredentialCache(): void {
  cached = null;
  inflight = null;
}

function base64UrlDecode(value: string): string {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/');
  return atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
}

/**
 * The token's `exp`, in epoch ms, or `null` if it cannot be read.
 *
 * The signature is not verified here and does not need to be: this is the
 * console reading its own freshly-minted token to decide when to ask for
 * another one. The backend is what verifies it.
 */
export function jwtExpiryMs(token: string): number | null {
  const parts = token.split('.');
  const payload = parts.length === 3 ? parts[1] : undefined;
  if (!payload) return null;
  try {
    const claims: unknown = JSON.parse(base64UrlDecode(payload));
    if (typeof claims !== 'object' || claims === null) return null;
    const exp = (claims as { exp?: unknown }).exp;
    return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : null;
  } catch {
    return null;
  }
}

/**
 * Ask the metadata server for an identity token.
 *
 * Returns `null` rather than throwing: an unreachable metadata server is a
 * state the caller renders as a 503, and a throw here would escape into the
 * route handler.
 */
async function mint(
  audience: string,
  doFetch: typeof fetch,
  now: number,
): Promise<CachedToken | null> {
  try {
    const response = await doFetch(
      `${METADATA_IDENTITY_URL}?audience=${encodeURIComponent(audience)}`,
      {
        headers: { 'Metadata-Flavor': 'Google' },
        cache: 'no-store',
        signal: AbortSignal.timeout(METADATA_TIMEOUT_MS),
      },
    );
    if (!response.ok) return null;
    const token = (await response.text()).trim();
    if (!token) return null;
    return {
      audience,
      token,
      expiresAtMs: jwtExpiryMs(token) ?? now + FALLBACK_TTL_MS,
    };
  } catch {
    // Deliberately swallowed and deliberately not logged: the only detail worth
    // reporting is "no identity", and the token must never reach a log line.
    return null;
  }
}

export interface CredentialOptions {
  fetchImpl?: typeof fetch;
  now?: number;
}

/**
 * The credential for one upstream request.
 *
 * Never throws. Never logs the token. Never returns it to the browser.
 */
export async function backendCredential(
  options: CredentialOptions = {},
): Promise<BackendCredential> {
  const doFetch = options.fetchImpl ?? fetch;
  const now = options.now ?? Date.now();

  const audience = process.env.FIRSTDUE_API_AUDIENCE?.trim();
  const staticToken = process.env.FIRSTDUE_CONSOLE_TOKEN?.trim();

  // Local / fake mode: unchanged from before this file existed.
  if (!audience) {
    return staticToken ? { kind: 'bearer', header: `Bearer ${staticToken}` } : { kind: 'none' };
  }

  if (cached && cached.audience === audience && now < cached.expiresAtMs - REFRESH_MARGIN_MS) {
    return { kind: 'bearer', header: `Bearer ${cached.token}` };
  }

  // One mint at a time: a burst of panel loads on a cold instance should cost
  // one metadata round trip, not one each.
  if (!inflight) {
    inflight = mint(audience, doFetch, now).finally(() => {
      inflight = null;
    });
  }
  const minted = await inflight;

  if (minted) {
    cached = minted;
    return { kind: 'bearer', header: `Bearer ${minted.token}` };
  }

  // Minting failed. A token that is inside its safety margin but not yet
  // expired still authenticates, and using it beats failing the request.
  if (cached && cached.audience === audience && now < cached.expiresAtMs) {
    return { kind: 'bearer', header: `Bearer ${cached.token}` };
  }
  if (staticToken) {
    return { kind: 'bearer', header: `Bearer ${staticToken}` };
  }
  return {
    kind: 'unavailable',
    message: 'the console could not obtain an identity token for the backend',
  };
}
