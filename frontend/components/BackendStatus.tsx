'use client';

import { useEffect, useState } from 'react';

import { getReadiness } from '@/lib/api/client';
import type { Readiness } from '@/lib/api/types';

import { StatusPill } from './StatusPill';

const POLL_MS = 5000;

type Connection =
  | { kind: 'checking' }
  | { kind: 'ready'; readiness: Readiness }
  | { kind: 'degraded'; readiness: Readiness }
  | { kind: 'unreachable'; message: string };

/**
 * Live backend connection state.
 *
 * Polls readiness rather than assuming the server render is still true. An
 * unreachable backend is rendered as unreachable -- never as "all clear".
 */
export function BackendStatus({ initial }: { initial?: Readiness }) {
  const [connection, setConnection] = useState<Connection>(
    initial ? { kind: initial.ready ? 'ready' : 'degraded', readiness: initial } : { kind: 'checking' },
  );

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function poll() {
      const result = await getReadiness({ signal: controller.signal });
      if (cancelled) return;
      if (result.ok) {
        setConnection(
          result.data.ready ? { kind: 'ready', readiness: result.data } : { kind: 'degraded', readiness: result.data },
        );
      } else {
        setConnection({ kind: 'unreachable', message: result.error.message });
      }
    }

    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="flex flex-wrap items-center gap-2" aria-live="polite" aria-atomic="true">
      {connection.kind === 'checking' && <StatusPill tone="muted" label="Checking backend" />}
      {connection.kind === 'ready' && <StatusPill tone="confirmed" label="Backend ready" />}
      {connection.kind === 'degraded' && (
        <StatusPill tone="disputed" label={`Backend ${connection.readiness.status}`} />
      )}
      {connection.kind === 'unreachable' && (
        <StatusPill tone="alarm" label="Backend unreachable" title={connection.message} />
      )}

      {(connection.kind === 'ready' || connection.kind === 'degraded') && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-micro text-muted">
          {(connection.readiness.checks ?? []).map((check) => (
            <li key={check.name}>
              <span aria-hidden="true">{check.ok ? '●' : '■'}</span> {check.name}: {check.detail}
            </li>
          ))}
        </ul>
      )}

      {connection.kind === 'unreachable' && (
        <p className="text-micro text-muted">
          Start it with <code className="text-ink">make demo</code>. Nothing on this screen is
          simulated while the backend is down.
        </p>
      )}
    </div>
  );
}
