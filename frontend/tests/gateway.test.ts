/**
 * The gateway is an allowlist, not a relay.
 *
 * The console service is publicly reachable and this route attaches a
 * privileged credential to whatever it forwards. So the properties under test
 * are the ones that stop it being an open door into the backend: only the paths
 * the console actually calls get through, the method matters, the internal
 * control plane is unreachable, and everything refused looks identical from
 * outside.
 */

import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GET, POST } from '@/app/api/gateway/[...path]/route';
import { resetBackendCredentialCache } from '@/lib/api/backend-auth';
import { gatewayTargetPath, MAX_PATH_SEGMENTS } from '@/lib/api/gateway-allowlist';

import { ADDRESS, DISTRICT, STATS } from './fixtures';

const BACKEND = 'http://backend.test';
const INCIDENT_ID = 'inc_9f2c41';

const ORIGINAL_ENV = { ...process.env };

function segments(path: string): string[] {
  return path.replace(/^\//, '').split('/');
}

/** A request as it arrives at the route: the console's own origin, gateway prefix. */
function gatewayRequest(path: string, init: RequestInit = {}): NextRequest {
  return new NextRequest(`http://console.test/api/gateway${path}`, init as never);
}

function upstream(body: unknown, contentType = 'application/json'): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': contentType },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetBackendCredentialCache();
  process.env.FIRSTDUE_API_BASE_URL = BACKEND;
  process.env.FIRSTDUE_CONSOLE_TOKEN = 'demo-console-token';
  delete process.env.FIRSTDUE_API_AUDIENCE;
  fetchMock = vi.fn(async () => upstream(STATS));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  process.env = { ...ORIGINAL_ENV };
  resetBackendCredentialCache();
});

describe('the allowlist', () => {
  const allowedGets = [
    '/readyz',
    '/healthz',
    '/api/v1/system/status',
    '/api/v1/registry/agents',
    '/api/v1/registry/subscriptions',
    `/api/v1/districts/${DISTRICT}/stats`,
    `/api/v1/districts/${DISTRICT}/queue`,
    // Why the fleet panel is drawing what it is drawing. Read-only, and
    // reachable from the browser because the moment somebody needs it is the
    // moment the console is the thing that looks broken.
    `/api/v1/districts/${DISTRICT}/slow-loop/diagnostics`,
    `/api/v1/buildings/${ADDRESS}`,
    `/api/v1/buildings/${ADDRESS}/timeline`,
    `/api/v1/buildings/${ADDRESS}/geometry`,
    `/api/v1/incidents/${INCIDENT_ID}/stream`,
    `/api/v1/incidents/${INCIDENT_ID}/brief/stream-enriched`,
    `/api/v1/incidents/${INCIDENT_ID}/log`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/diagnostics`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b/pdf`,
    '/api/v1/internal/audit/events',
    '/api/v1/internal/audit/decisions',
    `/api/v1/internal/audit/incidents/${INCIDENT_ID}/replay`,
  ];

  const allowedPosts = [
    '/api/v1/incidents',
    `/api/v1/incidents/${INCIDENT_ID}/brief/enrich`,
    `/api/v1/incidents/${INCIDENT_ID}/resolutions`,
    `/api/v1/incidents/${INCIDENT_ID}/resources`,
    `/api/v1/incidents/${INCIDENT_ID}/thermal`,
    `/api/v1/incidents/${INCIDENT_ID}/close`,
    `/api/v1/incidents/${INCIDENT_ID}/approvals/appr_17`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b/approvals/entry-path`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b/approvals/crew-brief`,
    `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b/dispatch`,
    '/api/v1/conflicts/conflict_0c93/referral',
    '/api/v1/referrals/ref_44/approve',
  ];

  it.each(allowedGets)('lets the console read %s', (path) => {
    expect(gatewayTargetPath(segments(path), 'GET')).toBe(path);
  });

  it.each(allowedPosts)('lets the console write %s', (path) => {
    expect(gatewayTargetPath(segments(path), 'POST')).toBe(path);
  });

  it('is scoped by method: a readable path is not thereby writable', () => {
    expect(gatewayTargetPath(segments(`/api/v1/incidents/${INCIDENT_ID}/log`), 'POST')).toBeNull();
    expect(gatewayTargetPath(segments('/api/v1/incidents'), 'GET')).toBeNull();
    expect(gatewayTargetPath(segments('/api/v1/internal/audit/events'), 'POST')).toBeNull();
  });

  it('refuses a path the console never calls', () => {
    expect(gatewayTargetPath(segments('/api/v1/secrets'), 'GET')).toBeNull();
    expect(gatewayTargetPath(segments('/api/v1/registry/agents/publish'), 'POST')).toBeNull();
    expect(gatewayTargetPath(segments('/openapi.json'), 'GET')).toBeNull();
  });

  it('refuses traversal, encoded traversal and absolute URLs', () => {
    expect(gatewayTargetPath(['api', 'v1', '..', '..', 'etc', 'passwd'], 'GET')).toBeNull();
    expect(gatewayTargetPath(['api', 'v1', 'buildings', '..%2f..%2fadmin'], 'GET')).toBeNull();
    // Next hands segments already decoded, so `%2e%2e` arrives looking like this.
    expect(gatewayTargetPath(['api', 'v1', 'buildings', '..'], 'GET')).toBeNull();
    expect(gatewayTargetPath(['http://evil.test/api/v1/registry/agents'], 'GET')).toBeNull();
    expect(gatewayTargetPath(['api', 'v1', 'buildings', 'a/b'], 'GET')).toBeNull();
    expect(gatewayTargetPath(['api', 'v1', 'buildings', 'a\\b'], 'GET')).toBeNull();
  });

  it('caps the number of segments', () => {
    const long = Array.from({ length: MAX_PATH_SEGMENTS + 1 }, () => 'a');
    expect(gatewayTargetPath(long, 'GET')).toBeNull();
    expect(gatewayTargetPath([], 'GET')).toBeNull();
  });
});

