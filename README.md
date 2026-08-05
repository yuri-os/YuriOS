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

## What she is

| | |
|---|---|
| **[A body](docs/bodies.md)** | A VRM body in a procedural cyberpunk sanctuary, with visemes, gaze and expression — or a Live2D body, or just her, floating transparently on your desktop |
| **[A voice](docs/voice.md)** | A real-time loop with barge-in: faster-whisper ears, the kokoro voice, silero turn-taking — all CPU-only, all local |
| **[A mind](docs/mind.md)** | SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE, forever. Activity states, a budget governor, salience gates, DREAM consolidation, goals with provenance, gated self-edits |
| **[Hands](docs/tools.md)** | Tools over real MCP — timer, music, weather, `take_selfie`, and `show_picture` — behind an allowlist, rate limits and a JSONL audit of every call |
| **[A camera](docs/selfies.md)** | Selfies through a hosted route, or on your own GPU with an SDXL or Krea 2 checkpoint, from a composable template library |
| **[A house](docs/characters.md)** | Multiple companions, each with her own Vault, memory, models, bot and journal. Import SillyTavern V2/V3 cards; export her as one |
| **[Any medium](docs/channels.md)** | The web page, a terminal client, Telegram. One conversation, one event bus, thin views |

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
