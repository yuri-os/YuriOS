# `web/live2d/` — the Live2D web client (SPEC §6.6)

YuriOS's second body: a browser client that drives a Live2D rig instead of the VRM.
First-party code, maintained here. It talks to the same server the VRM page does —
the audio-only `/ws/voice` wire, the `/api/events` bus, `/api/history`,
`/api/config` (the rig registry, `world/routes/live2d.py`), and `/api/settings`.

Files:

- `avatar.js` — the Live2D body and the name → Live2D-parameter expression map.
- `voice.js` — the edge VAD / barge-in client (its `expression` wire case is dead
  code since expressions moved onto `/api/events`, harmless).
- `settings.js` — the settings panel.
- `index.html` — the body plus the chat column (SPEC §2.6); loads the two bus scripts.
- `sanctuary.css` — the chat-column styles + the desktop-mode hide rule.
- `events.js` — the bus adapter: boots the shared chat panel (`/js/chat.js`) and maps
  `avatar`/`expression` events from `/api/events` onto `Avatar.setExpression`. The
  Live2D body is a guest, not a second fully-driven puppet (SPEC §6.6).

## The Live2D runtime is fetched, not committed

The Cubism Core runtime and the Live2D Free-Material rigs are **third-party** and are
**not** in this repo (proprietary Cubism Core + rig licensing — B2 §8.2's rule). Fetch
them into `web/live2d/vendor/` with:

    python scripts/fetch_live2d.py

With `vendor/` empty the page runs voice-only and says so. `web/live2d/vendor/` is
gitignored — it is the one genuinely vendored (external) piece under this directory;
everything else here is ours.
