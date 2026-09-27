/** @vitest-environment jsdom */
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('../mind/graph/views.js', () => {
  const draw = (root, ws) => { root.textContent = ws.visible().map((e) => e.id).join(', '); };
  return { drawGoals: draw, drawLedger: draw, drawMap: draw,
    drawSpace: draw, drawStories: draw, drawTimeline: draw };
});

import { graphAttached, markStale, mountGraph, unmountGraph } from '../mind/graph/workspace.js';

const payload = (latest, ids) => ({
  meta: { range: [1000, latest], generated_t: latest },
  events: ids.map(([id, t]) => ({ id, t, kind: 'journal', title: id, summary: '' })),
});

afterEach(() => {
  unmountGraph();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

it('clears the stale notice and follows new records when reloading the newest range', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  const load = vi.fn()
    .mockResolvedValueOnce(payload(2000, [['old', 1900]]))
    .mockResolvedValueOnce(payload(2600, [['old', 1900], ['new', 2500]]))
    .mockResolvedValueOnce(payload(3200, [['old', 1900], ['new', 2500], ['later', 3100]]));
  const deps = { load, go: vi.fn(), toast: vi.fn() };
  const root = await mountGraph('timeline', { w: '7', r: '1000-2000' }, deps);
  document.body.append(root);
  graphAttached();

  markStale();
  expect(root.querySelector('.gx-stale').hidden).toBe(false);
  root.querySelector('[data-reload]').click();
  await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  await vi.waitFor(() => expect(root.querySelector('.gx-stale').hidden).toBe(true));
  expect(root.querySelector('.gx-view').textContent).toContain('new');
  expect(location.hash).toContain('r=1600-2600');

  // A deliberately older view stays where the reader left it.
  const older = await mountGraph('timeline', { w: '7', r: '1000-1600' }, deps);
  root.remove();
  document.body.append(older);
  graphAttached();
  markStale();
  older.querySelector('[data-reload]').click();
  await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(3));
  await vi.waitFor(() => expect(older.querySelector('.gx-stale').hidden).toBe(true));
  expect(location.hash).toContain('r=1000-1600');
  expect(older.querySelector('.gx-view').textContent).not.toContain('later');
});
