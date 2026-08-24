/**
 * How the console gets a credential, and what it does when it cannot.
 *
 * The live path mints a Google-issued OIDC token from the Cloud Run metadata
 * server; the local path keeps using the static `make demo` token. The three
 * things that matter here are that the token is cached rather than re-minted
 * per request, that an unset audience leaves the demo path exactly as it was,
 * and that a failure produces "no credential" rather than a silently
 * unauthenticated upstream call.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { backendCredential, jwtExpiryMs, resetBackendCredentialCache } from '@/lib/api/backend-auth';

const AUDIENCE = 'https://firstdue-incident';
const ORIGINAL_ENV = { ...process.env };
const NOW = 1_700_000_000_000;

function base64Url(value: string): string {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** A token shaped like the metadata server's, expiring `ttlSeconds` from `NOW`. */
function idToken(ttlSeconds: number, marker = 'a'): string {
  const payload = base64Url(
    JSON.stringify({ aud: AUDIENCE, exp: Math.floor(NOW / 1000) + ttlSeconds, marker }),
  );
  return `${base64Url('{"alg":"RS256"}')}.${payload}.sig-${marker}`;
}

beforeEach(() => {
  resetBackendCredentialCache();
  delete process.env.FIRSTDUE_API_AUDIENCE;
  delete process.env.FIRSTDUE_CONSOLE_TOKEN;
});

afterEach(() => {
  process.env = { ...ORIGINAL_ENV };
  resetBackendCredentialCache();
});

describe('reading a token expiry', () => {
  it('reads exp out of the payload', () => {
    expect(jwtExpiryMs(idToken(3600))).toBe(Math.floor(NOW / 1000 + 3600) * 1000);
  });

  it('returns null for anything that is not a three-part JWT', () => {
    expect(jwtExpiryMs('not-a-jwt')).toBeNull();
    expect(jwtExpiryMs('a.b.c')).toBeNull();
    expect(jwtExpiryMs(`x.${base64Url('{"no":"exp"}')}.y`)).toBeNull();
  });
});

describe('local / fake mode', () => {
  it('uses the static console token when no audience is configured', async () => {
    process.env.FIRSTDUE_CONSOLE_TOKEN = 'demo-token';
    const fetchImpl = vi.fn();

    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });

    expect(credential).toEqual({ kind: 'bearer', header: 'Bearer demo-token' });
    // No audience means no metadata server: `make demo` runs off a laptop.
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('sends no credential at all when neither is configured', async () => {
    const fetchImpl = vi.fn();
    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });
    expect(credential).toEqual({ kind: 'none' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('live mode', () => {
  function metadata(token: string) {
    return vi.fn(async () => new Response(token, { status: 200 }));
  }

  beforeEach(() => {
    process.env.FIRSTDUE_API_AUDIENCE = AUDIENCE;
  });

  it('mints an identity token for the configured audience', async () => {
    const token = idToken(3600);
    const fetchImpl = metadata(token);

    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });

    expect(credential).toEqual({ kind: 'bearer', header: `Bearer ${token}` });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity' +
        `?audience=${encodeURIComponent(AUDIENCE)}`,
    );
    expect(new Headers(init.headers).get('metadata-flavor')).toBe('Google');
  });

  it('caches the token instead of minting one per request', async () => {
    const fetchImpl = metadata(idToken(3600));

    await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });
    await backendCredential({ fetchImpl: fetchImpl as never, now: NOW + 60_000 });
    await backendCredential({ fetchImpl: fetchImpl as never, now: NOW + 120_000 });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('mints one token for a burst of concurrent requests', async () => {
    const fetchImpl = metadata(idToken(3600));
    await Promise.all(
      Array.from({ length: 5 }, () =>
        backendCredential({ fetchImpl: fetchImpl as never, now: NOW }),
      ),
    );
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('refreshes inside the safety margin, before the token actually expires', async () => {
    const first = idToken(600, 'first');
    const second = idToken(3600, 'second');
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response(first, { status: 200 }))
      .mockResolvedValueOnce(new Response(second, { status: 200 }));

    const before = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });
    expect(before).toEqual({ kind: 'bearer', header: `Bearer ${first}` });

    // 6 minutes later the token is still valid for 4 -- inside the 5-minute margin.
    const after = await backendCredential({
      fetchImpl: fetchImpl as never,
      now: NOW + 6 * 60_000,
    });
    expect(after).toEqual({ kind: 'bearer', header: `Bearer ${second}` });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it('falls back to the static token when the metadata server is unreachable', async () => {
    process.env.FIRSTDUE_CONSOLE_TOKEN = 'demo-token';
    const fetchImpl = vi.fn().mockRejectedValue(new Error('ENOTFOUND'));

    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });

    expect(credential).toEqual({ kind: 'bearer', header: 'Bearer demo-token' });
  });

  it('reports no credential when metadata fails and no static token is set', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('ENOTFOUND'));

    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });

    expect(credential.kind).toBe('unavailable');
    if (credential.kind === 'unavailable') {
      expect(credential.message).not.toContain('ENOTFOUND');
    }
  });

  it('treats a non-200 from the metadata server as no credential', async () => {
    const fetchImpl = vi.fn(async () => new Response('forbidden', { status: 403 }));
    const credential = await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });
    expect(credential.kind).toBe('unavailable');
  });

  it('keeps using a still-valid token when a refresh fails', async () => {
    const token = idToken(600, 'held');
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response(token, { status: 200 }))
      .mockRejectedValueOnce(new Error('ENOTFOUND'));

    await backendCredential({ fetchImpl: fetchImpl as never, now: NOW });
    const credential = await backendCredential({
      fetchImpl: fetchImpl as never,
      now: NOW + 6 * 60_000,
    });

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(credential).toEqual({ kind: 'bearer', header: `Bearer ${token}` });
  });
});
