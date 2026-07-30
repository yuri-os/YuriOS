# HTTP & event API

One process, one origin, port **8768**. Everything is same-origin JSON unless noted.

The topology is deliberately small: **one outbound event bus** carrying every host→frontend event
as typed JSON, and **one websocket** for audio, because sound is the only flow that's
bidirectional, binary and latency-critical. Everything else is a broadcastable fact, and facts
ride the bus.

## Two address spaces

The host owns `/` and the registry; each character's runtime owns the Part I routes. A character's
routes are reachable two ways:

| Form | Example |
|---|---|
| Per character | `GET /api/characters/yuri/health` |
| Via the **primary** character | `GET /api/health` |

The primary is the first enabled autostart character (else the first to start). The unprefixed
form is what keeps the single-companion install, the terminal client and the desktop window
working with no character in the URL. With nothing running it answers `503`; a request for a
character that isn't running answers `404`.

Websockets follow the same shape: `/ws/voice` and `/ws/characters/<id>/voice`.

## Host routes

| Route | |
|---|---|
| `GET /` | the character switchboard (`?desktop=…` redirects to the primary's sanctuary) |
| `GET /characters/{id}/sanctuary/` | her VRM page |
| `GET /characters/{id}/live2d` | redirects to `/live2d/?character={id}` |
| `GET /api/characters` | `{version, primary, characters: […]}` |
| `GET /api/connections` | named connection profiles; `secret_configured` says whether the key env var is set |
| `POST /api/characters/import` | `multipart/form-data`, the card PNG in `file` |
| `GET /api/characters/{id}/profile` | `{settings: {…}}` |
| `PATCH /api/characters/{id}/profile` | edit; also accepts the review |
| `PATCH /api/characters/{id}/loop` | `{"enabled": bool}` — her mind, live |
| `PATCH /api/characters/{id}/controls` | `{"mind"?, "utility"?, "dream"?}` |
| `GET /api/characters/{id}/portrait` | PNG, `Cache-Control: no-cache` |
| `GET /api/characters/{id}/export` | her V2+V3 card PNG, as a download |
| `GET /api/characters/{id}/selfies/{name}` | one saved photo |
| `GET /api/characters/{id}/journal?days=` | `{days: [{day, entries}]}` |
| `GET /api/characters/{id}/log` | the tail of her tick trace + tool audit |
| `GET /api/characters/{id}/context-history` | `{context, history}` |
| `POST /api/characters/{id}/archive` | stop + move her root to `data/archives/` |
| `DELETE /api/characters/{id}/purge?confirm=` | delete; `confirm` must match her id or name |

A character summary looks like:

```json
{
  "id": "yuri", "name": "Yuri", "description": "…", "creator": "", "tags": [],
  "state": "engaged", "runtime_state": "ready", "error": null,
  "enabled": true, "review_required": false, "loop_enabled": true,
  "loops": {"mind": true, "utility": true, "dream": true},
  "model": "lm_studio/…", "voice": "kokoro", "connection_profile": "default",
  "body_backend": "vrm", "body_model": "",
  "portrait_url": "/api/characters/yuri/portrait",
  "context": {"used": 4211, "limit": 32768, "exact": true},
  "activity": "engaged"
}
```

Errors use a non-2xx status with `detail` in the body.

## Runtime routes

### Conversation

| Route | |
|---|---|
| `POST /api/chat` | `{text, session_id?, channel?}` → `{session_id, message}`. Mirrors the voice route's contract minus the audio; a failed turn leaves no trace. Never waits on the voice warm-up |
| `GET /api/history` | the last 100 chat entries, for backfilling a fresh page |

Text turns from all channels serialise on one lock.

### The event bus

`GET /api/events` — Server-Sent Events, one `data:` line per event.

On attach you get `hello`, then a replay of sticky appearance state (last-write-wins), then live
events. The stream pings while idle and ends itself on shutdown, so an open tab never holds Ctrl+C
hostage. **Attaching counts as presence**: it posts `user_present` to the mind, and the last
detach posts `user_absent`.

| Event | Payload |
|---|---|
| `hello` | her name |
| `message` | a chat entry — including `image_url` selfies and the originating `channel` |
| `draft` / `draft_cancel` | streaming sentence drafts |
| `avatar` | expression, gaze, posture, visemes, `rain`, `music` — the puppet lane |
| `journal` | a new `[she]` journal line |
| `mind` | activity/budget/goal updates for the inner-life tab |

Publishes are non-blocking (a stalled client loses events, never blocks the publisher) and
thread-safe.

### Audio

`WS /ws/voice` — the audio-only socket.

- **Up:** binary mic PCM, plus `hello` / `endpoint` / `bargein` / `text` control frames.
- **Down:** `session`, `filler` / `audio` (base64 PCM plus the sentence text, for visemes),
  `done`, `cancelled`, `error`.

Turn expressions are re-routed onto the event bus, so the face has exactly one lane.

### Health and boot

| Route | |
|---|---|
| `GET /api/health` | what's actually wired: character, channels, voice (ready/stt/tts/vad), tools, mind, activity, selfies, viewers, context |
| `GET /api/boot` | the startup board the enter gate polls: each service pending → loading → ready/failed/skipped, with timings |
| `GET /api/context` | `{used, limit, exact}` — prompt tokens against the window |

Backends degrade rather than fail, so `/api/health` is where "why is she silent / why won't she
set a timer?" gets answered without reading logs.

### The mind

| Route | |
|---|---|
| `GET /api/mind` | activity state, cadence, budget, goals, shelf, pending self-edits |
| `GET /api/mind/journal?days=` | her `[she]` lines by day (max 30) |
| `GET /api/mind/trace?n=` | the tick-trace tail (max 200) |
| `POST /api/mind/edits/{id}` | `{"approve": bool}` — queued as a signal the loop consumes next tick |

All of it reads *through* the mind's own stores, so the dashboard can never disagree with the
files. With the mind off, these answer `503` and `/api/health` says so.

### Channels

| Route | |
|---|---|
| `GET /api/channels/telegram/sending` | `{configured, sending_enabled}` |
| `POST /api/channels/telegram/sending` | `{"enabled": bool}` — gates outbound only; she keeps reading |

### Settings

| Route | |
|---|---|
| `GET /api/settings` | the schema-driven form and current values |
| `POST /api/settings` | surgical `.env` write of changed fields only |
| `GET /api/models?provider=` | what a provider can serve right now: `lm_studio` · `ollama` · `openrouter`. A failure returns an empty list plus an `error` string, never a 500 |

**Loopback callers only** — it reads and writes secrets. Non-loopback gets `403`.

### Assets

| Route | |
|---|---|
| `GET /selfies/{name}` | one saved photo from the runtime's `SELFIE_DIR` |
| `GET /api/config` | the Live2D rig registry: `{avatar_model, avatar_model_url, avatar_available}` |
| `/assets/`, `/dashboard/`, `/live2d/`, `/models/`, `/shared/`, `/js/` | static |

## Notes for client authors

- Render off `/api/events`; never poll. Filter your own echoes with `message.channel`.
- Send turns to `POST /api/chat` with your own `channel` string.
- Holding the SSE stream open makes her think you're present. That's usually what you want; a
  channel that is *reachable but not present* (Telegram) deliberately doesn't.
- Her proactive lines arrive as ordinary `message` events flagged `proactive`, so initiative comes
  free with the bus.
