# HTTP & event API

One process, one origin, port **8768**. Everything is same-origin JSON unless noted.

With the default loopback bind no credential is required. A non-loopback bind requires a 32+
character `OWNER_TOKEN` and protects every HTTP, SSE and WebSocket route. Browsers authenticate at
`/auth`; API clients send `Authorization: Bearer <token>`. Cross-origin browser requests and
WebSocket handshakes are rejected. Use an encrypted private transport such as Tailscale or SSH, or
put an authenticated TLS reverse proxy in front; the built-in server is plain HTTP.

All app factories enforce a streaming 48 MiB HTTP request-body ceiling. Oversized
`Content-Length` values or chunked bodies return JSON `413`; SSE responses and
WebSockets are not wrapped by this limiter. Interactive inference overload returns
JSON `503` with `Retry-After: 1`.

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
| `PUT /api/connections/{name}` | validate and save a host-owned `{backend, endpoint, api_key_env}` profile; retunes bound runtimes |
| `GET /api/characters/{id}/brain` | the character's effective brain connection and tuning settings |
| `PATCH /api/characters/{id}/brain` | save model/tuning overrides and apply them immediately; direct `endpoint`/`api_key_env` writes are rejected |
| `GET /api/brain` | the primary character's brain settings |
| `PATCH /api/brain` | save and immediately apply the primary character's brain overrides |
| `POST /api/characters/import` | `multipart/form-data`, the card PNG in `file` |
| `POST /api/characters` | create from a studio draft: `{draft, portrait?, character_id?}` → 201 |
| `GET /api/studio/template` | `{draft, sections, constitution_fields}` — a starting draft |
| `GET /api/characters/{id}/studio` | `{draft, provenance, grown, images, sections}` |
| `PATCH /api/characters/{id}/studio` | save a draft into the SOUL; one commit; restarts her |
| `POST /api/characters/{id}/studio/preview` | the card, report and privacy pane — no file |
| `POST /api/studio/optimize` | re-file a card with a model: `{draft, instructions?, model?, character?}` → `{draft, changes, notes, model, truncated, failed}`. **Proposes only** — nothing is written until the PATCH above. 502 with a readable reason if the model can't be reached or answers with nothing usable. Send `Accept: application/x-ndjson` to watch it instead (below) |
| `GET /api/studio/models?provider=&character=` | the optimize dialog's picker, same shape as `/api/models` but answered by the host, so it works for a character still under review |
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
| `GET /api/characters/{id}/journal?page=` | the diary index, 20 days a page, newest first: `{days: [{day, count}], page, has_more, total}` |
| `GET /api/characters/{id}/journal?day=` | one day's entries, newest first: `{day, entries: [{time, hers, text}]}` |
| `GET /api/characters/{id}/log` | the tail of her tick trace + tool audit, interleaved |
| `GET /api/characters/{id}/context-history` | `{context, history}` |
| `POST /api/characters/{id}/archive` | stop + move her root to `data/archives/` |
| `POST /api/characters/{id}/purge/prepare` | issue a high-entropy, short-lived, single-use deletion challenge |
| `DELETE /api/characters/{id}/purge` | permanently delete using `{"challenge":"…"}` in the JSON body; no query secret |
| `GET /api/onboarding` | local or owner-authenticated first-run model state: `{configured, model, recommendations, download}` |
| `POST /api/onboarding` | local or owner-authenticated model choice: `{"model": "…"}`; saves the house selection, starts a GGUF download when applicable, and returns `restart_required: true` |

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

### Watching an optimisation

`POST /api/studio/optimize` re-files a card in three sequential calls to a model,
which on a local one is minutes. Send `Accept: application/x-ndjson` and it
answers with one JSON object per line as the run happens:

