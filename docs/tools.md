# Tools — her hands

She has five tools, reached over a real **MCP** connection: an in-repo MCP server
(`yurios/world/tools/server.py`, FastMCP over stdio) that the brain connects to as a genuine MCP
client, discovering the tools with `list_tools` rather than hardcoding them.

```ini
TOOLS_BACKEND=mcp                 # mcp | fake | off
```

| Value | What it means |
|---|---|
| `mcp` *(default)* | the real in-repo MCP server over stdio |
| `fake` | deterministic offline results — tests, and a no-deps demo |
| `off` | no hands: she talks about doing things instead of doing them |

If the SDK or the server fails, the build degrades to tools-off and keeps talking. `/api/health`
reports the truth (`"mcp"` / `"fake"` / `"off"` / `"failed: …"`).

## The hands

| Tool | Arguments | Effect |
|---|---|---|
| `set_timer` | `minutes` (0 < m ≤ `TIMER_MAX_MINUTES`), `label?` | the host schedules the announcement |
| `play_music` | `action`, `track?`, `volume?` | drives the browser-side synthesized ambience |
| `get_weather` | `city?` (default `WEATHER_CITY`) | a real lookup — Open-Meteo, keyless |
| `take_selfie` | `look?`, `scene?`, `framing?`, `lighting?`, `mood?`, `wardrobe?`, `avoid?` | starts a render off-turn — see [Selfies](selfies.md) |
| `show_picture` | `subject`, `avoid?` | the same camera, pointed at something that isn't her |

The surface doesn't grow a shell. Heavy sandboxed hands are a named later rung, not an omission
to be patched around.

### set_timer

The MCP server is the *contract and audit point* — it validates and records — but the **host**
schedules the wake, because only the host owns her voice. When a timer elapses she announces it
aloud through the ambient seam, queued until it's deliverable: if nobody is there to hear it, it
waits rather than being lost. Timer announcements are delivered by the mind loop, so they are not
announced while `MIND_ENABLED=false` or while no model is configured.

```ini
TIMER_MAX_MINUTES=180
TOOL_RATE_TIMER=6                 # calls per minute
```

### play_music

Drives a generative ambient pad synthesized in the browser — not a media library. The seam is the
point: it proves an effector that reaches the frontend, and it costs nothing to run.

Because it is a synthesizer and not a library, `track` is a closed set — `warm_pad` and
`night_piano` are the two generators `web/js/music.js` actually implements. It is annotated as a
`Literal`, so the catalog reaches the model as an `enum` in the tool schema rather than as prose in
the description; a description alone gets a made-up track name back. This is the opposite of
`take_selfie`, where the library is deliberately a starting point and anything off-menu passes
through — the difference is that a selfie prompt is free text all the way down, and a track name
has to match a generator that exists.

```ini
TOOL_RATE_MUSIC=6
```

### get_weather

A real HTTP lookup against Open-Meteo, which needs no key, behind a provider seam with an offline
fake.

```ini
WEATHER_BACKEND=open_meteo        # open_meteo | fake
WEATHER_CITY=Seoul                # the default when she isn't told one
TOOL_RATE_WEATHER=4
```

### take_selfie and show_picture

Her camera has its own page: [Selfies](selfies.md). Two hands share it — `take_selfie` for a
picture of her, `show_picture` for a picture of anything else — and with `SELFIE_BACKEND=off`
neither is advertised at all: `list_tools` returns three.

## How a tool call actually happens

A `## TOOLS` block is appended to her system prompt, built from the *discovered* schemas. It tells
the model: speak a short lead-in sentence first, then emit a marker.

```
"let me check — [[get_weather {"city": "Seoul"}]]"
```

The streaming parser strips markers from her speech, tolerates markers split across token
boundaries, and silently drops unclosed, unknown or oversized ones (a 12B local model *will* emit
a broken one). On a closed marker: guard check → MCP call → a **continuation stream** — the
original messages plus her partial reply plus a `((tool result: …))` cue — which she finishes as
the same turn. So she *speaks to* what her hands found, rather than reciting a payload.

**First audio never waits on a tool:** the lead-in sentence reaches TTS before the call runs.
Barge-in cancels the continuation, and a barged-in tool turn persists nothing.

## The guard

Every call passes `yurios/world/tools/guard.py` first:

- an **allowlist** — exactly the discovered tools; anything else is denied,
- **one-per-turn dedupe** — the same hand with byte-identical arguments, twice in one reply, is
  the model re-emitting a marker it already spent, not a second thing she meant. A
  `status: "started"` result reads as "nothing happened" and invites exactly that; the repeat is
  denied before the bucket, so it costs her no budget,
- **per-tool rate limits** (token buckets on the injected clock),
- a **per-turn call cap**,
- a **per-call timeout**, and
- **result truncation**.

```ini
TOOL_MAX_CALLS_PER_TURN=2
TOOL_TIMEOUT_S=10
TOOL_LOG_DIR=./tool-logs
```

She can be *asked* anything; the guard decides what her hands actually do.

## The audit log

Every call — allowed **or denied** — appends one JSONL line to `TOOL_LOG_DIR`
(`data/characters/<id>/tool-logs/calls.jsonl`):

```json
{"ts": …, "tool": "get_weather", "args": {…}, "verdict": "allow", "duration_ms": 412, "result": "…"}
```

```bash
tail -f data/characters/yuri/tool-logs/calls.jsonl
```

That file is the answer to "what did she actually do?", and it's the same file the switchboard's
log tab reads.

## The mind does not use tools

Her always-on [mind](mind.md) never *initiates* tool calls — her hands stay conversational. A
tool-bearing autonomous act needs the broker that comes with the sandboxed workshop, which is a
named next rung rather than a thing quietly half-built.
