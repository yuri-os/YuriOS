/* Shared URL and session-key rules for sanctuary clients. This stays a classic
 * script so raw clients can load it directly while Vite can also bundle it. */
(() => {
  if (window.YuriOSRuntime) return;

  // Every per-character page the host routes (world/host.py): the 3D sanctuary,
  // the bodyless text room, the Live2D client's redirect target, and the mind
  // debug page. A page that is not in this list falls back to `?character=`, and
  // with neither it speaks to the primary runtime — which is exactly right for
  // the single-character app.
  const routeMatch = location.pathname.match(
    /^\/characters\/([^/]+)\/(?:sanctuary|text|live2d|mind)(?:\/|$)/);
  let characterId = null;
  if (routeMatch) {
    try { characterId = decodeURIComponent(routeMatch[1]); }
    catch (_) { characterId = routeMatch[1]; }
  } else {
    characterId = new URLSearchParams(location.search).get('character') || null;
  }

  const encodedId = characterId == null ? null : encodeURIComponent(characterId);

  function scopedPath(path, root) {
    if (!encodedId || typeof path !== 'string') return path;
    if (path !== root && !path.startsWith(root + '/') &&
        !path.startsWith(root + '?') && !path.startsWith(root + '#')) return path;
    return `${root}/characters/${encodedId}${path.slice(root.length)}`;
  }

  function apiPath(path) {
    return scopedPath(path, '/api');
  }

  function httpPath(path) {
    if (encodedId && typeof path === 'string' && path.startsWith('/selfies/')) {
      return `/api/characters/${encodedId}${path}`;
    }
    return apiPath(path);
  }

  function wsPath(path) {
    return scopedPath(path, '/ws');
  }

  function wsUrl(path) {
    const protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
    return protocol + location.host + wsPath(path);
  }

  function sessionKey(base = 'yuri.session') {
    return characterId == null ? base : `${base}.${characterId}`;
  }

  window.YuriOSRuntime = Object.freeze({
    characterId,
    apiPath,
    httpPath,
    wsPath,
    wsUrl,
    sessionKey,
  });
})();
