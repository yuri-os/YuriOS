/* The one fetch wrapper's error text (web/shared/http.js). A FastAPI validation
 * failure answers `{"detail": [{loc, msg, …}]}` — a list — and the mind page's
 * dream box rendered it as "[object Object]". */
import { afterEach, expect, it, vi } from 'vitest';

import { ApiError, detailMessage, request } from '../shared/http.js';

afterEach(() => vi.unstubAllGlobals());

it('reads every shape the server answers with', () => {
  expect(detailMessage({ detail: 'unknown connection profile' })).toBe('unknown connection profile');
  expect(detailMessage({ error: 'no such character' })).toBe('no such character');
  expect(detailMessage('Bad Gateway')).toBe('Bad Gateway');
  expect(detailMessage({ detail: [{
    type: 'value_error', loc: ['body', 'day'],
    msg: 'Value error, day must be a canonical YYYY-MM-DD date', input: '2099-13-45',
  }] })).toBe('day must be a canonical YYYY-MM-DD date');
  expect(detailMessage({ detail: [
    { loc: ['body', 'job'], msg: 'String should match pattern' },
    { loc: ['query'], msg: 'Field required' },
  ] })).toBe('job: String should match pattern; query: Field required');
  expect(detailMessage({}, 'fallback')).toBe('fallback');
  expect(detailMessage(null, 'fallback')).toBe('fallback');
});

it('never throws an error whose message is an object', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: false, status: 422,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => ({ detail: [{ loc: ['body', 'day'], msg: 'bad day' }] }),
  })));
  const error = await request('/api/mind/dream/run', { method: 'POST', body: '{}' }).catch((e) => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(422);
  expect(error.message).toBe('day: bad day');
});
