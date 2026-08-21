/**
 * A state chip.
 *
 * Never colour alone: every variant carries a glyph and a text label, so the
 * meaning survives colourblindness and a washed-out tablet in daylight.
 */

export type PillTone = 'confirmed' | 'disputed' | 'unknown' | 'live' | 'alarm' | 'muted';

const TONES: Record<PillTone, { className: string; glyph: string }> = {
  confirmed: { className: 'border-confirmed text-confirmed', glyph: '●' },
  disputed: { className: 'border-disputed text-disputed', glyph: '▲' },
  unknown: { className: 'border-unknown text-unknown', glyph: '○' },
  live: { className: 'border-live text-live', glyph: '◆' },
  alarm: { className: 'border-alarm text-alarm', glyph: '■' },
  muted: { className: 'border-line text-muted', glyph: '·' },
};

export function StatusPill({
  tone,
  label,
  title,
}: {
  tone: PillTone;
  label: string;
  title?: string;
}) {
  const { className, glyph } = TONES[tone];
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 border px-2 py-0.5 text-micro uppercase tracking-wide ${className}`}
    >
      <span aria-hidden="true">{glyph}</span>
      <span>{label}</span>
    </span>
  );
}
