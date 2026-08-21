/**
 * Typed backend client.
 *
 * Two rules:
 *  - never throw: an unreachable backend is rendered, not crashed on;
 *  - always surface the correlation id, so a console screenshot is traceable
 *    to the exact backend log line and audit chain.
 */

import type {
  ApiErrorBody,
  ApiErrorEnvelope,
  ApiResult,
  Liveness,
  Readiness,
  SystemStatus,
} from './types';

export const DEFAULT_API_BASE_URL = 'http://localhost:8000';
const DEFAULT_TIMEOUT_MS = 4000;

export function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? DEFAULT_API_BASE_URL;
}

/**
 * The backend credential, on the server only.
 *
 * Deliberately *not* a `NEXT_PUBLIC_` variable: Next.js inlines those into the
 * client bundle, and a console token in the browser is a console token in
 * anyone's devtools. Server components and the gateway route read this;
 * browser calls go through the gateway and never see it.
 */
function serverToken(): string | undefined {
  if (typeof window !== 'undefined') return undefined;
  return process.env.FIRSTDUE_CONSOLE_TOKEN;
}

function unreachable(message: string): ApiErrorBody {
  return {
    code: 'BACKEND_UNREACHABLE',
    message,
    details: {},
    request_id: null,
    correlation_id: null,
  };
}

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false;
  }
  const candidate = (value as { error: unknown }).error;
  return (
    typeof candidate === 'object' &&
    candidate !== null &&
    'code' in candidate &&
    'message' in candidate
  );
}

export interface RequestOptions {
  baseUrl?: string;
  timeoutMs?: number;
  /** Propagates one causal chain across the console and the backend. */
  correlationId?: string;
  fetchImpl?: typeof fetch;
  signal?: AbortSignal;
}

export async function apiGet<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
  const base = options.baseUrl ?? apiBaseUrl();
  const doFetch = options.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = serverToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.correlationId) {
    headers['X-Correlation-ID'] = options.correlationId;
  }

  try {
    const response = await doFetch(`${base}${path}`, {
      headers,
      signal: options.signal ?? controller.signal,
      cache: 'no-store',
    });

    const payload: unknown = await response.json().catch(() => null);

    if (!response.ok) {
      if (isErrorEnvelope(payload)) {
        return { ok: false, error: payload.error, unreachable: false };
      }
      return {
        ok: false,
        error: unreachable(`backend returned HTTP ${response.status}`),
        unreachable: false,
      };
    }

    return { ok: true, data: payload as T };
  } catch (caught) {
    const message = caught instanceof Error ? caught.message : 'request failed';
    return { ok: false, error: unreachable(message), unreachable: true };
  } finally {
    clearTimeout(timeout);
  }
}

export function getSystemStatus(options: RequestOptions = {}): Promise<ApiResult<SystemStatus>> {
  return apiGet<SystemStatus>('/api/v1/system/status', options);
}

export function getReadiness(options: RequestOptions = {}): Promise<ApiResult<Readiness>> {
  return apiGet<Readiness>('/readyz', options);
}

export function getLiveness(options: RequestOptions = {}): Promise<ApiResult<Liveness>> {
  return apiGet<Liveness>('/healthz', options);
}

// ---------------------------------------------------------------- writes --

/**
 * POST through the same envelope discipline as GET.
 *
 * Returned, never thrown: a refused write is a state the console renders --
 * "this needs a chief" is information, not an exception.
 */
export async function apiPost<T>(
  path: string,
  body: unknown = {},
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const base = options.baseUrl ?? apiBaseUrl();
  const doFetch = options.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
  };
  const token = serverToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.correlationId) {
    headers['X-Correlation-ID'] = options.correlationId;
  }

  try {
    const response = await doFetch(`${base}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: options.signal ?? controller.signal,
      cache: 'no-store',
    });
    const payload: unknown = await response.json().catch(() => null);

    if (!response.ok) {
      if (isErrorEnvelope(payload)) {
        return { ok: false, error: payload.error, unreachable: false };
      }
      return {
        ok: false,
        error: unreachable(`backend returned HTTP ${response.status}`),
        unreachable: false,
      };
    }
    return { ok: true, data: payload as T };
  } catch (caught) {
    const message = caught instanceof Error ? caught.message : 'request failed';
    return { ok: false, error: unreachable(message), unreachable: true };
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Browser-side calls go through the console's own origin.
 *
 * The backend credential lives on the server and is attached by the gateway
 * route, so it never reaches the browser at all.
 */
export const GATEWAY_PREFIX = '/api/gateway';

export function gatewayPath(apiPath: string): string {
  return `${GATEWAY_PREFIX}${apiPath}`;
}

export function browserGet<T>(apiPath: string, options: RequestOptions = {}) {
  return apiGet<T>(gatewayPath(apiPath), { ...options, baseUrl: '' });
}

export function browserPost<T>(apiPath: string, body: unknown = {}, options: RequestOptions = {}) {
  return apiPost<T>(gatewayPath(apiPath), body, { ...options, baseUrl: '' });
}
