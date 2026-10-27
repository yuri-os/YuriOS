import { describe, expect, it } from 'vitest';
import {
  ago, asVariant, bytes, cardState, chosenVariant, matchesQuery, needsLookUp, normalizeQuery,
  previews, progressLine, supportIssueUrl, unlistedFiles, variantLabel, variantsOf, visibleCards,
} from '../settings/checkpoint-list.js';
import { parseRoute } from '../settings/model.js';

/* Pictures → Checkpoints (web/settings/checkpoints.js, SPEC §11.5).
 *
 * The server sends cards already sorted into what this build will do with
 * them; what the page decides is where each one stands on this machine, which
 * ones a filter keeps, and which previews it shows while NSFW is off.
 */

const card = (version_id, extra = {}) => ({
  version_id, name: `Model ${version_id}`, version: 'v1', file: `m${version_id}.safetensors`,
  family: 'SDXL · Illustrious', base_model: 'Illustrious', status: 'best', nsfw: false,
  downloads: 100, size_bytes: 6.5e9, note: 'Loads as-is.', url: `https://civitai.com/models/${version_id}`,
  images: [], ...extra,
});

describe('where a card stands', () => {
  it('a running download wins, then the file in the folder, then a partial one', () => {
    const c = card(1);
    expect(cardState(c, {})).toBe('available');
    expect(cardState(c, { partial: { 'm1.safetensors': 10 } })).toBe('paused');
    expect(cardState(c, { downloads: [{ version_id: 1, file: 'm1.safetensors', state: 'failed' }],
      partial: { 'm1.safetensors': 10 } })).toBe('failed');
    expect(cardState(c, { files: [{ file: 'm1.safetensors' }],
      downloads: [{ version_id: 1, file: 'm1.safetensors', state: 'done' }] })).toBe('downloaded');
    expect(cardState(c, { files: [{ file: 'm1.safetensors' }],
      downloads: [{ version_id: 1, file: 'm1.safetensors', state: 'downloading' }] })).toBe('downloading');
  });
});

describe('the filters', () => {
  const cards = [
    card(1, { downloads: 10 }),
    card(2, { downloads: 500, nsfw: true }),
    card(3, { downloads: 900, status: 'no' }),
    card(4, { downloads: 50, status: 'works', family: 'SDXL · Pony' }),
  ];

  it('hides NSFW models until they are asked for, and sinks what will not load', () => {
    expect(visibleCards(cards, {}).map((c) => c.version_id)).toEqual([4, 1, 3]);
    expect(visibleCards(cards, { nsfw: true }).map((c) => c.version_id)).toEqual([2, 4, 1, 3]);
  });

  it('narrows by family and by what this build does with it', () => {
    expect(visibleCards(cards, { family: 'SDXL · Pony' }).map((c) => c.version_id)).toEqual([4]);
    expect(visibleCards(cards, { status: 'usable' }).map((c) => c.version_id)).toEqual([4, 1]);
    expect(visibleCards(cards, { status: 'best' }).map((c) => c.version_id)).toEqual([1]);
  });

  it('"Downloaded" keeps what is on this machine, finished or not', () => {
    const local = { files: [{ file: 'm1.safetensors' }], partial: { 'm4.safetensors': 3 } };
    expect(visibleCards(cards, { downloaded: true }, local).map((c) => c.version_id)).toEqual([4, 1]);
  });

  it('names the files in the folder no card accounts for', () => {
    expect(unlistedFiles([{ file: 'm1.safetensors' }, { file: 'mine.safetensors' }], cards))
      .toEqual([{ file: 'mine.safetensors' }]);
  });
});

describe('a card', () => {
  it('shows only PG and PG-13 previews while NSFW is off', () => {
    const c = card(1, { images: [{ url: 'a', nsfw_level: 8 }, { url: 'b', nsfw_level: 2 },
      { url: 'c', nsfw_level: 1 }, { url: 'd', nsfw_level: 1 }] });
    expect(previews(c, false).map((i) => i.url)).toEqual(['b', 'c']);
    expect(previews(c, true).map((i) => i.url)).toEqual(['a', 'b']);
  });

  it('asks for support on GitHub with the model and the reason already filled in', () => {
    const url = new URL(supportIssueUrl(card(9, { status: 'no', base_model: 'Anima',
      note: 'Not the 2B Anima.' })));
    expect(url.origin + url.pathname).toBe('https://github.com/yuri-os/YuriOS/issues/new');
    expect(url.searchParams.get('title')).toBe('Checkpoint support: Model 9 (Anima)');
    expect(url.searchParams.get('body')).toContain('Not the 2B Anima.');
    expect(url.searchParams.get('body')).toContain('https://civitai.com/models/9');
  });

  it('says how far a download has got and how long is left', () => {
    expect(progressLine({ done: 1e9, total: 6e9, speed: 50e6 })).toBe('1.0 GB of 6.0 GB · 50 MB/s · 2 min left');
    expect(progressLine({ done: 1e9, total: 2e9, speed: 50e6 })).toBe('1.0 GB of 2.0 GB · 50 MB/s · 20 s left');
    expect(progressLine({ done: 2e6, total: 0, speed: 0 })).toBe('2 MB');
    expect(bytes(12.6e9)).toBe('13 GB');
    expect(ago(1000, 1000 + 7200)).toBe('2 h ago');
  });
});

