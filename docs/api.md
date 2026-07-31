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
| `GET /studio/` | the card studio — `?character={id}` edits her, no parameter creates one |
| `GET /api/characters` | `{version, primary, characters: […]}` |
| `GET /api/connections` | named connection profiles; `secret_configured` says whether the key env var is set |
| `POST /api/characters/import` | `multipart/form-data`, the card PNG in `file` |
| `POST /api/characters` | create from a studio draft: `{draft, portrait?, character_id?}` → 201 |
| `GET /api/studio/template` | `{draft, sections, constitution_fields}` — a starting draft |
| `GET /api/characters/{id}/studio` | `{draft, provenance, grown, images, sections}` |
| `PATCH /api/characters/{id}/studio` | save a draft into the SOUL; one commit; restarts her |
| `POST /api/characters/{id}/studio/preview` | the card, report and privacy pane — no file |
| `POST /api/characters/{id}/export` | the card PNG, with export options in the body |
| `GET /api/characters/{id}/selfies` | `{selfies: [{name, url, bytes, taken_at}]}` |
| `POST /api/characters/{id}/portrait` | adopt a face: `{selfie}` or `{image}` (base64) |
| `GET /api/characters/{id}/profile` | `{settings: {…}}` |
| `PATCH /api/characters/{id}/profile` | edit; also accepts the review |
| `POST /api/characters/{id}/approve` | accept the review and start her: `{character, started, error}` |
| `PATCH /api/characters/{id}/loop` | `{"enabled": bool}` — her mind, live |
| `PATCH /api/characters/{id}/controls` | `{"mind"?, "utility"?, "dream"?}` |
| `GET /api/characters/{id}/portrait` | PNG, `Cache-Control: no-cache` |
| `GET /api/characters/{id}/export` | her V2+V3 card PNG, as a download, with defaults |
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

A refused export is the one place `detail` is an object rather than a string, because the client
has to act on it — see [the card format](card-format.md#when-the-export-refuses):

```json
{"detail": {"detail": "…why…", "code": "review_required",
            "surface": "vault/soul/USER.md", "field": "",
            "overlaps": [{"surface": "…", "excerpt": "…", "hard": false}]}}
```

`code` is one of `leak` (never overridable), `review_required` (re-send with
`{"acknowledged": true}` once a human has read the passages), `manifest`, `validation` or
`invalid`.

## Runtime routes

### Conversation

| Route | |
|---|---|
| `POST /api/chat` | `{text, session_id?, channel?, client_id?}` → `{session_id, user_message, message, active_selfies}`. Mirrors the voice route minus audio; `telegram` is a reserved origin |
| `POST /api/chat/cancel` | `{client_id, selfie_ids?}` → cancel that browser turn and its correlated camera work |
| `POST /api/greeting` | `{session_id?, channel?}` → `{session_id, message}`. She speaks first: the voice route greets on connect, a text client asks. Committed `proactive`, never persisted, once per session per run (`message: null` after that). The first-ever call plays her cold open |
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
- **Down:** `session`, `warming`, `filler` / `audio` (base64 PCM plus the sentence text, for
  visemes), `done`, `cancelled`, `error`.

Turn expressions are re-routed onto the event bus, so the face has exactly one lane.

Opening this socket is what loads her voice, and closing the last one frees it (see
[voice](voice.md#when-she-loads-it)): the first client into a cold room gets a `warming` frame and
waits ~20 s for the models rather than being answered by a stand-in.

### Health and boot

| Route | |
|---|---|
| `GET /api/health` | what's actually wired: character, channels, voice (loaded/listeners/stt/tts/vad), tools, mind, activity, selfies, viewers, context |
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
| `/assets/`, `/dashboard/`, `/studio/`, `/live2d/`, `/models/`, `/shared/`, `/js/` | static |

## Notes for client authors

- Render off `/api/events`; never poll. Filter your own echoes with `message.channel`.
- Send turns to `POST /api/chat` with your own `channel` string.
- Holding the SSE stream open makes her think you're present. That's usually what you want; a
  channel that is *reachable but not present* (Telegram) deliberately doesn't.
- Her proactive lines arrive as ordinary `message` events flagged `proactive`, so initiative comes
  free with the bus.
