import { describe, expect, it } from 'vitest';

import { STATE_META, canonicalState } from '../shared/activity-state.js';

/* The ladder's vocabulary, held to itself.
 *
 * The rule this file exists to defend is written at the top of
 * shared/activity-state.js: whatever rung of mind/policy.py's ladder a character
 * is on, every screen prints THAT word. The bug it replaced was a second, coarser
 * vocabulary that folded IDLE and DORMANT into one "resting" label, so the same
 * fact about the same character read differently on two screens. Aliases here map
 * a synonym onto a rung; none of them may map two rungs onto one word.
 */
describe('canonicalState', () => {
  it('keeps every rung of the mind loop as its own word', () => {
    for (const rung of ['engaged', 'idle', 'dream', 'dormant']) {
      expect(canonicalState(rung)).toBe(rung);
    }
  });

  it('never collapses two ladder rungs into one label', () => {
    const labels = ['engaged', 'idle', 'dream', 'dormant'].map((s) => STATE_META[s].label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('reads a payload however it is cased or padded', () => {
    expect(canonicalState('  DORMANT ')).toBe('dormant');
    expect(canonicalState('Engaged')).toBe('engaged');
  });

  it('maps a process word onto its own state, not onto a ladder rung', () => {
    // Host.summary() falls back to process states for a character with no mind
    // running. "is the process up" is a different fact from "which rung" — these
    // must not answer with engaged/idle/dream/dormant.
    expect(canonicalState('running')).toBe('ready');
    expect(canonicalState('stopped')).toBe('offline');
    expect(canonicalState('failed')).toBe('attention');
  });

  it('answers unknown for anything it does not recognise, including nothing', () => {
    expect(canonicalState(undefined)).toBe('unknown');
    expect(canonicalState(null)).toBe('unknown');
    expect(canonicalState('')).toBe('unknown');
    expect(canonicalState('resting')).toBe('unknown');  // the label that used to bucket
  });

  it('gives every state a colour and a rank the boards can sort on', () => {
    for (const [name, meta] of Object.entries(STATE_META)) {
      expect(meta.label, name).toBeTypeOf('string');
      expect(meta.color, name).toMatch(/^#[0-9a-f]{6}$/i);
      expect(Number.isInteger(meta.rank), name).toBe(true);
    }
  });

  it('ranks a character who is present above one who is not', () => {
    expect(STATE_META.engaged.rank).toBeLessThan(STATE_META.dormant.rank);
    expect(STATE_META.dormant.rank).toBeLessThan(STATE_META.offline.rank);
  });

  it('resolves every alias to a state that exists', () => {
    for (const alias of ['active', 'running', 'online', 'stopped', 'disabled',
                         'error', 'failed', 'sleeping', 'paused']) {
      expect(STATE_META[canonicalState(alias)], alias).toBeDefined();
      expect(canonicalState(alias), alias).not.toBe('unknown');
    }
  });
});
