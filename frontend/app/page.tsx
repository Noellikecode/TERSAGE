import { CommandCenter } from '@/components/CommandCenter';
import { getReadiness, getSystemStatus } from '@/lib/api/client';

// The console reflects backend state at request time; nothing is cached.
export const dynamic = 'force-dynamic';

export default async function Page() {
  const [statusResult, readinessResult] = await Promise.all([getSystemStatus(), getReadiness()]);

  const status = statusResult.ok ? statusResult.data : null;
  const readiness = readinessResult.ok ? readinessResult.data : null;
  const error = statusResult.ok
    ? null
    : `Backend status unavailable: ${statusResult.error.message}`;

  return <CommandCenter status={status} readiness={readiness} error={error} />;
}
