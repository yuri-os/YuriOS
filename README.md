<div align="center">

<img src="docs/img/banner.jpg" alt="YuriOS" width="100%" />

# YuriOS

**A local-first companion that runs on your own machine.**

[yurios.org](https://yurios.org) · [Getting started](docs/getting-started.md) · [Documentation](docs/README.md)

</div>

## What it does

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
| **[Hands](docs/tools.md)** | Tools over real MCP — timer, music, `take_selfie`, `show_picture`, and the web hands `web_search` / `read_page` / `research` — behind an allowlist, rate limits and a JSONL audit of every call |
| **[A shelf](docs/mind.md)** | Drop a document in and she reads it — in notes if it's long. What she is reading, what it will cost in model calls, and a stop button that loses nothing, all on the inner-life tab |
| **[A camera](docs/selfies.md)** | Selfies through a hosted route, or on your own GPU with an SDXL, Krea 2 or Anima checkpoint, from a composable template library — and a [Checkpoints](#settings) page that finds her one on Civitai |
| **[Eyes](docs/models.md#can-she-see-pictures)** | Show her something: attach, paste or drop a picture in any room — or send one to her Telegram — whenever the model she runs on can take one. She keeps her voice on a picture turn, and the photo is asked about, not stored in her memory |
| **[A house](docs/characters.md)** | Multiple companions, each with her own Vault, memory, models, bot and journal. Import SillyTavern V2/V3 cards; export her as one |
| **[Any medium](docs/channels.md)** | The web page, a terminal client, Telegram. One conversation, one event bus, thin views |

## The three surfaces

**[The switchboard](docs/characters.md#the-switchboard)** is the front door: one tile per
companion, her portrait, the rung of the ladder she is actually on — `ENGAGED`, `IDLE`,
`DORMANT`, `DREAM`, in the mind loop's own words — and her four loop switches (`mind`,
`utility`, `dream`, `hands`) on the tile itself. One companion can be a fully autonomous mind
while the one beside her stays reactive-only. Enter her room, leave it again, and she keeps
running: her life doesn't depend on being looked at.

![The switchboard: two companions, their states and their mind / utility / dream switches.](docs/img/switchboard.png)

**[The card studio](docs/characters.md#the-card-studio)** is where a companion is written.
**Import** takes a SillyTavern V2/V3 card — an ordinary `.PNG` with the card JSON in a text
chunk — and files its wall of description into identity, history, appearance and manner. Every
imported card arrives *disabled*, even one that declares itself a YuriOS card: a card off the
internet does not get a mind, tools and a Telegram bot before you've read it. **Create
character** opens the same page on the shape of a working companion. There you write her
against a live per-field prompt budget, give her a face, and edit her
[selfie library](docs/selfies.md#a-library-of-her-own). **Export PNG** takes her back out as one
card that opens anywhere cards open — who she is travels; your name, her memory of you and her
journal stay on the machine.

![The card studio: editing Yuri's identity, with her face, prompt budget and what stays local.](docs/img/card-studio.png)

**[The mind debug page](docs/mind.md#the-mind-debug-page)** is the honest one. The inner-life
tab tells you what she did, in her words; this tells you *why*, in the machine's. Every record
she left is joined into one timeline: tick traces with the scores and the runners-up, every
context window any model was handed (self-talk, dreams and reach-outs, not just chat), the tool
audit with the photo each call produced, the Vault's git history, her recall index, and what the
day cost. It reads files rather than a running mind, so the companion who just crashed is the
one you can still take apart.

![The mind debug page: the tool-call audit, each call with the photo it produced.](docs/img/mind-debug.png)

## Experimental — and it can spend

This is a reference implementation of *initiative*, not a hardened product. The autonomous
half — the tick loop, DREAM, self-edits, the desk, the shelf, and above all the web hands — is
new and still moving. Expect rough edges, and meet it with a local model first.

**The reading is the expensive part.** `research` answers the conversation in milliseconds and
then keeps going on its own: searching, fetching pages, reading each one. One sentence said in
passing can become dozens to hundreds of model calls over half an hour. On a local model that is
fan noise. On a metered API it is a bill, and nothing in the loop stops to ask you first.

- **The web hands are off by default.** Until you set `SEARCH_BACKEND`, they are not advertised
  to her at all — she can't call what she can't see.
- **Her hands are on when you're not in the room.** `MIND_TOOLS_ENABLED=true` and
  `MIND_TOOL_ALLOWLIST=*` ship as the defaults. Before each autonomous call the guard checks the
  backend, budget pressure, a cooldown, her own rate limits and a hard daily cap
  (`MIND_TOOL_CALLS_PER_DAY`, 64). Narrow the allowlist — `yurios settings MIND_TOOL_ALLOWLIST`
  lists every hand — or flip the `hands` switch on her tile to revoke them before her next tick.
- **`MIND_DAILY_TOKENS` is a governor, not a cap.** It is an estimate that sheds her to
  `DORMANT`; it never gates conversation and does not abort a read in flight.
  `RESEARCH_MAX_CALLS` (100) bounds a single run.
- **The inner-life tab is the meter**: what she's reading, what it costs, and a stop button that
  keeps what she has read. Every call is in `tool-logs/calls.jsonl`.

If you point her at a paid API — chat, utility or embeddings — set a spend limit with the
provider as well. [The documentation](docs/README.md#experimental--and-it-can-spend) has every
guard and what it does and doesn't stop.

## Install

Supports Linux, macOS, and Windows through WSL.

```bash
cd YuriOS
./install.sh
yurios configure                      # choose her language model
yurios restart                        # load it (starts the daemon if it is stopped)
yurios status
```

Or, with nothing but Docker, `docker compose up -d --build` and open the link
`docker compose logs yurios` prints — see [Installation → Docker](docs/installation.md#docker).

A fresh install selects no model, so she can't talk until you choose one and restart. Then open
the address `yurios status` shows (normally `http://localhost:8768`). The dashboard offers the
same model choice if you open it first; restart after it too.

Use `yurios doctor` if something is not working.

## Settings

Everything in `.env` is editable from the browser as well as the terminal. **House settings** on
the switchboard sorts every knob into pages — Models, Memory, Mind, Hands, Web, Pictures, Voice,
Room & body, Channels, Access, System — with a search across all of them and a **Restart** button
for applying saves (when started with `yurios start`). It also edits the files `.env` only points
at (third-party MCP servers, the house selfie-scene overlay), and has a page per character for her
own model, switches, night jobs and selfie scenes. The gear in every room opens the same table as
a dialog.

```bash
yurios settings                       # the common knobs, and whatever you have changed
yurios settings --all                 # every one of them
yurios settings CHAT_THINKING=false   # …and a restart applies it
yurios character list                 # the house, from the same client
yurios chat yuri -m "hello"           # one turn, then exit
```

**Checkpoints** (House settings → Pictures → Checkpoints) is where her local camera's model comes
from. It lists Civitai's most-downloaded checkpoints for every family the camera can load, each
marked *Ready*, *Loads*, *Check* or *Not supported*, and recommends the file her camera computes
in. Downloads run on the server with live progress, resume if interrupted, and are header-checked
before **Use for her camera** points `SELFIE_LOCAL_MODEL` at them. It needs a Civitai API key and
shows SFW only by default — see
[Selfies → Choosing a checkpoint from Civitai](docs/selfies.md#choosing-a-checkpoint-from-civitai).

![The Checkpoints page: once a Civitai API key is in, Civitai's checkpoints for every family her camera loads, each card with its status and a Download button.](docs/img/checkpoints.png)

## From your phone

Bind her to the network and pair the device. The owner token is generated, applied and handed
over as a QR code, so nothing gets typed; the same button sits beside `OWNER_TOKEN` in House
settings.

```bash
yurios settings HOST=0.0.0.0
yurios restart
yurios pair                           # prints a link and a QR; scan it
```

For private HTTPS without a LAN bind, publish the loopback server with Tailscale Serve —
`tailscale serve --bg http://127.0.0.1:8768` — and open House settings through the resulting
`https://…ts.net` address; its QR keeps that origin (from the terminal,
`yurios pair --url https://…ts.net`). SSH and authenticated TLS reverse proxies work too — see
[Configuration → Server](docs/configuration.md#server).

## Learn more

- [Getting started](docs/getting-started.md)
- [Installation options](docs/installation.md)
- [Models and connections](docs/models.md)
- [Configuration](docs/configuration.md) and [the CLI](docs/cli.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Full documentation](docs/README.md)

<div align="center">

**The companion is yours. Not theirs.**

[yurios.org](https://yurios.org) · [Substack](https://yurios.substack.com) · [X @yuriosshell](https://x.com/yuriosshell)

</div>
