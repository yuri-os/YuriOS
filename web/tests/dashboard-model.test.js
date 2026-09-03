import { describe, expect, it } from 'vitest';

import {
  contextEntries,
  filterCharacters,
  formatDiaryDay,
  initials,
  needsUserName,
  normalizeCharacter,
  normalizeCharacters,
  normalizeDetailItems,
  normalizeUserName,
} from '../dashboard/model.js';

/* dashboard/model.js is the switchboard's whole reading of the API: every row the
 * board draws has been through normalizeCharacter first, and every defaulting
 * decision in there is a claim about what a missing field MEANS. Those claims are
 * what this file pins — a `notify` that arrives as a bare boolean, an `unread`
 * count that arrives as a string, a card that is parked for review. Getting one of
 * them backwards shows a switch that lies about the state it is in.
 *
 * No DOM: this module renders nothing, it only decides. (dashboard.js does the
 * drawing, and needs a browser to mean anything.)
 */

const row = (over = {}) => ({ id: 'yuri', name: 'Yuri', state: 'engaged', ...over });

describe('normalizeCharacter', () => {
  it('drops a row with no id — there is no character to route to', () => {
    expect(normalizeCharacter({ name: 'nameless' })).toBeNull();
    expect(normalizeCharacter(null)).toBeNull();
    expect(normalizeCharacter({ id: '   ' })).toBeNull();
  });

  it('accepts any of the three names the API has used for the id', () => {
    expect(normalizeCharacter({ slug: 'yuri' }).id).toBe('yuri');
    expect(normalizeCharacter({ character_id: 'yuri' }).id).toBe('yuri');
  });

  it('falls back to the id when there is no display name', () => {
    expect(normalizeCharacter({ id: 'yuri' }).name).toBe('yuri');
  });

  it('carries the state through as its own word', () => {
    const character = normalizeCharacter(row({ state: 'dormant' }));
    expect(character.state).toBe('dormant');
    expect(character.stateMeta.label).toBe('dormant');
  });

  describe('the doorbell', () => {
    // SPEC §18.4.6. Two shapes reach here: the board's {enabled, available},
    // and the bare boolean the settings form posts.
    it('reads the board shape', () => {
      const character = normalizeCharacter(row({ notify: { enabled: false, available: true } }));
      expect(character.notify).toEqual({ enabled: false, available: true });
    });

    it('reads the settings form shape without snapping the switch back on', () => {
      // The optimistic re-normalise on save merges a bare boolean over `raw`.
      // Read as "no answer", `enabled` would default to true and the switch
      // would flip back in front of whoever just turned it off.
      const character = normalizeCharacter(row({ notify: false, notify_available: true }));
      expect(character.notify.enabled).toBe(false);
      expect(character.notify.available).toBe(true);
    });

    it('is on by default when the payload says nothing about it', () => {
      expect(normalizeCharacter(row()).notify.enabled).toBe(true);
    });

    it('is unavailable when the house switch says nothing', () => {
      // NOTIFY_ENABLED off: the per-character toggle is shown but inert, so the
      // board never offers a switch that quietly does nothing.
      expect(normalizeCharacter(row()).notify.available).toBe(false);
    });
  });

  describe('her hands', () => {
    // SPEC §26.1. Read exactly like the doorbell, and for the same reason:
    // `available` is the house switch MIND_TOOLS_ENABLED, and with it off the
    // per-character toggle is inert rather than pretending a character can
    // grant herself a capability this node has not installed.
    it('reads the board shape', () => {
      const character = normalizeCharacter(row({ hands: { enabled: false, available: true } }));
      expect(character.hands).toEqual({ enabled: false, available: true });
      expect(character.loops.hands).toBe(true); // the loops bag is its own answer
    });

    it('reads the settings form shape without snapping the switch back on', () => {
      const character = normalizeCharacter(row({ hands: false, hands_available: true }));
      expect(character.hands.enabled).toBe(false);
      expect(character.hands.available).toBe(true);
    });

    it('falls back to the loops bag when only that arrived', () => {
      expect(normalizeCharacter(row({ loops: { hands: false } })).hands.enabled).toBe(false);
    });

    it('is unavailable when the house switch says nothing', () => {
      // MIND_TOOLS_ENABLED is off out of the box, so this is the ordinary case
      // and the switch must read as inert rather than as available-and-on.
      expect(normalizeCharacter(row()).hands.available).toBe(false);
    });
  });

  it('counts unread only when the count is a real positive number', () => {
    expect(normalizeCharacter(row({ unread: { count: '3', selfies: 1 } })).unread)
      .toMatchObject({ count: 3, selfies: 1 });
    expect(normalizeCharacter(row({ unread: { count: -2 } })).unread.count).toBe(0);
    expect(normalizeCharacter(row({ unread: { count: 'lots' } })).unread.count).toBe(0);
    expect(normalizeCharacter(row()).unread).toEqual({ count: 0, selfies: 0, latest: null });
  });

  it('keeps a card parked until someone has read it through', () => {
    expect(normalizeCharacter(row({ review_required: true })).reviewRequired).toBe(true);
    expect(normalizeCharacter(row()).reviewRequired).toBe(false);
  });

  it('gives each row an accent even when the payload has none', () => {
    expect(normalizeCharacter(row(), 0).accent).toMatch(/^#[0-9a-f]{6}$/i);
    expect(normalizeCharacter(row(), 0).accent).not.toBe(normalizeCharacter(row(), 1).accent);
    expect(normalizeCharacter(row({ accent: '#ffffff' })).accent).toBe('#ffffff');
  });

  it('keeps the untouched payload on the row', () => {
    const raw = row({ anything: 'the board does not read yet' });
    expect(normalizeCharacter(raw).raw).toBe(raw);
  });
});

describe('normalizeCharacters', () => {
  it('takes either a bare array or a {characters: [...]} envelope', () => {
    expect(normalizeCharacters([row()])).toHaveLength(1);
    expect(normalizeCharacters({ characters: [row()] })).toHaveLength(1);
  });

  it('refuses a payload with no character list rather than drawing an empty board', () => {
    expect(() => normalizeCharacters({ error: 'nope' })).toThrow(TypeError);
  });

  it('sorts the present above the absent, then by name', () => {
    const sorted = normalizeCharacters([
      { id: 'c', name: 'Cass', state: 'offline' },
      { id: 'b', name: 'Bex', state: 'engaged' },
      { id: 'a', name: 'Ada', state: 'engaged' },
    ]);
    expect(sorted.map((c) => c.id)).toEqual(['a', 'b', 'c']);
  });

  it('skips rows it cannot route to instead of failing the whole board', () => {
    expect(normalizeCharacters([row(), { name: 'nameless' }])).toHaveLength(1);
  });
});

describe('filterCharacters', () => {
  const characters = normalizeCharacters([
    { id: 'yuri', name: 'Yuri', state: 'engaged', model: 'lm_studio/qwen' },
    { id: 'ada', name: 'Ada', state: 'dormant', description: 'the quiet one' },
  ]);

  it('returns everything for an empty query', () => {
    expect(filterCharacters(characters, '  ')).toBe(characters);
    expect(filterCharacters(characters, undefined)).toBe(characters);
  });

  it('searches name, id, state, description and model alike', () => {
    expect(filterCharacters(characters, 'YURI').map((c) => c.id)).toEqual(['yuri']);
    expect(filterCharacters(characters, 'dormant').map((c) => c.id)).toEqual(['ada']);
    expect(filterCharacters(characters, 'quiet').map((c) => c.id)).toEqual(['ada']);
    expect(filterCharacters(characters, 'qwen').map((c) => c.id)).toEqual(['yuri']);
  });
});

describe('initials', () => {
  it('takes the first and last initial of a full name', () => {
    expect(initials('Yuri Nakamura')).toBe('YN');
  });

  it('takes two letters of a single name', () => {
    expect(initials('Yuri')).toBe('YU');
  });

  it('has something to draw even with nothing to draw it from', () => {
    expect(initials('')).toBe('?');
    expect(initials(null)).toBe('?');
  });
});

describe('normalizeDetailItems', () => {
  // /log interleaves two raw row shapes with no shared vocabulary: tick traces
  // (ISO ts) and tool-call audits (epoch seconds).
  it('reads a tool-call audit', () => {
    const [item] = normalizeDetailItems([
      { tool: 'web_fetch', verdict: 'ok', result: '{"title": "a page"}', ts: 1700000000 },
    ]);
    expect(item.title).toBe('web_fetch');
    expect(item.body).toBe('title: a page');
    expect(item.time).toBe(new Date(1700000000 * 1000).toISOString());
  });

  it('says so in the title when a tool call did not go through', () => {
    const [item] = normalizeDetailItems([{ tool: 'rm_rf', verdict: 'denied', result: '' }]);
    expect(item.title).toBe('rm_rf — denied');
    expect(item.tone).toBe('denied');
  });

  it('renders a result the audit cut off mid-string as readable lines', () => {
    // brain.py's _execute JSON-serialises the dict, then the audit's 200-char cap
    // truncates it — so it usually arrives as invalid, unterminated JSON.
    const [item] = normalizeDetailItems([
      { tool: 'web_search', result: '{"query": "rain", "first": "a very long resul' },
    ]);
    expect(item.body).toBe('query: rain\nfirst: a very long resul…');
  });

  it('reads a tick trace', () => {
    const [item] = normalizeDetailItems([
      { ts: '2026-08-14T10:00:00Z', activity_state: 'IDLE',
        decided: { intention: 'REST' }, acted: { what: 'rest', result: 'rested' } },
    ]);
    expect(item).toMatchObject({ title: 'REST', body: 'rested', time: '2026-08-14T10:00:00Z' });
  });

  it('falls back to the activity state when a tick decided nothing', () => {
    const [item] = normalizeDetailItems([{ ts: '2026-08-14T10:00:00Z', activity_state: 'DORMANT' }]);
    expect(item.title).toBe('DORMANT');
  });

  it('folds an idle stretch into one line instead of one line per tick', () => {
    const tick = (ts) => ({ ts, activity_state: 'IDLE', decided: { intention: 'REST' },
                            acted: { what: 'rest', result: 'rested' } });
    const items = normalizeDetailItems([tick('t1'), tick('t2'), tick('t3')]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ count: 3, time: 't1', timeEnd: 't3' });
  });

  it('keeps two different ticks apart', () => {
    const items = normalizeDetailItems([
      { ts: 't1', decided: { intention: 'REST' } },
      { ts: 't2', decided: { intention: 'REACH OUT' } },
    ]);
    expect(items.map((i) => i.title)).toEqual(['REST', 'REACH OUT']);
  });

  it('takes any of the envelopes /log has been served under', () => {
    for (const key of ['entries', 'logs', 'events', 'items']) {
      expect(normalizeDetailItems({ [key]: [{ tool: 'x' }] })).toHaveLength(1);
    }
    expect(normalizeDetailItems({ nothing: 'here' })).toEqual([]);
  });
});

