import { describe, expect, it } from 'vitest';

import { inspectHtml, tickSections } from '../mind/graph/inspector.js';
import { layout } from '../mind/graph/layout.js';
import { createModel, niceTimeSteps, startOfDay } from '../mind/graph/model.js';

/* The joined views of the mind debug page (SPEC §24.4).
 *
 * The server does the joining; these hold the three places the page itself
 * decides something: what counts as joined to a record, where a record sits in
 * Space, and when the inspector says the records disagree or a link is only
 * inferred. The drawing is left alone — a canvas test asserts only a mock.
 */

const ev = (id, kind, t, extra = {}) => ({ id, kind, t, title: id, summary: '', lane: kind, ...extra });

function sample() {
  return createModel({
    meta: { act_threshold: 0.4, range: [1000, 2000] },
    events: [
      ev('t-1', 'tick', 1500, { tick_id: 't-1', detail: { acted: { what: 'tool_step', tool: 'research', result: 'ok (ok)' } } }),
      ev('pr-1', 'prompt', 1490, { tick_id: 't-1', corr_id: 'c-1' }),
      ev('call-1', 'tool', 1495, { tick_id: 't-1', tool: 'research', verdict: 'error', detail: { tool: 'research', verdict: 'error', result: '', duration_ms: 12000 } }),
      ev('shot-1', 'selfie', 1600, { corr_id: 'c-1' }),
      ev('j-1', 'journal', 1520),
      ev('lonely', 'signal', 1100),
    ],
    links: [
      { a: 't-1', b: 'j-1', rel: 'wrote', confidence: 'inferred', reason: 'Nearest tick around the minute' },
    ],
    stories: [{ id: 'story-t-1', kind: 'tick', anchor: 't-1', nodes: ['t-1', 'pr-1', 'call-1', 'j-1'], why: 'because' }],
  });
}

describe('what counts as joined', () => {
  it('gathers a tick, its calls, its corr_id and its links, and nothing unrelated', () => {
    const joined = sample().clusterOf('pr-1');
    expect([...joined.keys()].sort()).toEqual(['call-1', 'j-1', 'pr-1', 'shot-1', 't-1']);
    expect(joined.get('shot-1')).toBe('corr');
    expect(joined.has('lonely')).toBe(false);
  });

  it('reads a record in the story it was opened from, else the one it anchors', () => {
    const model = sample();
    expect(model.pickStory('j-1')?.id).toBe('story-t-1');
    expect(model.pickStory('lonely')).toBeNull();
  });

  it('answers a range from the sorted list, inclusive at both ends', () => {
    expect(sample().inRange(1490, 1500).map((e) => e.id)).toEqual(['pr-1', 'call-1', 't-1']);
  });
});

describe('the inspector', () => {
  it('says the records disagree when a tick calls a failed step ok', () => {
    const model = sample();
    const { html } = inspectHtml(model.byId.get('call-1'), model);
    expect(html).toContain('records disagree');
    expect(html).toContain('most likely timed out');
  });

  it('labels an inferred join as inferred, with its reason', () => {
    const model = sample();
    const { html, why } = inspectHtml(model.byId.get('t-1'), model);
    expect(html).toMatch(/Inferred: Nearest tick around the minute/);
    expect(why).toBe('because');
  });

  it('draws the appraisal against her own gate-1 line and escapes what she wrote', () => {
    const html = tickSections({ id: 't' }, {
      sensed: [], intention: '<b>goal</b>', runners_up: [], acted: {}, hands: {}, interrupt: {},
      appraised: [{ what: '<b>goal</b>', score: 0.7, why: '' }],
    }, { threshold: 0.55 });
    expect(html).toContain('Gate 1 at 0.55');
    expect(html).toMatch(/left:55(\.0*\d*)?%/);
    expect(html).not.toContain('<b>goal</b>');
  });
});

describe('the time grid', () => {
  it('steps on local midnight, so a day line means the start of a day here', () => {
    const t0 = startOfDay(Date.UTC(2026, 7, 1) / 1000) + 3600;
    const { ticks, step } = niceTimeSteps(t0, t0 + 10 * 86400, 600);
    expect(step).toBeGreaterThanOrEqual(86400);
    for (const t of ticks) expect(startOfDay(t)).toBe(t);
  });
});

describe('Space layout', () => {
  const events = Array.from({ length: 12 }, (_, i) =>
    ({ id: `node-${i}`, kind: i < 6 ? 'tick' : 'chat', title: `Record ${i}`, t: 1000 + i * 600 + (i % 3) * 7 }));
  const links = [
    { a: 'node-0', b: 'node-1', rel: 'tick', confidence: 'explicit' },
    { a: 'node-1', b: 'node-2', rel: 'corr', confidence: 'explicit' },
    { a: 'node-2', b: 'node-3', rel: 'said', confidence: 'inferred' },
    { a: 'node-3', b: 'node-4', rel: 'goal', confidence: 'explicit' },
    { a: 'node-0', b: 'missing', rel: 'corr' },
  ];
  const result = layout(events, links);
  const byId = new Map(result.nodes.map((n) => [n.event.id, n]));

  it('draws only real links between loaded records', () => {
    expect(result.nodes).toHaveLength(events.length);
    expect(result.edges).toHaveLength(4);
  });

  it('groups by recorded work identity, and never by an inferred cause', () => {
    expect(byId.get('node-0').group).toBe(byId.get('node-2').group);
    expect(byId.get('node-2').group).not.toBe(byId.get('node-3').group);
    expect(byId.get('node-3').group).not.toBe(byId.get('node-4').group);
    expect(result.groups.some((g) => g.unlinked && g.events.length === 6)).toBe(true);
  });

  it('is the same layout whatever order the records arrive in', () => {
    expect(JSON.stringify(layout([...events].reverse(), links))).toBe(JSON.stringify(result));
  });

  it('puts time on X and each record at its own moment', () => {
    const byTime = [...result.nodes].sort((a, b) => a.event.t - b.event.t);
    for (let i = 1; i < byTime.length; i++) expect(byTime[i].p[0]).toBeGreaterThan(byTime[i - 1].p[0]);
    const ranged = layout(events, links, [0, 20000]);
    expect(ranged.xOf(0)).toBe(-ranged.xOf(20000));
    for (const n of ranged.nodes) expect(Math.abs(n.p[0] - ranged.xOf(n.event.t))).toBeLessThan(1e-9);
  });

  it('fills a volume rather than a tilted plane', () => {
    const origin = result.nodes[0].p;
    const vectors = result.nodes.slice(1).map((n) => n.p.map((v, k) => v - origin[k]));
    const det = (a, b, c) => a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0]);
    expect(vectors.some((a) => vectors.some((b) => vectors.some((c) => Math.abs(det(a, b, c)) > 1)))).toBe(true);
  });

  it('handles an empty window', () => {
    expect(layout([], []).nodes).toHaveLength(0);
  });
});
