# YuriOS documentation

Everything about running, configuring and extending YuriOS. The [README](../README.md) is the
short version; [`SPEC.md`](../SPEC.md) is the normative specification these pages describe in
plain language. Where the two disagree, the SPEC wins — and that is a bug worth reporting.

## Experimental — and it can spend

This is a reference implementation of *initiative*, not a hardened product. Much of the autonomous
half is new and still moving: the tick loop, DREAM, self-edits, [her desk](mind.md#her-desk-and-her-skills),
[the shelf](mind.md#the-shelf-drop-folder-rag), and above all [the web hands](tools.md#web_search-read_page-and-research).
Expect rough edges, expect the shape of it to change between releases, and meet it with a local
model first.

**The reading is the expensive part.** `research` answers the conversation in milliseconds and
then keeps going on its own for as long as it takes — searching, fetching pages, and reading each
one. A long document is a model call per passage plus an embedding per chunk: dozens to hundreds
of calls, over half an hour, from one sentence you said in passing. On a local model that is fan
noise. On a metered API it is a bill, and nothing in the loop stops to ask you first.

What stands between you and that:

- **`SEARCH_BACKEND=off` is the default.** Until you turn it on, the three web hands are not
  advertised to her at all — she can't call what she can't see.
- **`MIND_DAILY_TOKENS` is a governor, not a cap.** At pressure ≥ 1.0 she sheds to DORMANT and
  goal work stops, but it is an *estimate* of spend, it never gates conversation, it does not
  abort a read already in flight, and it does not stand between a tool call and the run it starts.
- **The inner-life tab is the meter.** Every run, the document being read right now, its passage
  and model-call counts, and a stop button that keeps what she has already read.
- **`RESEARCH_MAX_CALLS=100`** caps estimated model calls admitted to one run; pages that do
  not fit wait unread on the shelf. `RESEARCH_MAX_PAGES` and `TOOL_RATE_RESEARCH` also bound
  how far one run and one minute can reach.
- **Mind tools are enabled by default**: `MIND_TOOLS_ENABLED=true`,
  `MIND_TOOL_ALLOWLIST=*`, and each character's `hands` switch is on by default. The
  guard still checks the backend, budget pressure, cooldown, rate limit, and daily cap
  before dispatch. To restrict her, set an explicit `MIND_TOOL_ALLOWLIST` or turn off
  the house or character switch. `yurios settings MIND_TOOL_ALLOWLIST` lists this
  build's hands and their backend status; the settings panel renders the same list as
  tick-boxes. A narrower starting allowlist is her desk alone:
  `write_note,append_note,read_note,list_notes`.

For autonomous calls, the numbers that stop a runaway night are different from the
estimates above, because they are *preconditions* checked before dispatch:

- **`MIND_TOOL_CALLS_PER_DAY` (64) is a cap, not a governor.** It is checked before the call
  and it refuses. `MIND_TOOL_PRESSURE_CEILING` (0.5) does the same thing with the budget:
  over it, the expensive hands — `research`, `read_page`, `web_search`, the cameras — are
  simply not offered.
- **The same call is refused for hours.** A persistent fingerprint ledger survives restarts,
  so "she re-dispatched the same research every tick all night" cannot happen.
- **Her buckets are not your buckets.** The mind gets its own rate limits
  (`TOOL_RATE_MIND_*`), so a busy night cannot leave your morning request denied.
- **Nothing she makes this way is sent to you.** It goes on her shelf, in her gallery, or on
  her desk. Whether you ever hear about it is the same reach-out gate as everything else,
  with the same quiet hours and the same daily cap.
- **`tool-logs/calls.jsonl` marks every one of them `mind_tool`.** "What did she do at 4am"
  is a file you read, not a vibe — and the switchboard's fourth toggle revokes her hands
  before her next tick, without restarting her.

If you point her at a paid API — chat, utility or embeddings — set a spend limit with the provider
as well. For the first few days, watch the numbers on the inner-life tab.

## Start here

| Page | What's in it |
|---|---|
| [Getting started](getting-started.md) | Install, first run, and the first ten minutes with her |
| [Installation](installation.md) | Every install path, the extras table, the doctor, and WSL |
| [Command line](cli.md) | `yurios` as a first-class client: settings, characters, chat, camera, dreams |
| [Models & connections](models.md) | Direct GGUF, LM Studio, Ollama, OpenRouter, supported LiteLLM routes, embeddings, context |
| [Configuration](configuration.md) | The complete `.env` reference, group by group, and the settings panel |

## Features

| Page | What's in it |
|---|---|
| [Characters](characters.md) | The switchboard, importing and writing cards in the studio, per-character storage, connection profiles, migration |
| [Bodies](bodies.md) | The VRM stage, the sanctuary room, Live2D, the transparent desktop window |
| [Voice](voice.md) | Ears, voice and turn-taking: faster-whisper, kokoro, Qwen3-TTS, GPT-SoVITS, silero |
| [Selfies](selfies.md) | Her camera: OpenRouter, local SDXL, Krea 2, the template library, provenance |
| [Tools](tools.md) | Every built-in and third-party MCP call, the guard, the audit log |
| [The mind](mind.md) | The tick loop, activity states, the two gates, goals, DREAM, her hands in the loop, the shelf, self-edits, the debug page |
| [Channels](channels.md) | The web page, the terminal client, Telegram, and what's planned |

## Reference

| Page | What's in it |
|---|---|
| [HTTP & event API](api.md) | Every route, the SSE event bus, the voice websocket |
| [The card format](card-format.md) | The V3 card YuriOS writes, the `yurios` block, and what is never on it |
| [Architecture](architecture.md) | The packages, the seams, the file layout, running the tests |
| [Troubleshooting](troubleshooting.md) | She's silent / she won't start / the room is black / turns fail |

## Background

- [White paper](whitepaper.md) — the design argument, at length.
- [`SPEC.md`](../SPEC.md) — the normative specification. Section numbers (`§7.6`) are stable and
  cited from the source.
- [`PROVENANCE.md`](../PROVENANCE.md) — where subsystems came from.
- [Building Agentic Waifus](https://yurios.org/book/index.html) — how she was built, chapter by
  chapter.
