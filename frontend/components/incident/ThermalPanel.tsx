'use client';

/**
 * Thermal coverage, face by face.
 *
 * All four faces always appear. A face nobody flew reads UNSCANNED and is
 * styled like an unknown, never like a cool one -- the single most dangerous
 * misreading this console could invite is an unflown Charlie side looking
 * clear.
 *
 * Every reading carries the sentence that thermal measures surface temperature
 * and cannot see through walls.
 */

import { StatusPill } from '@/components/StatusPill';
import type { FaceView } from '@/lib/api/types';

export const FACES = ['ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'] as const;

export const THERMAL_CAVEAT =
  'Thermal imaging measures the surface temperature of the exterior skin. It cannot see through walls, and a hot surface has many causes.';

export function ThermalPanel({
  faces,
  onRegister,
  busy = false,
}: {
  faces: FaceView[];
  onRegister?: (face: string) => void;
  busy?: boolean;
}) {
  const byLabel = new Map(faces.map((face) => [face.label, face]));

  return (
    <section aria-labelledby="thermal-heading" className="space-y-2">
      <h3 id="thermal-heading" className="text-micro uppercase tracking-widest text-muted">
        Thermal coverage
      </h3>
      <ul className="grid grid-cols-2 gap-2">
        {FACES.map((label) => {
          const face = byLabel.get(label);
          const thermal = face?.thermal;
          const scanned = thermal?.kind === 'QUANTITY';
          const unavailable = thermal?.kind === 'UNAVAILABLE';
          return (
            <li key={label} className="border border-line bg-surface p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-ink">{label}</span>
                <StatusPill
                  tone={scanned ? 'confirmed' : unavailable ? 'alarm' : 'unknown'}
                  label={scanned ? 'scanned' : unavailable ? 'unavailable' : 'unscanned'}
                />
              </div>
              <p
                className={`mt-1 font-mono text-micro ${
                  scanned ? 'text-ink' : unavailable ? 'text-alarm' : 'text-unknown'
                }`}
              >
                {thermal?.kind === 'QUANTITY'
                  ? `${thermal.magnitude.toFixed(0)} ${thermal.unit} peak surface`
                  : thermal?.kind === 'UNAVAILABLE'
                    ? `UNAVAILABLE — ${thermal.reason}`
                    : 'UNSCANNED — no coverage'}
              </p>
              {onRegister && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onRegister(label)}
                  className="mt-2 border border-line px-2 py-0.5 text-micro text-muted hover:text-ink disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
                >
                  Register pass
                </button>
              )}
            </li>
          );
        })}
      </ul>
      <p className="text-micro leading-5 text-muted">{THERMAL_CAVEAT}</p>
    </section>
  );
}
