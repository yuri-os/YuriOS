# Character dashboard API contract

The dashboard is a plain static ES-module client over YuriOS's multi-character host.
All routes are same-origin and return JSON unless noted otherwise.

## Registry

`GET /api/characters`

Returns either a JSON array or `{ "characters": [...] }`. Each active character has:

```json
{
  "id": "yuri",
  "name": "Yuri",
  "state": "awake",
  "loop_enabled": true,
  "description": "Watching rain move over the lower city.",
  "portrait_url": "/api/characters/yuri/portrait",
  "accent": "#88ad9b",
  "model": "openrouter/example/model",
  "voice": "alloy",
  "unread": { "count": 2, "selfies": 1, "latest": "2026-08-14T03:05:00" },
  "loops": { "mind": true, "utility": true, "dream": true, "hands": true },
  "notify": { "enabled": true, "available": false },
  "hands": { "enabled": true, "available": false },
  "updated_at": "2026-07-28T10:42:00Z"
}
```

`notify` and `hands` are the same shape and are read the same way: `enabled` is this
character's switch, `available` is the **house** switch behind it (`NOTIFY_ENABLED`,
`MIND_TOOLS_ENABLED`). The two are in series — a character can never talk her way past the
house — so when `available` is false the board shows her toggle **inert with the reason on
it**, rather than offering a switch that quietly does nothing. `hands` is whether her mind
may reach for a tool between conversations (SPEC §26.1); it is also mirrored in `loops` for
the four-switch stack on the tile.

`unread` is what she reached out about while nobody was in the room (SPEC §18.4) — the tile wears
a mark when `count` is above zero, and says *picture* rather than *message* when `selfies` accounts
for them. It is read from her Vault for a character whose runtime is down as well as from a live
one, because a reach-out made before the last restart is the one most worth still showing. A
missing or malformed object normalizes to zero. The mark clears when you enter her room, not from
the board.

Recognized states are `awake`, `engaged`, `dreaming`, `resting`, `attention`, and
`offline`. The UI safely displays unknown values as `unknown`.

## Loop control

`PATCH /api/characters/{id}/loop`

```json
{ "enabled": true }
```

Returns the updated character object, `{ "character": {...} }`, or any successful
JSON acknowledgement. The dashboard updates optimistically and rolls back on a
non-2xx response.

`PATCH /api/characters/{id}/controls` takes any of `mind`, `utility`, `dream`, `hands`
and `notify` as booleans. `utility`, `dream` and `notify` are wired at construction and so
rebuild the runtime; `mind` and `hands` reach the running character live. `hands` in
particular **must** land before her next tick without a restart — it is the kill switch for
autonomous tool use, and one that needed a rebuild would be a setting wearing a switch's
clothes. Revoking it cancels nothing already dispatched and denies everything after it, in
`tool-logs/calls.jsonl`.

## Character detail

The drawer lazily requests the selected tab. This keeps per-character data scoped
and avoids downloading every journal on registry load.

- `GET /api/characters/{id}/journal?page=` — the diary index, 20 days a page, newest
  first: `{ "days": [{ "day": "YYYY-MM-DD", "count": n }], "page", "has_more", "total" }`
- `GET /api/characters/{id}/journal?day=YYYY-MM-DD` — that day, newest entry first:
  `{ "day", "entries": [{ "time", "hers", "text" }] }`
- `GET /api/characters/{id}/log`
- `GET /api/characters/{id}/context-history`

`POST /api/characters/{id}/approve` accepts the review and starts her.
`POST /api/characters/{id}/start` / `stop` bring a reviewed character up or down
without archiving. `POST /api/characters/{id}/clone` duplicates the whole tree
under a new id. `POST /api/characters/{id}/archive` parks her under
`data/archives/` with an `archive.json` snapshot; `GET /api/archives` lists
those folders and `POST /api/archives/{name}/restore` puts one back.

The log route may return an array, `{ "entries": [...] }`, or `{ "logs": [...] }`.
Entry fields are normalized from `title`, `event`, `type`, `body`, `content`,
`message`, `text`, `timestamp`, `created_at`, or `time`.

Context returns a JSON object or `{ "context": {...} }`. Its values are presented
as read-only diagnostics.

## Settings

`GET /api/characters/{id}/profile` returns a settings object or
`{ "settings": {...} }` with `name`, `voice`, `model`, and `description`.

`PATCH /api/characters/{id}/profile` accepts those fields and returns an
updated character, `{ "character": {...} }`, or a successful acknowledgement.
Blank `voice` and `model` values mean inherit the node default.

Model and connection fields — `model`, `utility_model`, `connection_profile`,
`endpoint`, `api_key_env` and the model knobs (`temperature`, `chat_thinking`,
`utility_thinking`, `max_reply_tokens`, `context_length`) — reach a running
character **without a restart**, and the response carries `applied: [...]` naming
what moved on the live runtime. The character is rebuilt only when the save
actually changed something wired at construction — her name, voice, body, or the
utility/dream loops — so posting the whole form with one new model value keeps
her conversation alive. `mind` and `hands` are applied live rather than rebuilding her.

## Her brain

`GET /api/characters/{id}/brain` returns the same fields as a form:

```json
{ "character": "yuri", "name": "Yuri", "running": true,
  "connection_profile": "default", "key_configured": true,
  "fields": [{ "key": "chat_model", "type": "model", "value": "ollama/llama3",
               "inherited": "lm_studio/…", "help": "…" }],
  "effective": { "chat_model": "…", "utility_model": "…",
                 "endpoint": "…", "api_key_env": "OPENROUTER_API_KEY" } }
```

