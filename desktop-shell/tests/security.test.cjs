const { test } = require('node:test');
const assert = require('node:assert/strict');
const { guardWindow, roomURL } = require('../security.cjs');

function guarded(url = 'http://127.0.0.1:8768/characters/yuri/sanctuary/') {
  const handlers = {};
  const wc = {
    getURL: () => url,
    session: {
      setPermissionRequestHandler(fn) { handlers.request = fn; },
      setPermissionCheckHandler(fn) { handlers.check = fn; },
      setDisplayMediaRequestHandler(fn) { handlers.display = fn; },
    },
    on(event, fn) { handlers[event] = fn; },
    setWindowOpenHandler(fn) { handlers.popup = fn; },
  };
  guardWindow({ webContents: wc }, url);
  const details = { isMainFrame: true, requestingUrl: url, mediaTypes: ['audio'], mediaType: 'audio' };
  return { wc, handlers, details, origin: new URL(url).origin };
}

test('allows only the selected room main-frame microphone in both permission phases', () => {
  for (const url of ['http://127.0.0.1:8768/', 'http://172.20.2.4:8768/', 'https://room.example/']) {
    const { wc, handlers, details, origin } = guarded(url);
    let granted;
    handlers.request(wc, 'media', value => { granted = value; }, details);
    assert.equal(granted, true);
    assert.equal(handlers.check(wc, 'media', origin, details), true);
  }
});

test('denies camera, combined capture, unknown capture and missing details', () => {
  const { wc, handlers, details, origin } = guarded();
  for (const mediaTypes of [['video'], ['audio', 'video'], ['unknown'], [], undefined]) {
    let granted;
    handlers.request(wc, 'media', value => { granted = value; }, { ...details, mediaTypes });
    assert.equal(granted, false);
  }
  for (const mediaType of ['video', 'unknown', undefined]) {
    assert.equal(handlers.check(wc, 'media', origin, { ...details, mediaType }), false);
  }
  handlers.request(wc, 'media', value => assert.equal(value, false));
});

test('denies foreign origins, ports, frames, service workers and web contents', () => {
  const { wc, handlers, details, origin } = guarded();
  for (const patch of [{ requestingUrl: 'https://evil.example/' },
    { requestingUrl: 'http://127.0.0.1:9999/' }, { isMainFrame: false },
    { requestingUrl: undefined }]) {
    handlers.request(wc, 'media', value => assert.equal(value, false), { ...details, ...patch });
    assert.equal(handlers.check(wc, 'media', origin, { ...details, ...patch }), false);
  }
  handlers.request({}, 'media', value => assert.equal(value, false), details);
  assert.equal(handlers.check(null, 'media', origin, details), false);
  assert.equal(handlers.check(wc, 'media', 'https://evil.example', details), false);
  for (const permission of ['display-capture', 'geolocation', 'notifications', 'hid']) {
    handlers.request(wc, permission, value => assert.equal(value, false), details);
    assert.equal(handlers.check(wc, permission, origin, details), false);
  }
  handlers.display({}, result => assert.deepEqual(result, {}));
});

test('allows local room and login navigation, blocks foreign navigation and popups', () => {
  const { handlers, origin } = guarded();
  for (const eventName of ['will-navigate', 'will-redirect', 'will-frame-navigate']) {
    for (const url of [origin + '/auth?next=%2F', origin + '/text/']) {
      handlers[eventName]({ url, preventDefault() { assert.fail('same origin blocked'); } });
      handlers[eventName]({ preventDefault() { assert.fail('legacy navigation blocked'); } }, url);
    }
    for (const url of ['https://evil.example/', 'file:///etc/passwd', 'javascript:alert(1)']) {
      let prevented = false;
      handlers[eventName]({ url, preventDefault() { prevented = true; } });
      assert.equal(prevented, true);
    }
  }
  assert.deepEqual(handlers.popup({ url: origin + '/text/' }), { action: 'deny' });
});

test('refuses unsafe initial URL schemes and embedded credentials', () => {
  for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'http://user:secret@host/']) {
    assert.throws(() => roomURL(url));
  }
});
