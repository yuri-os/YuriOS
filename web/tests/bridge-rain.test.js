// The rain is off unless the house sends weather (SPEC §6.2): the bridge hands
// every `rain` command to the room, the sound bed and the page's chrome.
import { expect, it, vi } from 'vitest';
import { ControlBridge } from '../js/bridge.js';

it('passes a rain command to the room, the bed and the page', () => {
  const room = { setRain: vi.fn() };
  const music = { setRainLevel: vi.fn() };
  const onRain = vi.fn();
  new ControlBridge({}, { room, music, onRain }).dispatch({ op: 'rain', intensity: 0.4 });
  expect(room.setRain).toHaveBeenCalledWith(0.4);
  expect(music.setRainLevel).toHaveBeenCalledWith(0.4);
  expect(onRain).toHaveBeenCalledWith(0.4);
});

it('needs no page hook at all', () => {
  expect(() => new ControlBridge({}, {}).dispatch({ op: 'rain', intensity: 0 })).not.toThrow();
});
