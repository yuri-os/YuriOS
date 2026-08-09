<div align="center">

<img src="docs/img/banner.jpg" alt="YuriOS" width="100%" />

# YuriOS

**A local-first companion that runs on your own machine.**

[yurios.org](https://yurios.org) · [Getting started](docs/getting-started.md) · [Documentation](docs/README.md)

</div>

## What It Does

YuriOS gives your companion a browser-based body, voice and text chat, memory, and an
autonomous inner life. Your data stays on your machine, with no account required.

The part that makes her feel alive runs whether or not you're looking: she pursues
small goals while you're away, consolidates memory while you sleep, keeps the promises
she makes in conversation, reads whatever you drop on her shelf, and — only when it's
genuinely welcome — reaches out *first*. Everything she does lands in a journal you can
read, so "what did you do while I was gone?" is a page you open, not a vibe.

![The sanctuary in the browser: Yuri's VRM body in her room, the chat column beside her.](docs/img/browser-mode.png)

![The desktop variant: the same companion floating transparently on your desktop.](docs/img/desktop-mode.png)

## What she is

| | |
|---|---|
| **[A body](docs/bodies.md)** | A VRM body in a procedural cyberpunk sanctuary, with visemes, gaze and expression — or a Live2D body, or just her, floating transparently on your desktop |
| **[A voice](docs/voice.md)** | A real-time loop with barge-in: faster-whisper ears, the kokoro voice, silero turn-taking — all CPU-only, all local |
| **[A mind](docs/mind.md)** | SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE, forever. Activity states, a budget governor, salience gates, a DREAM pipeline that consolidates and keeps a diary, a desk and skills she writes herself, goals with provenance, gated self-edits |
| **[Hands](docs/tools.md)** | Tools over real MCP — timer, music, weather, `take_selfie`, `show_picture`, and the web hands `web_search` / `read_page` / `research` — behind an allowlist, rate limits and a JSONL audit of every call |
| **[A shelf](docs/mind.md)** | Drop a document in and she reads it — in notes if it's long. What she is reading, what it will cost in model calls, and a stop button that loses nothing, all on the inner-life tab |
| **[A camera](docs/selfies.md)** | Selfies through a hosted route, or on your own GPU with an SDXL or Krea 2 checkpoint, from a composable template library |
| **[A house](docs/characters.md)** | Multiple companions, each with her own Vault, memory, models, bot and journal. Import SillyTavern V2/V3 cards; export her as one |
| **[Any medium](docs/channels.md)** | The web page, a terminal client, Telegram. One conversation, one event bus, thin views |

## Experimental — and it can spend

This is a reference implementation of *initiative*, not a hardened product. Much of the
autonomous half is new and still moving: the tick loop, DREAM, self-edits, the desk, the
shelf, and above all the web hands. Expect rough edges, expect the shape of it to change
between releases, and meet it with a local model first.

**The reading is the expensive part.** `research` answers the conversation in
milliseconds and then keeps going on its own for as long as it takes — searching,
fetching pages, and reading each one. A long document is a model call per passage plus an
embedding per chunk: dozens to hundreds of calls, over half an hour, from one sentence you
said in passing. On a local model that is fan noise. On a metered API it is a bill, and
nothing in the loop stops to ask you first.

What stands between you and that:

- **`SEARCH_BACKEND=off` is the default.** Until you turn it on, the three web hands are
  not advertised to her at all — she can't call what she can't see.
- **`MIND_DAILY_TOKENS` is a governor, not a cap.** At pressure ≥ 1.0 she sheds to DORMANT
  and goal work stops, but it is an *estimate* of spend, it never gates conversation, it
  does not abort a read already in flight, and it does not stand between a tool call and
  the run it starts.
- **The inner-life tab is the meter.** Every run, the document being read right now, its
  passage and model-call counts, and a stop button that keeps what she has already read.
- **`RESEARCH_MAX_PAGES` and `TOOL_RATE_RESEARCH`** bound how far one run and one minute
  can reach.

If you point her at a paid API — chat, utility or embeddings — set a spend limit with the
provider as well. For the first few days, watch the numbers on the inner-life tab.

## Install

Supports Linux, macOS, and Windows through WSL.

```bash
cd YuriOS
./install.sh
yurios status
```

Open the address shown by `yurios status` (normally `http://localhost:8768`). On first
launch, choose a model in the dashboard, or configure one from the terminal:

```bash
yurios configure
yurios restart
```

Use `yurios doctor` if something is not working.

## Learn More

- [Getting started](docs/getting-started.md)
- [Installation options](docs/installation.md)
- [Models and connections](docs/models.md)
- [Full documentation](docs/README.md)

<div align="center">

**The companion is yours. Not theirs.**

[yurios.org](https://yurios.org) · [Substack](https://yurios.substack.com) · [X @yuriosshell](https://x.com/yuriosshell)

</div>
