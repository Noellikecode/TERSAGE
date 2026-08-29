/**
 * The console's gateway to the backend.
 *
 * Every browser request goes through here rather than straight to the API, for
 * one reason: **the console's credential never reaches the browser.** It is
 * obtained on the server, attached here, and the client only ever talks to its
 * own origin.
 *
 * That also means one place handles SSE. The incident stream is proxied as a
 * stream -- the body is piped through untouched, so `Last-Event-ID` resume and
 * frame ordering work exactly as the backend sends them.
 *
 * Because this route attaches a privileged credential and the console service
 * is publicly reachable, it forwards an **allowlist** and nothing else. See
 * `lib/api/gateway-allowlist.ts` for the list and the reasoning; anything off it
 * is answered with a 404, which is also the answer for a traversal attempt and
 * for a real endpoint the console has no business calling. The three cases are
 * indistinguishable from outside on purpose.
 */

import { NextRequest } from 'next/server';

import { backendCredential, type BackendCredential } from '@/lib/api/backend-auth';
import { gatewayTargetPath, type GatewayMethod } from '@/lib/api/gateway-allowlist';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/**
 * Where the backend is, read at request time.
 *
 * `FIRSTDUE_API_BASE_URL` first: it is a plain server variable, so Cloud Run's
 * value is read from the environment at runtime. `NEXT_PUBLIC_API_BASE_URL` is
 * inlined by webpack at *build* time and is not set during the Docker build, so
 * it is only good as a local-development fallback.
 */
function backendBaseUrl(): string {
  return (
    process.env.FIRSTDUE_API_BASE_URL ??
    process.env.NEXT_PUBLIC_API_BASE_URL ??
    'http://localhost:8000'
  );
}

function backendHeaders(request: NextRequest, credential: BackendCredential): Headers {
  // Built from scratch, not copied: a client-supplied Authorization header must
  // never survive into the upstream request.
  const headers = new Headers();
  headers.set('Accept', request.headers.get('accept') ?? 'application/json');

  if (credential.kind === 'bearer') {
    headers.set('Authorization', credential.header);
  }
  // SSE resume: the browser sends this automatically on reconnect, and the
  // backend needs it to know where to pick up.
  const lastEventId = request.headers.get('last-event-id');
  if (lastEventId) {
    headers.set('Last-Event-ID', lastEventId);
  }
  // Conditional revalidation for tiles.
  //
  // The terrain route answers with a content-derived `ETag`, and without this
  // the validator never reaches it: this route builds the upstream request from
  // scratch, so an unlisted header is simply dropped. That made the backend's
  // ETag inert through the console -- every revalidation returned a full PNG of
  // a hillside the browser already had. A validator is not a credential and
  // carries nothing about the caller, so it is safe on the allowlist.
  const ifNoneMatch = request.headers.get('if-none-match');
  if (ifNoneMatch) {
    headers.set('If-None-Match', ifNoneMatch);
  }
  const correlationId = request.headers.get('x-correlation-id');
  if (correlationId) {
    headers.set('X-Correlation-ID', correlationId);
  }
  return headers;
}

/** An unreachable backend is a state to render, not an exception to throw. */
function unreachable(message: string): Response {
  return Response.json(
    {
      error: {
        code: 'BACKEND_UNREACHABLE',
        message,
        details: {},
        request_id: null,
        correlation_id: null,
      },
    },
    { status: 503 },
  );
}

/**
 * The single answer to everything off the allowlist.
 *
 * 404 rather than 403: a 403 tells the caller the endpoint exists.
 */
function notFound(): Response {
  return Response.json(
    {
      error: {
        code: 'NOT_FOUND',
        message: 'no such route',
        details: {},
        request_id: null,
        correlation_id: null,
      },
    },
    { status: 404 },
  );
}

interface Resolved {
  url: string;
  headers: Headers;
}

/**
 * Validate, authorize and authenticate -- or hand back the response to send.
 */
