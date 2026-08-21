/**
 * The console's gateway to the backend.
 *
 * Every browser request goes through here rather than straight to the API, for
 * one reason: **the console's credential never reaches the browser.** It is read
 * from the server environment, attached here, and the client only ever talks to
 * its own origin.
 *
 * That also means one place handles SSE. The incident stream is proxied as a
 * stream -- the body is piped through untouched, so `Last-Event-ID` resume and
 * frame ordering work exactly as the backend sends them.
 */

import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const BACKEND = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';

/** Read at request time, never at build time, and never sent to the client. */
function consoleToken(): string | undefined {
  return process.env.FIRSTDUE_CONSOLE_TOKEN;
}

function backendHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  headers.set('Accept', request.headers.get('accept') ?? 'application/json');

  const token = consoleToken();
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  // SSE resume: the browser sends this automatically on reconnect, and the
  // backend needs it to know where to pick up.
  const lastEventId = request.headers.get('last-event-id');
  if (lastEventId) {
    headers.set('Last-Event-ID', lastEventId);
  }
  const correlationId = request.headers.get('x-correlation-id');
  if (correlationId) {
    headers.set('X-Correlation-ID', correlationId);
  }
  return headers;
}

function targetUrl(request: NextRequest, path: string[]): string {
  const search = request.nextUrl.search;
  return `${BACKEND}/${path.join('/')}${search}`;
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

export async function GET(request: NextRequest, context: { params: { path: string[] } }) {
  const headers = backendHeaders(request);
  try {
    const upstream = await fetch(targetUrl(request, context.params.path), {
      headers,
      cache: 'no-store',
    });

    const contentType = upstream.headers.get('content-type') ?? '';
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

    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { 'Content-Type': contentType || 'application/json' },
    });
  } catch (caught) {
    return unreachable(caught instanceof Error ? caught.message : 'request failed');
  }
}

export async function POST(request: NextRequest, context: { params: { path: string[] } }) {
  const headers = backendHeaders(request);
  headers.set('Content-Type', 'application/json');
  const body = await request.text();

  try {
    const upstream = await fetch(targetUrl(request, context.params.path), {
      method: 'POST',
      headers,
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
