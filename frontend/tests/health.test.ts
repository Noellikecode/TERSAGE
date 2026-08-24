/**
 * The console's liveness probe.
 *
 * Cloud Run points both the startup probe and the liveness probe at
 * `/api/health`. The property that matters is what it does *not* do: it must
 * not consult the backend, because a probe that fails when a different service
 * is down would have Cloud Run restart a console that is working fine.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { GET, dynamic } from '@/app/api/health/route';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('GET /api/health', () => {
  it('answers 200 with a small JSON body', async () => {
    const response = GET();
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('application/json');

    const body = (await response.json()) as { status: string; service: string; uptime_s: number };
    expect(body.status).toBe('ok');
    expect(body.service).toBe('firstdue-console');
    expect(typeof body.uptime_s).toBe('number');
  });

  it('is never statically prerendered', () => {
    expect(dynamic).toBe('force-dynamic');
  });

  it('does not touch the network, so a backend outage cannot fail the probe', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    expect(GET().status).toBe(200);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
