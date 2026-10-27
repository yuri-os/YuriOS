import { describe, expect, it } from 'vitest';

import { EVERY_MODEL, archTarget, assemble, cameraFor, cellOf, inherited, modelTarget,
         resolve, usedTargets, withField } from '../studio/wording.js';

// The page resolves her camera's wording itself so the preview follows each
// keystroke. The rule must be forge/prompts.py's exactly — these mirror the
// cases tests/test_forge_prompts.py pins on the Python side.

const layer = (name, profile, editable = false) => ({
  name, editable,
  profile: { default: {}, architectures: {}, models: {}, ...profile },
});
const value = (resolved, key) => resolved[key].value;

describe('resolving her camera wording', () => {
  it('lets the narrowest cell win within a layer', () => {
    const house = layer('house', {
      default: { quality_preamble: 'EVERY' },
      architectures: { anima: { quality_preamble: 'ANIMA' } },
      models: { 'a.safetensors': { quality_preamble: 'MODEL-A' } },
    });
    expect(value(resolve([house], 'anima', 'a.safetensors'), 'quality_preamble')).toBe('MODEL-A');
    expect(value(resolve([house], 'anima', 'b.safetensors'), 'quality_preamble')).toBe('ANIMA');
    expect(value(resolve([house], 'sdxl', 'c.safetensors'), 'quality_preamble')).toBe('EVERY');
  });

  it('lets a later layer win outright, even over a narrower cell', () => {
    const house = layer('house', { models: { 'a.safetensors': { quality_preamble: 'HOUSE-A' } } });
    const her = layer('her', { default: { quality_preamble: 'HERS' } });
    expect(resolve([house, her], 'anima', 'a.safetensors').quality_preamble)
      .toEqual({ value: 'HERS', source: 'her · every model' });
  });

  it('names the layer and the cell a value came from', () => {
    const house = layer('house', { architectures: { anima: { base_negative: 'N' } } });
    expect(resolve([house], 'anima', 'm').base_negative.source).toBe('house · architecture anima');
  });

  it('never counts an empty field as a value', () => {
    const shipped = layer('shipped', { default: { quality_postfix: 'KEEP' } });
    const her = layer('her', { default: { quality_postfix: '   ' } });
    expect(value(resolve([shipped, her], 'sdxl', ''), 'quality_postfix')).toBe('KEEP');
  });
});

describe('editing one cell', () => {
  it('drops an emptied field, and an emptied cell with it', () => {
    let profile = withField(undefined, modelTarget('m'), 'quality_postfix', 'POST');
    expect(cellOf(profile, modelTarget('m'))).toEqual({ quality_postfix: 'POST' });
    profile = withField(profile, modelTarget('m'), 'quality_postfix', '');
    expect(profile.models).toEqual({});
  });

  it('does not mutate the profile it was given', () => {
    const before = { default: { quality_preamble: 'A' }, architectures: {}, models: {} };
    withField(before, EVERY_MODEL, 'quality_preamble', 'B');
    expect(before.default.quality_preamble).toBe('A');
  });

  it('shows what a cell would inherit, from its own scope down', () => {
    const shipped = layer('shipped', { default: { base_negative: 'SHIPPED' } });
    const house = layer('house', { architectures: { anima: { base_negative: 'HOUSE-ANIMA' } } }, true);
    const her = layer('her', { models: { 'a.safetensors': { base_negative: 'HER-A' } } }, true);
    const models = [{ key: 'a.safetensors', architecture: 'anima' }];
    const layers = [shipped, house, her];

    // her cell for model a: everything below it, minus that cell itself
    expect(inherited(layers, 'her', modelTarget('a.safetensors'), models).base_negative.value)
      .toBe('HOUSE-ANIMA');
    // the house's anima cell: only the shipped layer is beneath it
    expect(inherited(layers, 'house', archTarget('anima'), models).base_negative.value)
      .toBe('SHIPPED');
  });

  it('knows which kind of model a listed checkpoint is', () => {
    const models = [{ key: 'a.safetensors', architecture: 'anima' }];
    expect(cameraFor(modelTarget('a.safetensors'), models)).toEqual({ arch: 'anima', model: 'a.safetensors' });
    expect(cameraFor(modelTarget('unknown.safetensors'), models)).toEqual({ arch: '', model: 'unknown.safetensors' });
    expect(cameraFor(archTarget('sdxl'), models)).toEqual({ arch: 'sdxl', model: '' });
  });

  it('lists the cells set in either editable layer, and only those', () => {
    const used = usedTargets([
      layer('shipped', { architectures: { anima: { base_negative: 'x' } } }),
      layer('house', { models: { m: { quality_postfix: 'y' } } }, true),
      layer('her', { default: { quality_preamble: 'z' } }, true),
    ]);
    expect([...used].sort()).toEqual([EVERY_MODEL, modelTarget('m')].sort());
  });
});

describe('the preview', () => {
  it('assembles the prompt the way Character.assemble does', () => {
    const resolved = resolve([layer('house', {
      default: { quality_preamble: 'PRE', quality_postfix: 'POST',
                 base_negative: 'NEG', character_negative: 'PERSON' },
    })], 'sdxl', '');
    expect(assemble(resolved, 'ID', 'HERS')).toEqual({
      positive: 'PRE ID ‹scene› POST',
      negative: 'NEG PERSON HERS',
    });
  });
});
