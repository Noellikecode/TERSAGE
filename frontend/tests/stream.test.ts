/**
 * The brief stream's two rules.
 *
 * Nothing is rendered that is not in the log, and the screen never goes
 * backwards.
 */

import { describe, expect, it } from 'vitest';

import { applyEmission } from '@/lib/api/stream';
import { emission } from './fixtures';

describe('applying a streamed emission', () => {
  it('keeps versions in order however they arrive', () => {
    const three = emission({ version: 3, stage: 'AMENDMENT' });
    const one = emission({ version: 1 });
    const two = emission({ version: 2, stage: 'ENRICHED' });

    let state = applyEmission([], three);
    state = applyEmission(state, one);
    state = applyEmission(state, two);

    expect(state.map((e) => e.version)).toEqual([1, 2, 3]);
  });

  it('drops an emission that is not in the log', () => {
    const unpersisted = emission({ version: 2, persisted_at: null });
    expect(applyEmission([], unpersisted)).toEqual([]);
  });

  it('ignores a redelivered version rather than duplicating it', () => {
    const first = applyEmission([], emission({ version: 1 }));
    const again = applyEmission(first, emission({ version: 1, emission_id: 'other' }));
    expect(again).toHaveLength(1);
    expect(again[0]!.emission_id).toBe('emission-1');
  });

  it('never rewinds the commander’s screen on reconnect', () => {
    let state = applyEmission([], emission({ version: 1 }));
    state = applyEmission(state, emission({ version: 2, stage: 'ENRICHED' }));
    // A resume that redelivers version 1 must not become the latest.
    state = applyEmission(state, emission({ version: 1 }));
    expect(state[state.length - 1]!.version).toBe(2);
  });
});
