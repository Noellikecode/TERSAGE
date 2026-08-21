/**
 * An honest empty state.
 *
 * The console never renders invented rows to look busy. When a surface has no
 * data yet, it says what will populate it and which phase delivers it.
 */
export function EmptyState({
  title,
  detail,
  phase,
}: {
  title: string;
  detail: string;
  phase?: number;
}) {
  return (
    <div className="border border-dashed border-line p-6 text-muted">
      <p className="text-ink">{title}</p>
      <p className="mt-1 max-w-prose text-micro leading-5">{detail}</p>
      {phase !== undefined && (
        <p className="mt-3 text-micro uppercase tracking-wide text-muted">
          Delivered in phase {phase}
        </p>
      )}
    </div>
  );
}
