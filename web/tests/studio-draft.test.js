import { describe, expect, it } from 'vitest';

import { normalise, SECTIONS } from '../studio/draft.js';

describe('character drives in Studio', () => {
  it('preserves drives while normalising a server draft', () => {
    const draft = normalise({
      name: 'YuriQuant',
      drives: ['Research catalysts before acting', '', 'Report times in Seoul time'],
    });

    expect(draft.drives).toEqual([
      'Research catalysts before acting', 'Report times in Seoul time',
    ]);
  });

  it('renders drives as an editable list, not an executable goal field', () => {
    const field = SECTIONS.flatMap((section) => section.fields)
      .find((item) => item.key === 'drives');
    expect(field.type).toBe('list');
    expect(field.hint).toContain('not executable tasks');
  });
});
