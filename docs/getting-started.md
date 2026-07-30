# Getting started

This page takes you from a clone to a companion who talks back. If you want the details of any
step, each section links to the page that has them.

## 1. Install

Linux, macOS, or Windows via WSL. The script installs system packages, `uv`, Node, the venv, her
Vault and the web build, then tells you what it wired up.

```bash
cd YuriOS
./install.sh                       # ~1.6 GB: everything .env.example selects, no CUDA
source .venv/bin/activate
```

That is the full install: her body, brain, memory, MCP tools, text chat **and her real voice**
(faster-whisper ears, the kokoro voice, silero turn-taking — all CPU-only). No flags and no
follow-up step; the script installs exactly what the `.env` it writes selects. Her voice models
download themselves the first time she speaks, and after that she is offline.

Want her without the voice stack? `./install.sh --thin` is a 280 MB text-only install. Everything
is additive and re-runnable, so you can add the voice later. See [Installation](installation.md)
for every flag, the extras table with measured sizes, and the manual step-by-step if you'd rather
no script touched your system.

## 2. Give her a brain

The one part that isn't pip-installable is the model. Any [LiteLLM](https://docs.litellm.ai/)
route works; the shipped `.env` points at a local **LM Studio** on `:1234` (Developer tab → Start
Server, or `lms server start`):

```bash
lms get HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive   # her thinking (chat + utility)
lms get text-embedding-nomic-embed-text-v1.5                  # her memory's embeddings
```

An uncensored model on purpose: she's a companion, not an assistant, and a refusal-trained model
plays her badly — it breaks character to decline, which is the one thing a person in the room
never does.

Prefer Ollama, OpenRouter, or something else? It's a one-line swap — see
[Models & connections](models.md).

## 3. Run her

```bash
python -m yurios.world             # → http://localhost:8768
```

That address opens the **character switchboard**. Select a card to enter her sanctuary; leaving
the room returns to the switchboard *without* stopping that character's background life.

On the first 0.2 start, an existing 0.1 install's `vault/`, `corpus/`, `traces/`, `tool-logs/`
and `selfies/` are copied into a registered `yuri` character before any mind wakes. The old
directories stay untouched as a backup. See [Characters](characters.md#migrating-from-01).

Wondering what's actually wired? `python -m yurios.doctor` reads your `.env` and says.

## 4. The first ten minutes

Choose a character, click **Enter**, click **enter the sanctuary**, then click **start listening**
(the mic button, bottom-left) to give the page your microphone — voice won't work until you do.
Now talk, or type to her in the chat column.

![The sanctuary in the browser](img/browser-mode.png)

Then try the loop end to end:

- **Drop a document** (`.md`/`.txt`) into her `vault/knowledge/reference/` — within a heartbeat
  she reads it, indexes it, journals "read and shelved …", and can answer from it *with a
  citation* (doc + character span), without it touching what she remembers about *you*.
- **Let her make a promise** — say "remind me to call mom tomorrow", or get an "I'll look into
  that" out of her. `cat data/characters/<id>/vault/goals.md`: it's there, with provenance
  (`promise:her-own-words`) and a due time. Come back the next day and she'll raise it — once, at
  a reasonable hour — or you'll find "thought about it; chose not to interrupt" in the journal.
- **Leave her alone overnight** — DORMANT ticks every 15 minutes, and in the small hours DREAM
  folds yesterday's journal into `memory/semantic/facts.md`. She wakes changed by yesterday.
- **Watch her think** — the **inner life** tab in the chat column shows her activity state and
  heartbeat, today's token budget, the goals on her mind, the shelf, edits waiting on your
  approval, and the journal of what she did while you were gone.

More on all of it in [The mind](mind.md).

## 5. Where to go next

- Put her on your desktop instead of in a tab → [Bodies](bodies.md#desktop-mode)
- Give her a second body → [Bodies](bodies.md#live2d)
- Talk to her without a GPU → [Bodies](bodies.md#the-text-room--no-body)
- Let her take selfies → [Selfies](selfies.md)
- Reach her from your phone → [Channels](channels.md#telegram)
- Add a second companion → [Characters](characters.md)
- Turn a dial → [Configuration](configuration.md)
