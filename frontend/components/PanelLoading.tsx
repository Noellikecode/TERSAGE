/**
 * The one waiting state, shared by every panel that fetches or renders slowly.
 *
 * Three panels each drew their own: a line of grey text for imagery, a
 * different line for the 3D tiles, and nothing at all for the structure model
 * while WebGL built its scene. A blank rectangle and a rectangle that is
 * working look identical, and on a fireground the difference is whether an
 * officer waits or reloads.
 *
 * `role="status"` and `aria-live="polite"` because this replaces content that
 * is about to arrive, and a screen reader should hear that it is coming rather
 * than hear silence. The motion is `animate-pulse` on three dots, which
 * `prefers-reduced-motion` already stops at the browser level -- the label
 * carries the same message without it, which is why the label is not optional.
 */

export function PanelLoading({
  label,
  detail = null,
  testId,
}: {
  /** What is being waited for, in the words of the thing waiting. */
  label: string;
  /** One line on why it takes a moment, when there is an honest one. */
  detail?: string | null;
  testId?: string;
}) {
  return (
    <div
      className="flex min-h-[8rem] flex-col items-center justify-center gap-3 border border-dashed border-line bg-surface/40 p-6"
      role="status"
      aria-live="polite"
      data-testid={testId ?? 'panel-loading'}
    >
      <span aria-hidden="true" className="flex gap-1.5">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="h-2 w-2 animate-pulse rounded-full bg-live"
            style={{ animationDelay: `${index * 160}ms`, animationDuration: '1.1s' }}
          />
        ))}
      </span>
      <p className="text-body text-ink">{label}</p>
      {detail && <p className="max-w-prose text-center text-micro leading-5 text-muted">{detail}</p>}
    </div>
  );
}
