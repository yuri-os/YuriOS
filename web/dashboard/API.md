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
  "updated_at": "2026-07-28T10:42:00Z"
}
```

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

## Character detail

The drawer lazily requests the selected tab. This keeps per-character data scoped
and avoids downloading every journal on registry load.

- `GET /api/characters/{id}/journal`
- `GET /api/characters/{id}/log`
- `GET /api/characters/{id}/context-history`

Journal and log routes may return an array, `{ "entries": [...] }`, or respectively
`{ "days": [{ "day": "YYYY-MM-DD", "entries": [...] }] }` and `{ "logs": [...] }`.
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

## Errors and sanctuary route

Errors should use an appropriate non-2xx status and one of `detail`, `error`, or
`message` in the JSON body. The dashboard enters a character via the stable browser
route `/characters/{id}/sanctuary/`; this is navigation, not an API request.