async function resolve(
  request: NextRequest,
  segments: string[] | undefined,
  method: GatewayMethod,
): Promise<Resolved | Response> {
  const path = gatewayTargetPath(segments ?? [], method);
  if (!path) return notFound();

  const credential = await backendCredential();
  if (credential.kind === 'unavailable') {
    // Never fall through to an unauthenticated upstream call.
    return unreachable(credential.message);
  }

  return {
    url: `${backendBaseUrl()}${path}${request.nextUrl.search}`,
    headers: backendHeaders(request, credential),
  };
}

export async function GET(request: NextRequest, context: { params: { path: string[] } }) {
  const resolved = await resolve(request, context.params.path, 'GET');
  if (resolved instanceof Response) return resolved;

  try {
    const upstream = await fetch(resolved.url, {
      headers: resolved.headers,
      cache: 'no-store',
    });

    const contentType = upstream.headers.get('content-type') ?? '';

    // A revalidation that matched. Handled before anything reads a body,
    // because a 304 has none and carries no content-type -- it would otherwise
    // fall through to the text branch and be relabelled as JSON, which is a
    // 304 the browser cannot use for the image it asked about.
    if (upstream.status === 304) {
      const revalidated: Record<string, string> = {};
      const etag = upstream.headers.get('etag');
      if (etag) revalidated['ETag'] = etag;
      const cached = upstream.headers.get('cache-control');
      if (cached) revalidated['Cache-Control'] = cached;
      return new Response(null, { status: 304, headers: revalidated });
    }

    if (contentType.includes('text/event-stream')) {
      // Piped, not buffered: the whole point of the stream is that frames
      // arrive as they are produced.
      return new Response(upstream.body, {
        status: upstream.status,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache, no-transform',
          Connection: 'keep-alive',
        },
      });
    }

    if (contentType.startsWith('image/')) {
      // Bytes, not text. `upstream.text()` decodes as UTF-8, which corrupts a
      // PNG silently -- and an elevation tile's RGB *is* the height data, so a
      // corrupted one is not a broken picture but a wrong mountain. Piped for
      // the same reason the event stream is: there is nothing to inspect.
      //
      // The upstream's cache header is carried through. Terrain does not move,
      // and re-fetching a square on every camera nudge would spend a metered
      // quota to redraw an identical hillside.
      const headers: Record<string, string> = { 'Content-Type': contentType };
      const cacheControl = upstream.headers.get('cache-control');
      if (cacheControl) headers['Cache-Control'] = cacheControl;
      // And the validator, so the browser can ask "still this one?" next time.
      const etag = upstream.headers.get('etag');
      if (etag) headers['ETag'] = etag;
      return new Response(upstream.body, { status: upstream.status, headers });
    }

    if (contentType.startsWith('application/pdf')) {
      // Bytes, for the same reason the image branch is: `upstream.text()`
      // decodes as UTF-8, and a PDF's cross-reference table is byte offsets
      // into the file. Re-encoding shifts every one of them, and the browser
      // gets a document it will not open -- silently, with a 200 on it.
      //
      // The disposition is carried through so the printed brief downloads under
      // the backend's own `crew-brief-{package_id}.pdf` rather than under the
      // gateway path, which is what a records clerk has to file it by.
      const headers: Record<string, string> = { 'Content-Type': contentType };
      const disposition = upstream.headers.get('content-disposition');
      if (disposition) headers['Content-Disposition'] = disposition;
      return new Response(upstream.body, { status: upstream.status, headers });
    }

    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { 'Content-Type': contentType || 'application/json' },
    });
  } catch (caught) {
    return unreachable(caught instanceof Error ? caught.message : 'request failed');
  }
}

export async function POST(request: NextRequest, context: { params: { path: string[] } }) {
  const resolved = await resolve(request, context.params.path, 'POST');
  if (resolved instanceof Response) return resolved;

  resolved.headers.set('Content-Type', 'application/json');
  const body = await request.text();

  try {
    const upstream = await fetch(resolved.url, {
      method: 'POST',
      headers: resolved.headers,
      body: body || '{}',
      cache: 'no-store',
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
      },
    });
  } catch (caught) {
    return unreachable(caught instanceof Error ? caught.message : 'request failed');
  }
}
