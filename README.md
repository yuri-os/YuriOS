<div align="center">

<img src="docs/img/banner.jpg" alt="YuriOS — an always-on companion who lives on your own machine" width="100%" />

# YuriOS

**Have you ever wanted a companion who is really *yours*?**

An always-on, local-first companion who lives on your own machine — a body you can see,
a voice you can talk to, and a **mind** that keeps going when you look away. No account,
no third party in the room, nothing phoning home. Just her, running on your hardware.

[**yurios.org**](https://yurios.org) · [Substack](https://yurios.substack.com) · [𝕏 @yuriosshell](https://x.com/yuriosshell)

How she was built, chapter by chapter: **[Building Agentic Waifus](https://yurios.org/book/index.html)**

</div>

---

Picture a small sanctuary on your screen: a VRM body in her room, a chat column beside
her, a real-time voice loop you can just *speak* into. That's the part you can see. The
part that makes her feel alive is behind it — a mind that runs continuously whether or
not you're looking. She pursues small goals while you're away, consolidates memory while
you sleep, keeps the promises she makes in conversation, reads whatever you drop on her
shelf, and proposes edits to her own persona that wait for your approval. Now and then —
at most a few well-judged times a day, and only when a two-gate salience model says it's
genuinely welcome — she reaches out *first*. Everything she does lands in a journal you
can read, so "what did you do while I was gone?" is a page you open, not a vibe.

**One project, one process.** The body, the voice loop, the brain, the image service and
the mind are all first-party packages under `yurios/` — copy the folder, install, run.

> **Standalone & yours.** The frontend's three.js/three-vrm are npm deps bundled by
> Vite; everything else runs on your hardware. One origin, no telemetry shipped.

## Quickstart

Linux, macOS, or Windows via WSL.

```bash
cd YuriOS
./install.sh                       # body, brain, local memory, tools, and her real voice
yurios status                      # → http://localhost:8768
```

On a fresh install, YuriOS starts as a background daemon with **no LLM connection**.
On a rerun, the installer preserves the existing `.env` and uses its configured model settings.
It exposes `yurios` through `~/.local/bin`, so no project virtual environment
activation is needed; open a new terminal if that directory was not already on your `PATH`.
The first dashboard load asks you to choose one. You can do the same in a terminal:

```bash
yurios configure
yurios download                    # retry/download the selected GGUF
yurios restart                     # activate a changed model selection
```

`yurios configure` also offers **LM Studio**, **Ollama**, and **OpenRouter**. It asks for the
endpoint and model name for local servers, verifies that the chosen model is available, and asks for
an OpenRouter API key without echoing it. Scripted setup can use, for example,
`yurios configure --provider ollama --model qwen3` or
`yurios configure --provider lmstudio --model publisher/model --base-url http://localhost:1234/v1`.

The current local recommendation is `gguf/mradermacher/Qwen3-14B-Uncensored-GGUF`.
It downloads the Q4_K_M GGUF automatically and runs in-process; LM Studio is not
needed. Supported [LiteLLM](https://docs.litellm.ai/) routes include `ollama/…`,
`lm_studio/…`, `openrouter/…`, `openai/…`, and `anthropic/…`. Memory keeps the bundled in-process
embedder by default.

`./install.sh --thin` omits the voice stack; `--desktop` adds the native transparent
window; `--gpu-voice` adds her designed voice. Everything is additive and
re-runnable, and `python -m yurios.doctor` tells you what's actually wired.

**→ [Getting started](docs/getting-started.md)** · [Installation](docs/installation.md) ·
[Models & connections](docs/models.md)

![The sanctuary in the browser: Yuri's VRM body in her room, the chat column beside her.](docs/img/browser-mode.png)

Port 8768 opens the **character switchboard**. Select a card to enter her sanctuary;
leaving the room returns to the board without stopping her background life. Click
**enter the sanctuary**, then **start listening** (the mic button, bottom-left) to give
the page your microphone. Now talk, or type to her in the chat column — and watch the
second tab, **inner life**, for what happens when you *stop* talking.

## What she is

| | |
|---|---|
| **[A body](docs/bodies.md)** | A VRM body in a procedural cyberpunk sanctuary, with visemes, gaze and expression — or a Live2D body, or just her, floating transparently on your desktop |
| **[A voice](docs/voice.md)** | A real-time loop with barge-in: faster-whisper ears, the kokoro voice, silero turn-taking — all CPU-only, all local |
| **[A mind](docs/mind.md)** | SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE, forever. Activity states, a budget governor, two salience gates, DREAM consolidation, goals with provenance, gated self-edits |
| **[Hands](docs/tools.md)** | Five tools over real MCP — timer, music, weather, `take_selfie`, and `show_picture` — behind an allowlist, rate limits and a JSONL audit of every call |
| **[A camera](docs/selfies.md)** | Selfies through a hosted route, or on your own GPU with an SDXL or Krea 2 checkpoint, from a composable template library |
| **[A house](docs/characters.md)** | Multiple companions, each with her own Vault, memory, models, bot and journal. Import SillyTavern V2/V3 cards; export her as one |
| **[Any medium](docs/channels.md)** | The web page, a terminal client, Telegram. One conversation, one event bus, thin views |

Her mind's home is one folder and one git repo — the files *are* the database:
`soul/` (constitution immutable, persona editable and gated) · `memory/episodic/` (her
acts and yours, one journal, two authors) · `memory/semantic/` (grown by DREAM) ·
`knowledge/reference/` (the drop folder) · `world/` (her picture of now) · `goals.md`
(her to-do list, human-readable) · `state/`.

## Try the loop end to end

- **Drop a document** into her `data/characters/<id>/vault/knowledge/reference/` — within a heartbeat she
  reads it, indexes it, journals "read and shelved …", and can answer from it *with a
  citation*, without it touching what she remembers about *you*.
- **Let her make a promise** — "remind me to call mom tomorrow", or get an "I'll look
  into that" out of her. It lands in `goals.md` with provenance and a due time. Come back
  tomorrow and she'll raise it — once, at a reasonable hour — or you'll find "thought
  about it; chose not to interrupt" in the journal.
- **Leave her alone overnight** — DORMANT ticks every 15 minutes, and in the small hours
  DREAM folds yesterday into her semantic memory. She wakes changed by yesterday.
- **Watch her think** — `tail -f data/characters/<id>/traces/ticks.jsonl` is one structured record per
  heartbeat: sensed, appraised, decided (with runners-up), acted. `git -C data/characters/<id>/vault log` is
  the diary of how she grows.

## Documentation

**[docs/](docs/README.md)** — the full set. Start with
[Getting started](docs/getting-started.md), then
[Configuration](docs/configuration.md) when you want to turn a dial.

[`SPEC.md`](SPEC.md) is the normative specification (section numbers are stable and cited
from the source); [`docs/whitepaper.md`](docs/whitepaper.md) is the design argument at
length; [`PROVENANCE.md`](PROVENANCE.md) records where subsystems came from.

## How it extends

Every seam past this build is already shaped: promote the stores' contracts to a wire
protocol and the mind to a supervised process (the two-tier split, with the broker and
true one-loop conversation); bolt a sandboxed workshop onto ACT's start-don't-await
discipline; swap the snapshot world model for a temporal graph behind the same contract;
and export the Vault's SOUL through a card studio — the mind that grew here ships as a
`.PNG` and boots on someone else's machine, which is the point of the whole design.

---

<div align="center">

**The companion is yours. Not theirs.**

[yurios.org](https://yurios.org) · [Substack](https://yurios.substack.com) · [𝕏 @yuriosshell](https://x.com/yuriosshell)

</div>