`value` is her own override (`""` = inherit) and `inherited` is what the node's
`.env` would give her. `PATCH` takes `{ key: value }` with an empty value
clearing an override, and answers with the same payload plus `changed` (what was
written to the registry) and `applied` (what moved on the live runtime). Both
routes are also served unprefixed as `/api/brain` for the primary character,
which is what the gear panel in a single-companion install calls. The API key is
never sent or accepted — only the name of the variable holding it.

The dashboard reaches it from **Her brain** in the settings dialog's footer. The
two panels are deliberately never open at once: the profile `PATCH` writes her
model, endpoint and key rows too, so leaving one closes it and coming back
reopens it with a fresh `GET /profile` — a stale copy can never be saved over a
brain change. Leaving with unsaved profile edits takes two presses, the first of
which says what would be lost. What the brain panel adds over the profile form
is the thing a plain text box cannot say — every field is tri-state, and an
empty one names the value it inherits — plus `chat_thinking`,
`utility_thinking`, `temperature`, `max_reply_tokens` and `context_length`,
which the profile form has no row for. Only changed fields are sent.

## The house `.env`

`GET /api/settings` returns the installation's configuration as groups of fields —
the table in `yurios/envfile.py`, which is every knob the running `Config` declares,
not a shortlist. A field is `{ key, type, help, value }`, except a secret, which is
`{ …, configured: bool }` and never carries its value. `POST` takes `{ KEY: value }`
for the fields that changed and answers
`{ written: [...], ignored: [...], restart_required: bool, env_path }`. A blank
string preserves a secret and JSON `null` removes it. A save that would leave the
installation unable to start — an owner token under 32 characters, or a non-loopback
`HOST` with no token — is refused with 422 rather than written.

The board declares these itself rather than passing them to a character runtime, so
**House settings** works on a node with nothing running. Where a primary character is
up, her config answers, since a per-character channel credential resolves to the
variable she actually reads it from.

## Pairing a device

`GET /api/pairing` describes how to get into this installation from somewhere else:

```json
{ "configured": true, "live": true, "reachable": true, "host": "0.0.0.0",
  "port": 8768, "token": "…", "env_path": "…",
  "links": [{ "origin": "http://192.168.1.20:8768",
              "url": "http://192.168.1.20:8768/auth?token=…&next=%2F",
              "qr": "<svg …>" }] }
```

One entry per address a phone might reach her at, most likely first — the Host header
this request arrived on leads, since something on the network demonstrably routes
there. `reachable` is false for a loopback bind, where no code will help.
`live` is false when `.env` holds a token the running server is not honouring yet
(a hand-edited file), so the panel can say which.

`POST /api/pairing/token` generates a token, writes it, and applies it to the running
boundary at once — no restart — returning the same payload. Rotating signs every other
session out; the caller keeps its own, because the response re-issues its cookie.

Both routes hand over the secret in the clear, which is the exception to the
write-only rule above and is the point: the caller is already either on the loopback
interface or holding that same token, and the value has to reach another device.
Scanning the link opens `GET /auth?token=…`, which exchanges the token for the
HttpOnly session cookie and redirects — the phone never stores the token.

## PNG import

`POST /api/characters/import` accepts `multipart/form-data` with the character PNG
in the `file` field. The client accepts PNG files up to 25 MB. A successful response
may contain the imported character; the dashboard refreshes the registry regardless.

## Approve

A character whose summary carries `review_required: true` was imported from a card
this node did not write: she is registered, her rooms are served, and nothing is
running behind them. `POST /api/characters/{id}/approve` takes no body and returns
`{ "character": {...}, "started": bool, "error": string|null }`. The two flags are
separate on purpose — the review is cleared either way, and a runtime that failed to
come up is reported rather than silently re-parked. The drawer only offers this while
`review_required` is set.

While she is parked the dashboard also refuses to walk into her rooms: every sanctuary,
Live2D and text link on her card and in her drawer opens the approval dialog instead, and
a successful approve continues to the door that was clicked. The room routes themselves
stay reachable by URL — the host's own refusal (a `4404` close on the voice socket,
carrying the reason) is what a client sees if it gets there another way.

## Archive

`POST /api/characters/{id}/archive` takes no body. Archive is intentionally distinct
from permanent deletion: it stops the character loop and removes the character from
the active `GET /api/characters` registry. Any 2xx JSON response is accepted.

Permanent deletion is a separate two-step API and is not currently exposed as a
dashboard button. Call `POST /api/characters/{id}/purge/prepare`, then send the
returned short-lived `challenge` in the JSON body of `DELETE
/api/characters/{id}/purge`. Challenges are character-bound and single-use; they
must never be put in a query string.

## Errors and sanctuary route

Errors should use an appropriate non-2xx status and one of `detail`, `error`, or
`message` in the JSON body. The dashboard enters a character via the stable browser
route `/characters/{id}/sanctuary/`; this is navigation, not an API request.

## The mind debug page

The fourth way off a character card — and the **Debug mind** button in the detail
drawer's action row — is not a room: `/characters/{id}/mind` (`web/mind/`)
reads her files rather than talking to her — the activity timeline, tick traces, every
context window she was given, her tool calls, and the Vault's own history. It is
navigation, like the rooms, and it is **not** gated by the approval dialog: a parked
character is one you may well want to inspect before deciding.

Its API lives on the host under `/api/characters/{id}/debug/*` and is documented in
`docs/api.md`. Note the namespace: `…/{id}/mind` is already dispatched to the runtime's
own `/api/mind` (the sanctuary's inner-life panel), so the debug routes may never
live there.