describe('formatDiaryDay', () => {
  it('lands on the local calendar day, not UTC midnight', () => {
    // `new Date("2026-08-14")` is UTC midnight, which prints as the 13th anywhere
    // west of Greenwich. Parsed with explicit y/m/d args it cannot drift.
    const day = formatDiaryDay('2026-08-14', { day: 'numeric', month: 'numeric' });
    expect(day).toContain('14');
  });

  it('hands back anything that is not a date, untouched', () => {
    expect(formatDiaryDay('not-a-day')).toBe('not-a-day');
    expect(formatDiaryDay(undefined)).toBe('');
  });
});

describe('contextEntries', () => {
  it('reads through a {context: {...}} envelope or a bare object', () => {
    expect(contextEntries({ context: { turn_count: 3 } })).toEqual([{ key: 'turn count', value: '3' }]);
    expect(contextEntries({ turn_count: 3 })).toEqual([{ key: 'turn count', value: '3' }]);
  });

  it('pretty-prints a nested value rather than printing [object Object]', () => {
    const [entry] = contextEntries({ window: { used: 10 } });
    expect(entry.value).toBe(JSON.stringify({ used: 10 }, null, 2));
  });

  it('has nothing to show for a payload that is not an object', () => {
    expect(contextEntries(null)).toEqual([]);
    expect(contextEntries([1, 2])).toEqual([]);
  });
});

describe('needsUserName', () => {
  it('asks when USER_NAME is the default pronoun that collides with her You', () => {
    expect(needsUserName('you')).toBe(true);
    expect(needsUserName('You')).toBe(true);
    expect(needsUserName('')).toBe(true);
    expect(needsUserName('the user')).toBe(true);
    expect(needsUserName(undefined)).toBe(true);
  });

  it('does not ask when a real name is already set', () => {
    expect(needsUserName('Alex')).toBe(false);
    expect(needsUserName('Sam')).toBe(false);
  });
});

describe('normalizeUserName', () => {
  it('keeps a real name and refuses the colliding pronouns', () => {
    expect(normalizeUserName('  Alex  ')).toBe('Alex');
    expect(normalizeUserName('you')).toBe('');
    expect(normalizeUserName('the user')).toBe('');
    expect(normalizeUserName('')).toBe('');
  });
});
