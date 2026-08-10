# Tools — her hands

Her hands are reached over a real **MCP** connection: an in-repo MCP server
(`yurios/world/tools/server.py`, FastMCP over stdio) that the brain connects to as a genuine MCP
client, discovering the calls with `list_tools` rather than hardcoding them. There are fifteen
built-in calls when every optional group is enabled, plus any third-party MCP calls you mount.
Disabled groups are not advertised at all.

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

| Tool | Arguments | Effect | Available when |
|---|---|---|---|
| `set_timer` | `minutes` (0 < m <= `TIMER_MAX_MINUTES`), `label?` | schedules a spoken announcement | always |
| `play_music` | `action`, `track?`, `volume?` | drives browser-side synthesized ambience | always |
| `take_selfie` | `look?`, `scene?`, `framing?`, `lighting?`, `mood?`, `wardrobe?`, `avoid?` | starts a render off-turn | `SELFIE_BACKEND` is not `off` |
| `show_picture` | `subject`, `avoid?` | starts a render of something other than Yuri | `SELFIE_BACKEND` is not `off` |
| `web_search` | `query`, `k?` | returns titles, links, and snippets from SearXNG | `SEARCH_BACKEND` is not `off` |
| `read_page` | `url` | reads one page and shelves it | `SEARCH_BACKEND` is not `off` |
| `research` | `topic`, `depth?` | reads several pages off-turn and posts what she found | `SEARCH_BACKEND` is not `off` |
| `list_notes` | `folder?` | lists files on Yuri's workspace desk | `WORKSPACE_ENABLED=true` |
| `read_note` | `path` | reads one workspace note | `WORKSPACE_ENABLED=true` |
| `write_note` | `path`, `text` | replaces a workspace note | `WORKSPACE_ENABLED=true` |
| `append_note` | `path`, `text` | adds text to a workspace note | `WORKSPACE_ENABLED=true` |
| `delete_note` | `path` | removes a workspace note | `WORKSPACE_ENABLED=true` |
| `read_skill` | `name` | reads a skill's full instructions | `SKILLS_ENABLED=true` |
| `write_skill` | `name`, `description`, `instructions` | saves or replaces a skill | `SKILLS_ENABLED=true` |
| `delete_skill` | `name` | removes a skill | `SKILLS_ENABLED=true` |

Third-party calls configured through `MCP_SERVERS` are discovered and admitted through the same
guard, with `TOOL_RATE_EXTERNAL` as their default rate. The surface doesn't grow a shell. Heavy
sandboxed hands are a named later rung, not an omission to be patched around.

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

### Workspace notes and skills

The desk calls operate only inside `vault/workspace/`: relative paths only, no `..`, dotfiles, or
paths outside the desk. `write_note` replaces a whole file; use `append_note` for a running log.
`list_notes` returns names and sizes, and `read_note` returns the content. Notes are scratch space,
so they are not versioned in the Vault.

