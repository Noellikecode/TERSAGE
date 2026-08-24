/**
 * The console's own liveness signal.
 *
 * Cloud Run points both the startup probe and the liveness probe here. It
 * answers one question and only one: **is this Next.js process serving?**
 *
 * It deliberately does not touch the backend. A probe that fails because a
 * *different* service is down would have Cloud Run restart the console for
 * someone else's outage, and a console that can still render "backend
 * unreachable" is more useful than a console in a crash loop. Backend health is
 * reported inside the page, by `BackendStatus`, where an operator can see it.
 */

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export function GET(): Response {
  return Response.json(
    {
      status: 'ok',
      service: 'firstdue-console',
      uptime_s: Math.round(process.uptime()),
    },
    { status: 200, headers: { 'Cache-Control': 'no-store' } },
  );
}
