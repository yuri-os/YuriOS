// A body swapped in the switchboard arrives as `load_model` (SPEC §6.8): the new
// skeleton has an empty mixer, so the page's hook puts her idle back on — only
// once the model has actually loaded, and never for one that failed.
import { expect, it, vi } from 'vitest';
import { ControlBridge } from '../js/bridge.js';

it('puts her idle back on after the new body has loaded', async () => {
  const stage = { loadModel: vi.fn(() => Promise.resolve()) };
  const onModel = vi.fn();
  new ControlBridge(stage, { onModel }).dispatch({ op: 'load_model', url: '/b.vrm' });
  expect(stage.loadModel).toHaveBeenCalledWith('/b.vrm');
  await vi.waitFor(() => expect(onModel).toHaveBeenCalledOnce());
});

it('does not reach for the idle when the body failed to load', async () => {
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  const stage = { loadModel: vi.fn(() => Promise.reject(new Error('bad file'))) };
  const onModel = vi.fn();
  new ControlBridge(stage, { onModel }).dispatch({ op: 'load_model', url: '/b.vrm' });
  await vi.waitFor(() => expect(error).toHaveBeenCalled());
  expect(onModel).not.toHaveBeenCalled();
  error.mockRestore();
});
