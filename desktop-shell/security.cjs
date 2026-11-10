/* The desktop frame grants only the selected room's main-frame microphone.
 * SPEC §6.5: a web page in a native frame never gains a second trust boundary. */
function roomURL(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('The desktop room must be an HTTP(S) URL without credentials');
  }
  return url;
}

function sameOrigin(value, origin) {
  try { return roomURL(value).origin === origin; } catch { return false; }
}

function guardWindow(win, selectedURL) {
  const origin = roomURL(selectedURL).origin;
  const wc = win.webContents;
  const frameAllowed = (sender, details) => sender === wc &&
    details?.isMainFrame === true && sameOrigin(details.requestingUrl, origin) &&
    sameOrigin(wc.getURL(), origin);
  wc.session.setPermissionRequestHandler((sender, permission, done, details) => {
    done(permission === 'media' && frameAllowed(sender, details) &&
      Array.isArray(details.mediaTypes) && details.mediaTypes.length > 0 &&
      details.mediaTypes.every(type => type === 'audio'));
  });
  wc.session.setPermissionCheckHandler((sender, permission, requestingOrigin, details) =>
    permission === 'media' && frameAllowed(sender, details) &&
    sameOrigin(requestingOrigin, origin) && details.mediaType === 'audio');
  wc.session.setDisplayMediaRequestHandler((_request, done) => done({}));
  wc.setWindowOpenHandler(() => ({ action: 'deny' }));
  const navigate = (event, target) => {
    if (!sameOrigin(typeof target === 'string' ? target : event.url, origin)) event.preventDefault();
  };
  wc.on('will-navigate', navigate);
  wc.on('will-redirect', navigate);
  wc.on('will-frame-navigate', navigate);
}

module.exports = { guardWindow, roomURL, sameOrigin };
