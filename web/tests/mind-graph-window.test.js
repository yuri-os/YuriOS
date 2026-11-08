/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('../mind/graph/views.js', () => {
  const draw = (root, ws) => { root.textContent = ws.visible().map((e) => e.id).join(', '); };
  return { drawGoals: draw, drawLedger: draw, drawMap: draw,
    drawSpace: draw, drawStories: draw, drawTimeline: draw };
});

let mountGraph, graphAttached, unmountGraph;
const payload = {
  meta: { range: [1000, 2000], generated_t: 2000 },
  events: [{ id: 'recent', t: 1900, kind: 'journal', title: 'recent', summary: '' }],
};

beforeEach(async () => {
  vi.resetModules();
  ({ mountGraph, graphAttached, unmountGraph } = await import('../mind/graph/workspace.js'));
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  history.replaceState(null, '', '/');
});

afterEach(() => {
  unmountGraph();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function mount(params = {}) {
  const deps = { load: vi.fn().mockResolvedValue(payload), go: vi.fn(), toast: vi.fn() };
  const root = await mountGraph('timeline', params, deps);
  document.body.append(root);
  graphAttached();
  return { root, ...deps };
}

it('loads only a week when opening the timeline without a window', async () => {
  const { root, load } = await mount();
  expect(load.mock.calls).toEqual([[7]]);
  expect(root.querySelector('[data-days="7"]').classList.contains('on')).toBe(true);
  expect(location.hash).toContain('w=7');
});

it('keeps the default week when a linked record is missing', async () => {
  const { load, toast } = await mount({ sel: 'selfie-dream-2026-11-05' });
  expect(load.mock.calls).toEqual([[7]]);
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('outside the loaded window'), 'success');
  expect(location.hash).toContain('w=7');
  expect(location.hash).not.toContain('sel=');
});

it('preserves an explicitly linked window when its selection is missing', async () => {
  const { load } = await mount({ w: '1', sel: 'older' });
  expect(load.mock.calls).toEqual([[1]]);
  expect(location.hash).toContain('w=1');
});

it('loads all history when the link explicitly requests it', async () => {
  const { root, load } = await mount({ w: '0', sel: 'recent' });
  expect(load.mock.calls).toEqual([[0]]);
  expect(root.querySelector('[data-days="0"]').classList.contains('on')).toBe(true);
  expect(location.hash).toContain('w=0');
  expect(location.hash).toContain('sel=recent');
});

it('loads all history when the reader chooses All', async () => {
  const { root, load } = await mount();
  root.querySelector('[data-days="0"]').click();
  await vi.waitFor(() => expect(location.hash).toContain('w=0'));
  expect(load.mock.calls).toEqual([[7], [0]]);
});
