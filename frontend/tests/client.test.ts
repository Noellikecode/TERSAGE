import { describe, expect, it, vi } from 'vitest';

import { apiGet, getSystemStatus } from '@/lib/api/client';
import type { SystemStatus } from '@/lib/api/types';

const STATUS: SystemStatus = {
  app: 'firstdue',
  version: '0.1.0',
  environment: 'test',
  mode: 'fake',
  storage_backend: 'memory',
  event_backend: 'memory',
  workspace_writes: 'fake',
  municipality_id: 'san-francisco-ca',
  districts: ['sffd-district-03'],
  instant_brief_budget_ms: 500,
  seeded_profiles: 8,
  published_agents: 8,
  capabilities: [{ id: 'fake-mode', label: 'Fake mode', status: 'AVAILABLE', phase: 1 }],
  disclosure: 'Decision-support prototype.',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('api client', () => {
  it('returns typed data on success', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(STATUS));
    const result = await getSystemStatus({ fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.mode).toBe('fake');
  });

  it('parses the backend error envelope', async () => {
    const envelope = {
      error: {
        code: 'STALE_VERSION',
        message: 'profile was modified concurrently',
        details: { expected_version: 3 },
        request_id: 'req_1',
        correlation_id: 'corr_1',
      },
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(envelope, 409));
    const result = await apiGet('/api/v1/anything', {
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('STALE_VERSION');
      expect(result.error.correlation_id).toBe('corr_1');
      expect(result.unreachable).toBe(false);
    }
  });

  it('reports an unreachable backend instead of throwing', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'));
    const result = await apiGet('/healthz', { fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.unreachable).toBe(true);
      expect(result.error.code).toBe('BACKEND_UNREACHABLE');
    }
  });

  it('forwards the correlation id so a screenshot is traceable', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(STATUS));
    await getSystemStatus({
      fetchImpl: fetchImpl as unknown as typeof fetch,
      correlationId: 'corr-77',
    });
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)['X-Correlation-ID']).toBe('corr-77');
  });
});
