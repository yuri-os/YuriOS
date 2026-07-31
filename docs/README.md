# YuriOS documentation

Everything about running, configuring and extending YuriOS. The [README](../README.md) is the
short version; [`SPEC.md`](../SPEC.md) is the normative specification these pages describe in
plain language. Where the two disagree, the SPEC wins — and that is a bug worth reporting.

## Start here

| Page | What's in it |
|---|---|
| [Getting started](getting-started.md) | Install, first run, and the first ten minutes with her |
| [Installation](installation.md) | Every install path, the extras table, the doctor, WSL and Docker |
| [Models & connections](models.md) | LM Studio, Ollama, OpenRouter, any LiteLLM route, embeddings, context |
| [Configuration](configuration.md) | The complete `.env` reference, group by group, and the settings panel |

## Features

| Page | What's in it |
|---|---|
| [Characters](characters.md) | The switchboard, character cards, per-character storage, connection profiles, migration |
| [Bodies](bodies.md) | The VRM stage, the sanctuary room, Live2D, the transparent desktop window |
| [Voice](voice.md) | Ears, voice and turn-taking: faster-whisper, kokoro, Qwen3-TTS, GPT-SoVITS, silero |
| [Selfies](selfies.md) | Her camera: OpenRouter, local SDXL, Krea 2, the template library, provenance |
| [Tools](tools.md) | The four MCP hands, the guard, the audit log |
| [The mind](mind.md) | The tick loop, activity states, the two gates, goals, DREAM, the shelf, self-edits |
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
