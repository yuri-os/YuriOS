/**
 * Every row of her own brain on the settings page has a name. The rows come
 * from the host's OVERRIDE_SCHEMA (yurios/world/rewire.py); a key missing from
 * the page's BRAIN_LABELS falls back to the bare key, which is how
 * "reply_length" ended up titled in snake_case beside "Reply token ceiling".
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { expect, it } from 'vitest';

const read = (path) => readFileSync(resolve(process.cwd(), path), 'utf8');

it('names every brain field the host sends', () => {
  const schema = read('../yurios/world/rewire.py')
    .split('OVERRIDE_SCHEMA')[1].split('\n)\n')[0];
  const keys = [...schema.matchAll(/\{"key": "(\w+)"/g)].map((m) => m[1]);
  expect(keys).toContain('reply_length');
  const labels = read('settings/character.js')
    .split('const BRAIN_LABELS = {')[1].split('};')[0];
  const named = [...labels.matchAll(/(\w+): "/g)].map((m) => m[1]);
  expect(keys.filter((key) => !named.includes(key))).toEqual([]);
});