Skills live in `vault/skills/`. `read_skill` loads the instructions for one named skill;
`write_skill` stores a lowercase-hyphenated name, a short description that says when to use it,
and its full instructions; `delete_skill` removes it. Skills are versioned. See [The mind](mind.md#her-desk-and-her-skills)
for the storage layout and prompt behavior.

```ini
WORKSPACE_ENABLED=true             # false = no note calls or desk context
SKILLS_ENABLED=true                # false = no skill calls or catalog context
TOOL_RATE_DESK=20                  # calls per minute across notes and skills
```

### web_search, read_page and research

Her three web hands. They arrive together or not at all — searching with no way to read what you
found is half a capability, and `research` is the two of them in sequence — so one knob turns all
three on:

```ini
SEARCH_BACKEND=off                # searxng | fake | off
SEARXNG_URL=http://localhost:8080
SEARCH_RESULTS=5                  # rows per web_search
SEARCH_LANGUAGE=en
SEARCH_SAFESEARCH=1               # 0 none | 1 moderate | 2 strict
FETCH_TIMEOUT_S=8
FETCH_MAX_BYTES=2000000
RESEARCH_MAX_PAGES=5
TOOL_RATE_SEARCH=6
TOOL_RATE_READ=6
TOOL_RATE_RESEARCH=2
```

With `SEARCH_BACKEND=off` — the default — `list_tools` doesn't mention them at all. No hand, not a
dead one, the same rule as `SELFIE_BACKEND=off`.

> **Experimental — and this is the hand that can cost you money.** The web hands are new and still
> moving, and `research` is the one thing here that keeps spending after it has answered: it
> returns to the conversation in milliseconds and then searches, fetches and *reads*, for as long
> as that takes. A long page is a model call per passage plus an embedding per chunk — dozens to
> hundreds of calls, over half an hour, from one sentence you said in passing. On a local model
> that is fan noise. On a metered API it is a bill, and nothing in the loop stops to ask you first.
>
> `MIND_DAILY_TOKENS` is a governor, not a spend cap: it is an estimate, it never gates
> conversation, it does not abort a read already in flight, and it does not stand between a tool
> call and the run it starts. What actually bounds a run is `RESEARCH_MAX_PAGES`, the
> `TOOL_RATE_RESEARCH` bucket, and you — the **inner life** tab shows every run and the document
> being read right now with its model-call count, and the stop button there loses nothing. If she
> is pointed at a paid API, set a spend limit with the provider as well.

**Why SearXNG.** SearXNG is the search backend because it is keyless *and* third-party-less. You run the instance, so the record of what
she searched for is a file on your own machine. That is the local-first argument applied to the
one capability that usually hands your curiosity to somebody else.

#### Setting it up

The installer asks, and does the whole thing if you say yes — pulls the image, writes a settings
file with the JSON format enabled, starts the container, and flips `SEARCH_BACKEND` in `.env`:

```bash
./install.sh --web-search      # or answer y when it asks
```

It needs Docker. If Docker isn't usable the installer says so and carries on with web search
off — an optional capability never costs you the rest of the install.

After that the container is looked after for you:

| | |
|---|---|
| `yurios start` | brings `yurios-searxng` up alongside her if it's stopped |
| `yurios doctor` | says whether she can actually search *right now* |
| `data/searxng/settings.yml` | yours to edit — engines, safesearch, languages |

The settings file is written once and never overwritten, so a rerun of the installer won't revert
your edits. It's mounted read-only, which keeps it yours: SearXNG's entrypoint chowns
`/etc/searxng` to its own uid when it can, and that quietly takes your own config file away from
you.

> **The trap this saves you from.** SearXNG ships with its JSON output format **disabled**. An
> instance that answers a browser perfectly will answer *this* with `403` until `json` is in
> `search.formats`. The installer's settings file has it on from the start; if you point
> `SEARXNG_URL` at an instance you already run, `yurios doctor` names this specific cause, because
> a bare 403 sends you looking at authentication instead.
>
> ```bash
> curl 'http://localhost:8080/search?q=test&format=json'
> ```

**Pointing her at an instance you already have** is fine — set `SEARXNG_URL` and leave the
container alone. Anything that isn't loopback is never started, stopped or recreated by YuriOS,
and a working instance on loopback that we didn't create is reported as "not ours" rather than
claimed.

**Search then read is exactly two calls,** which is `TOOL_MAX_CALLS_PER_TURN`. That is deliberate:
the budget was sized for a hand that finds a thing and a hand that opens it. Raising the cap to fit
a third call raises it for every other tool too.

#### What she reads, she keeps

This is the part that makes the web hands more than a slower lookup. A tool result is
normally 600 characters that expire when the turn ends. Instead, every page — whether `research`
fetched it or she opened it herself with `read_page` — is ingested into her
[knowledge store](mind.md): chunked, situated, embedded, and indexed with a doc and character span
she can cite. The document carries the URL it came from in its header, so a citation survives the
round trip back to a source.

```bash
ls vault/knowledge/reference/          # web-on-tea-1754557200.md, …
```

And it comes back. Every turn she assembles searches the shelf for whatever you just said and
puts the best few chunks into the system prompt as **WHAT YOU'VE READ**, each with its
`doc (chars a-b)` citation, so she can answer from a page she read last week and tell you where
it came from. That block is deliberately not the memory block: reading is not remembering, and
a page she found must never come back as something you told her. `KNOWLEDGE_K` sets how many
chunks; `KNOWLEDGE_K=0` turns the slot off and leaves the shelf as a pure archive.

The model never sees the whole page. `read_page` returns a ~400-character `gist` for her to speak
to and the full text alongside it; the brain truncates only the copy that reaches the model and the
audit log, and hands the untruncated one to the host for shelving. Two audiences, one call.

With no mind running (`MIND_ENABLED=false`, or no model chosen yet) there is no shelf, so nothing
is kept — she still searches, still reads, still tells you what she found, and the message says
plainly that it wasn't filed.

#### research is start-don't-await

A search plus three fetches plus three embedding passes is twenty-odd seconds and `TOOL_TIMEOUT_S`
is ten. So `research` follows the camera's rule (§7.6): the server validates the ask and answers
`{"status": "started"}` immediately, the turn finishes with no dead air, and the reading happens
off-turn in `yurios/world/research.py`. What she found arrives in the chat as a message a while
later, routed back to the channel that asked, followed by one soft spoken line if she's free.

`depth` controls how many pages a run reads. When Yuri leaves it out, it defaults to
`RESEARCH_MAX_PAGES`; raise that setting to allow and default to a larger run.

A page that won't open — a paywall, a 404, a PDF — is skipped, not fatal. A run where nothing
opens still ends in her saying so.

#### read_page will not read this machine

`url` is the first tool argument that comes from a language model rather than from a person, and
her own API is on `localhost:8768` with the settings panel's keys behind it. So before every
request — and again on every redirect hop, which is why redirects are followed by hand —
`yurios/world/tools/fetch.py` rejects any scheme that isn't http/https, resolves the host, and
refuses private, loopback, link-local, reserved and multicast addresses. Non-text responses are
refused too, and the body stops at `FETCH_MAX_BYTES`.

### take_selfie and show_picture

Her camera has its own page: [Selfies](selfies.md). Two hands share it — `take_selfie` for a
picture of her, `show_picture` for a picture of anything else — and with `SELFIE_BACKEND=off`
neither is advertised at all.

## How a tool call actually happens

A `## TOOLS` block is appended to her system prompt, built from the *discovered* schemas. It tells
the model: speak a short lead-in sentence first, then emit a marker.

```
"let me write that down — [[write_note {"path": "research/you.md", "text": "…"}]]"
```

The streaming parser strips markers from her speech, tolerates markers split across token
boundaries, and silently drops unknown or oversized ones. On a closed marker: guard check → MCP
call → a **continuation stream** — the original messages plus her partial reply plus a
`((tool result: …))` cue — which she finishes as the same turn. So she *speaks to* what her hands
found, rather than reciting a payload.

**First audio never waits on a tool:** the lead-in sentence reaches TTS before the call runs.
Barge-in cancels the continuation, and a barged-in tool turn persists nothing.

### Reading the marker the model actually writes

A 12B local model *will* emit a broken one, and the interesting part is *how*. Driven through
every desk tool, the live model got the closer wrong every single time:

```
[[read_note {"path": "research/probe.md"}] ]
```

A space between the brackets. `endswith("]]")` never fires on that, so the marker stayed open and
swallowed everything after it — her next sentences and her next marker — until some accidental
`]]` came along. One space cost the whole turn. The prose tools are worse: asked to serialize a
paragraph it is still composing, the model leaves literal newlines and bare `"` inside the JSON
string and sometimes stops a bracket short.

So the parser reads what arrives rather than what the directive asked for:

| what she writes | what happens |
|---|---|
| `}]]`, `}] ]`, `}]\n]` | all close the marker; a stray bracket left in the body is trimmed |
| a literal newline or bare `"` inside a prose argument | the object is re-read leniently — `json` still owns every scalar, list and nested object |
| `}]` and then end-of-stream | salvaged, if the body is otherwise complete |
| anything still unreadable | audit line (`dropped: malformed marker`), marked unrun in the verbatim record, and **one** re-emit pass |

Every recovery is self-validating: it can only ever produce a call that parses, so junk still
drops. That last row matters as much as the others — a silently dropped marker stays in the
transcript, so next turn she reads her own broken call back as evidence and tells you the note
exists.

**She does not say her lead-in twice.** "Continue from where you left off" reads to the model as
"say it again, then continue", so the continuation's echo of the previous pass is matched and
dropped. The match *holds* rather than swallows, so a continuation that merely opens the same way
("I'll check." → "I'll check the other one too.") is released whole.

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
{"ts": …, "tool": "write_note", "args": {…}, "verdict": "allow", "duration_ms": 12, "result": "…"}
```

```bash
tail -f data/characters/yuri/tool-logs/calls.jsonl
```

That file is the answer to "what did she actually do?", and it's the same file the switchboard's
log tab reads.

## Somebody else's MCP server

The client was always a genuine MCP client — point it at a different command line and she has
different hands. `MCP_SERVERS` relaxes that to more than one at a time:

```ini
MCP_SERVERS=./mcp-servers.json
TOOL_RATE_EXTERNAL=4              # default bucket for a discovered tool
```

```json
{
  "mcpServers": {
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "env": {}, "rate": 4}
  }
}
```

It is the same `{"mcpServers": {...}}` shape everything else uses, so a config you already have
pastes straight in. `rate` is the one addition — calls per minute for every tool that server
offers; without it they get `TOOL_RATE_EXTERNAL`.

Unset (the default) means her own server alone, byte for byte as before. With servers configured:

- her server is mounted **first**, so a third-party server advertising a name she already has is
  ignored rather than intercepting hers — the log says so;
- tool names stay **unprefixed**, because the model reads them, the audit log records them and the
  host dispatches on them;
- a server that **won't start is skipped, not fatal**. A typo in a third-party entry costs her that
  server, not her timers and her desk. `/api/health` and the boot board report how many of each
  came up.

Discovery is the allowlist for these (§7.3): nobody here can hardcode a rate for a tool whose name
isn't known until `list_tools` answers, so whatever a mounted server offers is admitted at its
configured rate. Her own hands are *not* admitted this way — their rates encode decisions discovery
can't see, like `SELFIE_BACKEND=off` leaving the camera out of the buckets.

**Mounting a server gives her its hands, rate-limited but not reviewed.** That is the trade the
knob exists to make; make it deliberately.

## The mind does not use tools

Her always-on [mind](mind.md) never *initiates* tool calls — her hands stay conversational. A
tool-bearing autonomous act needs the broker that comes with the sandboxed workshop, which is a
named next rung rather than a thing quietly half-built.