describe('the internal namespace', () => {
  const sealed = [
    '/api/v1/internal/events/push',
    '/api/v1/internal/callbacks/write',
    '/api/v1/internal/scheduler/tick',
    '/api/v1/internal/metrics',
    '/api/v1/internal/events/dead-letters',
  ];

  it.each(sealed)('is unreachable at %s, by any method', (path) => {
    expect(gatewayTargetPath(segments(path), 'GET')).toBeNull();
    expect(gatewayTargetPath(segments(path), 'POST')).toBeNull();
  });

  it('admits nothing under /api/v1/internal/ beyond the read-only audit views', () => {
    const reachable = ['GET', 'POST']
      .flatMap((method) =>
        [...sealed, '/api/v1/internal', '/api/v1/internal/audit', '/api/v1/internal/audit/x'].map(
          (path) => gatewayTargetPath(segments(path), method as 'GET' | 'POST'),
        ),
      )
      .filter((path) => path !== null);
    expect(reachable).toEqual([]);
  });
});

describe('the gateway route', () => {
  it('forwards an allowed read with the credential attached', async () => {
    const response = await GET(gatewayRequest(`/api/v1/districts/${DISTRICT}/stats`), {
      params: { path: segments(`/api/v1/districts/${DISTRICT}/stats`) },
    });

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BACKEND}/api/v1/districts/${DISTRICT}/stats`);
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer demo-console-token');
  });

  it('keeps the query string', async () => {
    await GET(gatewayRequest('/api/v1/internal/audit/events?limit=60'), {
      params: { path: segments('/api/v1/internal/audit/events') },
    });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${BACKEND}/api/v1/internal/audit/events?limit=60`);
  });

  it('pipes an event stream through untouched', async () => {
    fetchMock.mockResolvedValueOnce(
      upstream('event: brief\ndata: {}\n\n', 'text/event-stream; charset=utf-8'),
    );
    const response = await GET(gatewayRequest(`/api/v1/incidents/${INCIDENT_ID}/stream`), {
      params: { path: segments(`/api/v1/incidents/${INCIDENT_ID}/stream`) },
    });
    expect(response.headers.get('content-type')).toBe('text/event-stream');
    expect(await response.text()).toContain('event: brief');
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer demo-console-token');
  });

  it('answers a disallowed path with 404 and never calls upstream', async () => {
    const response = await GET(gatewayRequest('/api/v1/secrets'), {
      params: { path: ['api', 'v1', 'secrets'] },
    });
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe('NOT_FOUND');
  });

  it('answers /api/v1/internal/events/push with 404, not 403', async () => {
    const response = await POST(
      gatewayRequest('/api/v1/internal/events/push', { method: 'POST', body: '{}' }),
      { params: { path: segments('/api/v1/internal/events/push') } },
    );
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('answers a traversal with the same 404 as anything else', async () => {
    const refused = await GET(gatewayRequest('/api/v1/x'), {
      params: { path: ['api', 'v1', '..', '..', 'etc'] },
    });
    const unknown = await GET(gatewayRequest('/api/v1/x'), {
      params: { path: ['api', 'v1', 'nope'] },
    });
    expect(refused.status).toBe(404);
    expect(await refused.text()).toBe(await unknown.text());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards a write body', async () => {
    fetchMock.mockResolvedValueOnce(upstream({ incident_id: INCIDENT_ID }));
    const response = await POST(
      gatewayRequest('/api/v1/incidents', { method: 'POST', body: '{"address":"sf-0450-hayes"}' }),
      { params: { path: ['api', 'v1', 'incidents'] } },
    );
    expect(response.status).toBe(200);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BACKEND}/api/v1/incidents`);
    expect(init.body).toBe('{"address":"sf-0450-hayes"}');
  });

  it('drops a client-supplied Authorization header', async () => {
    await GET(
      gatewayRequest('/api/v1/registry/agents', {
        headers: { authorization: 'Bearer forged' },
      }),
      { params: { path: segments('/api/v1/registry/agents') } },
    );
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer demo-console-token');
  });

  it('returns the 503 envelope rather than calling upstream unauthenticated', async () => {
    process.env.FIRSTDUE_API_AUDIENCE = 'https://firstdue-incident';
    delete process.env.FIRSTDUE_CONSOLE_TOKEN;
    resetBackendCredentialCache();
    fetchMock.mockRejectedValue(new Error('metadata.google.internal is not reachable'));

    const response = await GET(gatewayRequest('/api/v1/registry/agents'), {
      params: { path: segments('/api/v1/registry/agents') },
    });

    expect(response.status).toBe(503);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe('BACKEND_UNREACHABLE');
    // The only call made was the one that failed to mint a token.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('metadata.google.internal');
  });
});

describe('tile revalidation through the gateway', () => {
  const TILE = '/api/v1/terrain/elevation/8/41/98';

  it('carries the backend’s ETag out to the browser', async () => {
    // The terrain route derives an ETag from the tile bytes. This route builds
    // its response from scratch, so a header it does not name is dropped -- and
    // a validator the browser never receives is one it can never send back.
    fetchMock.mockResolvedValueOnce(
      new Response('PNGBYTES', {
        status: 200,
        headers: {
          'content-type': 'image/png',
          etag: '"abc123"',
          'cache-control': 'private, max-age=604800, immutable',
        },
      }),
    );
    const response = await GET(gatewayRequest(TILE), { params: { path: segments(TILE) } });
    expect(response.headers.get('etag')).toBe('"abc123"');
    expect(response.headers.get('cache-control')).toContain('immutable');
  });

  it('sends the browser’s validator upstream', async () => {
    // The other half. Without it the backend can never answer 304 and every
    // revalidation returns a full PNG of a hillside that has not moved.
    fetchMock.mockResolvedValueOnce(
      new Response('PNGBYTES', { status: 200, headers: { 'content-type': 'image/png' } }),
    );
    await GET(gatewayRequest(TILE, { headers: { 'if-none-match': '"abc123"' } }), {
      params: { path: segments(TILE) },
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('if-none-match')).toBe('"abc123"');
  });

  it('passes a 304 through as a 304 rather than relabelling it as JSON', async () => {
    // A 304 has no body and no content-type, so it misses the image branch and
    // used to fall through to the text branch -- arriving as a 304 labelled
    // `application/json`, which the browser cannot apply to the image it asked
    // about.
    fetchMock.mockResolvedValueOnce(
      new Response(null, { status: 304, headers: { etag: '"abc123"' } }),
    );
    const response = await GET(gatewayRequest(TILE, { headers: { 'if-none-match': '"abc123"' } }), {
      params: { path: segments(TILE) },
    });
    expect(response.status).toBe(304);
    expect(response.headers.get('etag')).toBe('"abc123"');
    // No content-type at all, which is what a 304 is supposed to carry -- and
    // is what proves it did not take the text branch on the way out.
    expect(response.headers.get('content-type')).toBeNull();
  });

  it('still never forwards a caller-supplied Authorization header', async () => {
    // The allowlist grew by one header; the rule it protects has not changed.
    fetchMock.mockResolvedValueOnce(
      new Response('PNGBYTES', { status: 200, headers: { 'content-type': 'image/png' } }),
    );
    await GET(
      gatewayRequest(TILE, {
        headers: { authorization: 'Bearer stolen', 'if-none-match': '"abc123"' },
      }),
      { params: { path: segments(TILE) } },
    );
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer demo-console-token');
  });
});

/**
 * The printed brief through the gateway.
 *
 * The route had exactly two byte-preserving branches -- the event stream and
 * images -- and everything else fell through to `upstream.text()`. A PDF taking
 * that branch is decoded as UTF-8 and re-encoded, which moves every byte offset
 * in its cross-reference table: the browser receives a 200, saves a file, and
 * the file does not open. So `application/pdf` is piped, and the backend's own
 * `Content-Disposition` travels with it because that is the filename a records
 * clerk files the sheet under.
 */
describe('the entry package PDF through the gateway', () => {
  const PDF = `/api/v1/incidents/${INCIDENT_ID}/entry-packages/pkg_1a2b/pdf`;

  it('pipes the bytes rather than decoding them as text', async () => {
    // A byte that is not valid UTF-8 on its own, which is what makes this
    // assertion about encoding rather than about plumbing.
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34, 0x0a, 0x80, 0xff]);
    fetchMock.mockResolvedValueOnce(
      new Response(bytes, {
        status: 200,
        headers: {
          'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="crew-brief-pkg_1a2b.pdf"',
        },
      }),
    );
    const response = await GET(gatewayRequest(PDF), { params: { path: segments(PDF) } });
    expect(response.headers.get('content-type')).toBe('application/pdf');
    expect(response.headers.get('content-disposition')).toBe(
      'attachment; filename="crew-brief-pkg_1a2b.pdf"',
    );
    const received = new Uint8Array(await response.arrayBuffer());
    expect(Array.from(received)).toEqual(Array.from(bytes));
  });

  it('refuses to write to the PDF path', async () => {
    const response = await POST(gatewayRequest(PDF, { method: 'POST' }), {
      params: { path: segments(PDF) },
    });
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