```
{"event":"pass","state":"start","index":1,"total":3,"name":"persona","label":"who she is"}
{"event":"pass","state":"retry","index":1,"total":3,"name":"persona","label":"who she is"}
{"event":"pass","state":"done","index":1,"total":3,"name":"persona","label":"who she is","fields":["identity","history"]}
…
{"event":"done","result":{"draft":{…},"changes":[…],"notes":"…"}}
```

The last line is always `done` or `error`. The status is 200 before the first
pass has run, so a failure arrives as `{"event":"error","message":…}` rather than
as a code — the same sentence the JSON route would have put in `detail`. Closing
the connection cancels the run. Everything else about the endpoint is unchanged:
without that Accept header it answers with the single object above.

Errors use a non-2xx status with `detail` in the body.

A refused export is the one place `detail` is an object rather than a string, because the client
has to act on it — see [Characters → When the export refuses](characters.md#when-the-export-refuses):

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
| `POST /api/chat` | `{text, session_id?, channel?, client_id?, image_id?}` → `{session_id, user_message, message, active_selfies}`. Mirrors the voice route minus audio; `telegram` is a reserved origin |
| `POST /api/chat/cancel` | `{client_id, selfie_ids?}` → cancel that browser turn and its correlated camera work |
| `POST /api/greeting` | `{session_id?, channel?}` → `{session_id, message}`. She speaks first: the voice route greets on connect, a text client asks. Committed `proactive`, never persisted, once per session per run (`message: null` after that). The first-ever call plays her cold open |
| `GET /api/history` | the last 100 chat entries, for backfilling a fresh page |
| `GET /api/inbox` | `{entries, unread}` — what she reached out about while the room was empty, oldest first. `?all=1` includes what has already been seen |
| `POST /api/inbox/read` | `{marked, unread}` — everything pending has now been seen. Owner-gated |

`image_id` sends a picture with the line (SPEC §35) — the id `POST /api/uploads` answered with,
never the bytes. `text` may be empty when one is attached. An id that no longer resolves is a
`404` rather than a turn with the words alone, and a model that cannot be sent pictures answers
`409`. See [Models](models.md#can-she-see-pictures).

`/api/history` is an in-memory ring and does not survive a restart; the inbox is on disk and does.
A page opening merges the two by message id and shows what it has not seen under a *while you were
away* rule, then marks it read — being in her room is the acknowledgement, so there is no
per-entry dismiss. See [The mind](mind.md) and [Channels](channels.md#desktop-notifications).

Text turns from all channels serialise on one lock. HTTP text and session fields are
bounded, and each character admits one active HTTP turn plus two waiters; further
turns receive the retryable `503` response above. Typed WebSocket text is capped at
16 KiB and session IDs at 128 bytes.

### The event bus

`GET /api/events` — Server-Sent Events, one `data:` line per event.

On attach you get `hello`, then a replay of sticky appearance state (last-write-wins), then live
events. The stream pings while idle and ends itself on shutdown, so an open tab never holds Ctrl+C
hostage. **Attaching counts as presence**: it posts `user_present` to the mind, and the last
detach posts `user_absent`.

| Event | Payload |
|---|---|
| `hello` | `{character: "<name>"}` |
| `capabilities` | `{image_input, detail}` — whether her model can be sent a picture; sticky, and re-published when the model is swapped |
| `message` | a chat entry — including `image_url` selfies, the originating `channel`, and `unheard` on a line she started into a room that may have been empty |
| `gallery` | a picture was scored (`{action: "rate", image, score, by, at}`) — so a second open room stops showing the old number |
| `draft` / `draft_cancel` | streaming sentence drafts |
| `avatar` | expression, gaze, posture, visemes, `rain`, `music` — the puppet lane |
| `journal` | a new `[she]` journal line |
| `mind` | activity/budget/goal updates for the inner-life tab |
| `context` | current `{used, limit, limit_source, reserve, exact, pct}` context-meter snapshot; sticky |
| `selfie_status` | `{id, state, client_id?}` for asynchronous camera work: `started`, `done`, `cancelled`, or `error` |

Publishes are non-blocking (a stalled client loses events, never blocks the publisher) and
thread-safe.

`GET /api/notifications` is a second, much smaller stream, present only when `NOTIFY_ENABLED` is
on (`404` otherwise, so a client stops asking). It carries `{type: "notify", character, title,
body, message_id, kind}` for `unheard` lines and nothing else, and the Electron desktop shell
reads it. **Attaching does not count as presence** — that is the whole reason it is not
`/api/events`. A shell sits in the tray for hours, and presence there would make her believe you
were in the room permanently, which would suppress the very reach-outs the stream exists to
deliver.

### Audio

`WS /ws/voice` — the audio-only socket.

- **Up:** binary mic PCM, plus `hello` / `endpoint` / `bargein` / `text` control frames. A `text`
  frame may carry `image_id` (from `POST /api/uploads`) — the id, never the bytes, which is what
  keeps a picture turn on the path that has TTS on the end of it.
- **Down:** `session`, `warming`, `ready`, `processing`, `filler` / `audio` (base64 PCM plus the
  sentence text, for visemes), `done`, `cancelled`, `error`. `ready` means the voice stack is
  available; `processing` includes the accepted turn's optional `client_id`.

Turn expressions are re-routed onto the event bus, so the face has exactly one lane.

Opening this socket is what loads her voice, and closing the last one frees it (see
[voice](voice.md#when-she-loads-it)): the first client into a cold room gets a `warming` frame and
waits ~20 s for the models rather than being answered by a stand-in.

### Health and boot

| Route | |
|---|---|
| `GET /api/health` | what's actually wired: character, channels, voice (loaded/listeners/stt/tts/vad), tools, mind, activity, selfies, `image_input` (+ `image_input_detail`), viewers, context |
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
| `GET /api/mind/reading` | research runs, the document being read right now with its passage and model-call counts, and everything held |
| `POST /api/mind/reading/stop` | `{"run": "<id>"}` to stop a research run, `{}` to stop just the read in flight |
| `POST /api/mind/reading/resume` | `{"doc": "<name>"}` — let a held document be read again, from where it stopped |
| `POST /api/mind/dream/run` | manually run DREAM; `day` must be canonical `YYYY-MM-DD`, and `budget` is typed then clamped to `1..MIND_DREAM_TICK_TOKENS` |
| `GET /api/mind/dream/jobs` | every job file on disk, parsed, plus the kinds and builtin names this build knows |
| `GET /api/mind/dream/jobs/{name}` | one job file, raw |
| `PUT /api/mind/dream/jobs/{name}` | `{"text": "<the whole file>"}` — validated, committed, and the running roster rebuilt |
| `DELETE /api/mind/dream/jobs/{name}` | remove it; a builtin reverts to its shipped prompt |

`/api/mind/dream` and `/api/mind/dream/jobs` answer different questions and are not
interchangeable: the first is the roster that will run tonight, builtins and files folded
together; the second is what is on disk, which is the only thing an editor can edit. A name is
matched against `^[a-z0-9_-]{1,64}$` and the path is built from it, so `vault/dreams/` is the only
directory these four can reach. A file that would not work is refused `422` with the shape of one
that would.

All of it reads *through* the mind's own stores, so the dashboard can never disagree with the
files. With the mind off, these answer `503` and `/api/health` says so — except
`GET /api/mind/reading`, which answers either way (`"mind": false`), because a panel that 503s
tells you nothing about whether anything is being spent.

A stop is cooperative — it lands after the passage she is on, and a run winds down through its own
ending — so both surfaces carry the asked-for-but-not-yet state: `reading.stopping` is `true` from
the POST until the read notices, and a run sits at `stage: "stopping"` for the same stretch. The
panel shows **busy pausing** on the button for exactly that gap.

### The mind debug page

Served at `/characters/{id}/mind` (`web/mind/`), over a read-only API on the **host**:

| Route | |
|---|---|
| `GET …/debug/overview` | activity, budget, vault head, row counts, and a manifest of every log with a `rotated` flag |
| `GET …/debug/activity?page=` | the activity-state timeline — one row per real transition, with the reason that fired it |
| `GET …/debug/ticks?page=&state=&q=` | full tick records; `…/ticks/{tick_id}` joins the calls, prompts and signals it caused |
| `GET …/debug/signals?page=&type=` · `…/debug/goals` · `…/debug/self-edits` | the inbox, her intentions, the queue waiting on your ruling |
| `GET …/debug/calls?page=&tool=&verdict=&corr_id=` | the tool audit, with the rendered photo joined on `corr_id` |
| `GET …/debug/selfies?page=` | the render ledger |
| `GET …/debug/prompts/days?page=` | days that have model calls, counted by kind |
| `GET …/debug/prompts?day=&kind=&page=` | the index — `messages` stripped, `preview` kept |
| `GET …/debug/prompts/{id}` | one whole context window; a `chat_turn` resolves its pointer into `corpus/turns.jsonl` |
| `GET …/debug/vault/commits?page=&path=` · `…/vault/commits/{sha}` | the Vault's history, and one commit's patch |
| `GET …/debug/vault/tree?path=` · `…/vault/file?path=&rev=` · `…/vault/history?path=` | browse it, read a file now or at a commit, and see every edit to it |
| `GET …/debug/memory` · `…/debug/memory/chunks?page=&kind=&q=` · `…/memory/chunks/{id}` | her memory, and the recall index (never with embeddings in a list) |
| `GET …/debug/economics` · `…/debug/utility?page=&kind=` | context pressure, the budget, and what the small model produced |

Two rules hold across all of it. Every route **reads files** — none needs her running, because a
stopped or crashed character is exactly the one you need to inspect; the only live value
(`overview.live`) is `null` when she is down and never blended into history. And paging is
newest-first over `mind/util.jsonl_page`, which reads backwards from the end of the file, so a
32 MB prompt log is never pulled into memory to show its last twenty rows.

The namespace is `debug/` and not `mind/` deliberately: `/api/characters/{id}/mind` already
dispatches to the child app's `/api/mind` above, and a host route by that name would shadow it.

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
| `GET /api/gallery?page=&limit=` | the shelf as newest-first pages over the render ledger: `{items: [{name, url, caption, created_at, backend, model, seed, prompt, negative, bytes, score, rated_at}], page, limit, has_more, total, rated}`. Limit caps at 60; a shelf with no ledger is an empty page, not a 404 |
| `POST /api/gallery/rate` | `{name, score}` — a score of 1–10 for one picture, or `null` to clear it. Appends to `selfies/ratings.jsonl` and publishes a `gallery` event; `404` for a name that is not on the shelf |
| `POST /api/uploads` | multipart `file=` → `{id, url, media_type, width, height, bytes}`. A picture to send her: decoded, oriented, capped at `CHAT_IMAGE_MAX_PX` and stripped of metadata. `409` when her model can't see, `413` over `UPLOAD_MAX_BYTES`, `415` when it isn't an image |
| `GET /api/uploads/{name}` | one picture you sent, for the transcript to render |
| `GET /api/config` | the Live2D rig registry: `{avatar_model, avatar_model_url, avatar_available}` |
| `/assets/`, `/dashboard/`, `/studio/`, `/live2d/`, `/models/`, `/shared/`, `/js/` | static |

## Notes for client authors

- Render off `/api/events`; never poll. Filter your own echoes with `message.channel`.
- Send turns to `POST /api/chat` with your own `channel` string.
- Holding the SSE stream open makes her think you're present. That's usually what you want; a
  channel that is *reachable but not present* (Telegram) deliberately doesn't.
- Her proactive lines arrive as ordinary `message` events flagged `proactive`, so initiative comes
  free with the bus.