describe('the route', () => {
  it('is a page of its own under Pictures', () => {
    expect(parseRoute('#/checkpoints', [{ id: 'pictures' }])).toEqual({ page: 'checkpoints', key: '' });
  });
});

describe('the search', () => {
  const cards = [
    card(1, { name: 'WAI Illustrious SDXL', creator: 'WAI0731', file: 'waiIllustriousSDXL_v170.safetensors' }),
    card(2, { name: 'AniDream TrueHentai', family: 'Anima', base_model: 'Anima', nsfw: true }),
    card(3, { name: 'Something else', queries: ['dreamy'] }),
  ];

  it('is remembered the way the server remembers it', () => {
    expect(normalizeQuery('  Hidden\t  GEM ')).toBe('hidden gem');
    expect(normalizeQuery(undefined)).toBe('');
  });

  it('matches every word anywhere on the card, or what Civitai returned for it', () => {
    expect(matchesQuery(cards[0], 'wai sdxl')).toBe(true);
    expect(matchesQuery(cards[0], 'v170')).toBe(true);           // the file name
    expect(matchesQuery(cards[0], 'wai pony')).toBe(false);
    expect(matchesQuery(cards[2], 'Dreamy')).toBe(true);         // found by that search
    expect(matchesQuery(cards[2], 'dream')).toBe(false);
    expect(matchesQuery(cards[2], '')).toBe(true);
  });

  it('narrows the list on top of the filters', () => {
    expect(visibleCards(cards, { query: 'dream' }).map((c) => c.version_id)).toEqual([]);
    expect(visibleCards(cards, { query: 'dream', nsfw: true }).map((c) => c.version_id)).toEqual([2]);
  });

  it('asks Civitai only when nothing cached answers, once per search', () => {
    expect(needsLookUp(cards, 'truehentai')).toBe(false);        // cached, even if NSFW hides it
    expect(needsLookUp(cards, 'zz')).toBe(false);                // too short to ask by itself
    expect(needsLookUp(cards, 'pony realism')).toBe(true);
    expect(needsLookUp(cards, 'Pony  Realism', new Set(['pony realism']))).toBe(false);
  });
});

describe('a version that comes as several files', () => {
  const fp16 = { file: 'iv_fp16.safetensors', size_bytes: 6.46e9, precision: 'fp16', size: 'full',
    recommended: true, loads: true, note: 'Best match: her camera runs SDXL in fp16.' };
  const fp32 = { file: 'iv_fp32.safetensors', size_bytes: 12.92e9, precision: 'fp32', size: 'full',
    recommended: false, loads: true, note: '' };
  const c = card(7, { file: fp16.file, variants: [fp16, fp32] });

  it('opens on the best match, or on the file already here, or on the one you picked', () => {
    expect(chosenVariant(c, {}).file).toBe(fp16.file);
    expect(chosenVariant(c, { files: [{ file: fp32.file }] }).file).toBe(fp32.file);
    expect(chosenVariant(c, { partial: { [fp32.file]: 9 } }).file).toBe(fp32.file);
    expect(chosenVariant(c, {}, fp32.file).file).toBe(fp32.file);
    expect(chosenVariant(c, {}, 'gone.safetensors').file).toBe(fp16.file);
  });

  it('tracks each file on its own', () => {
    const local = { downloads: [{ version_id: 7, file: fp32.file, state: 'downloading' }] };
    expect(cardState(asVariant(c, fp32), local)).toBe('downloading');
    expect(cardState(asVariant(c, fp16), local)).toBe('available');
  });

  it('counts as downloaded, and as listed, whichever file is here', () => {
    const local = { files: [{ file: fp32.file }] };
    expect(visibleCards([c], { downloaded: true }, local)).toHaveLength(1);
    expect(unlistedFiles([{ file: fp32.file }], [c])).toEqual([]);
  });

  it('labels a file by precision, size and weight', () => {
    expect(variantLabel(fp32)).toBe('fp32 · 13 GB');
    expect(variantLabel({ ...fp16, size: 'pruned' })).toBe('fp16 · pruned · 6.5 GB');
  });

  it('treats a card cached before files were listed as its one file', () => {
    expect(variantsOf(card(8, { status: 'no' }))).toMatchObject([{ file: 'm8.safetensors',
      recommended: true, loads: false }]);
  });
});
