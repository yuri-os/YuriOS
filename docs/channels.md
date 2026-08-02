# Channels

The sanctuary page is one frontend, not the only one. Every medium shows the same one
conversation, because a frontend is a thin view: input becomes a text turn, output is rendered
off the one event bus, and nothing talks to the brain directly.

Two seams make any medium a frontend — a shared text-turn runner inbound, and an `EventHub`
subscription outbound. Committed messages carry the channel they came from, so an adapter can
filter its own echoes. And because the mind's proactive lines land as `proactive` messages on that
same bus, **every channel receives her initiative for free**.

A failed channel is one degraded medium, never a down host. `/api/health` and the boot board say
which are up.

## The web page

The default: `http://localhost:8768/`. See [Bodies](bodies.md).

## The terminal

```bash
python -m yurios.chat                              # against a running server
python -m yurios.chat --url http://192.168.1.5:8768
python -m yurios.chat --character mika             # enter a card directly
python -m yurios.chat --model ollama/qwen3:8b       # change her reply model
python -m yurios.chat --new                        # start a fresh conversation window
```

A remote thin client over `POST /api/chat` and `/api/events`. On a multi-character host it shows
the available cards at launch; pass `--character <id>` to skip the picker, or use `/cards` and
`/use <id>` while chatting. Each card keeps its own conversation session. `/model <id>` and
`/utility <id>` change the selected card's models live; use `reset` to inherit the host default.
Its SSE attach counts as presence, exactly like an open page — so she knows you're there. The
conversation window survives across runs unless you pass `--new`.

She speaks first here too: the terminal asks for the greeting on arrival (`POST /api/greeting`),
so you get the same opener a headset would. Reattaching to a conversation already going stays
quiet — the greeting is once per session per server run. The very first time you meet a character
this is where her cold open plays.

## Telegram

She's in your pocket: messages sent to the bot are ordinary turns, and their replies land back in
that Telegram chat, selfies included.

### Setup

1. Make a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Put it in `.env` (or paste it into the gear panel in her room):

   ```ini
   TELEGRAM_BOT_TOKEN=123456:ABC…
   TELEGRAM_CHAT_ID=
   ```
3. Restart, then message the bot once. It replies with the `TELEGRAM_CHAT_ID` to set.
4. Set that id and restart again.

A channel is **on when its credentials are set** — there's no separate enable flag.

With `TELEGRAM_CHAT_ID` unset she's in **pairing mode**: the bot answers with the id to configure
and processes nothing else. Once it's set she binds to exactly one chat, and strangers are
ignored.

Telegram is *reachable, not present*: it posts no presence signals, so a message there doesn't
make her think you're in the room. Selfies are sent as the file itself.

### One bot, one character

Telegram's `getUpdates` is exclusive per token. Two companions sharing a bot would fight over its
updates forever and neither would be reachable — Telegram answers all but the last poller with
*"Conflict: terminated by other getUpdates request"*.

So each companion gets her own @BotFather bot, named with her registry id upper-cased:

```ini
TELEGRAM_BOT_TOKEN_MIA=…
TELEGRAM_CHAT_ID_MIA=…
```

The short way is the **gear in her own room** — the settings panel shows *her* pair and nobody
else's, so pasting a token there can never take over another companion's chat.

The unsuffixed pair stays the first companion's. Once there are others, say who keeps it:

```ini
TELEGRAM_CHARACTER=yuri
```

With that unset, the shared pair is offered to every character without her own bot and the first
runtime to start holds it; the rest report the medium as `held by <her>`, which is a healthy state
rather than a fault. A companion with no bot of her own is simply not on Telegram — she's still in
the room and the terminal.

### Cross-chat forwarding

Replies stay in the chat where they originated by default. A web, voice, CLI or API conversation
is not copied into Telegram, and the browser rooms do not show a Telegram button. To deliberately
mirror those replies into Telegram, enable this in the settings panel and restart:

```ini
TELEGRAM_SEND_NON_TELEGRAM=true
```

Telegram-originated turns always receive their reply in Telegram regardless of this setting.

## Planned

Two more mediums are shaped on the same seam (`yurios/world/channels/base.py`) and not yet
implemented:

- **WhatsApp**, over a webhook transport.
- **A game-engine NPC API** — a websocket the engine connects to: player utterances in as text
  turns with scene context, `message` events out as dialogue, and the same avatar/expression
  events as animation cues. A game is another frontend and effector set, never a second brain.

## Writing your own

Implement `yurios/world/channels/base.py`'s `Channel`. Inbound, hand text to the shared turn
runner (or just `POST /api/chat` if you're out of process); outbound, subscribe to the event bus
and render `message` events, filtering the ones whose `channel` is your own.

A channel may declare a `claim` on credentials that can only be held once — the manager gives it
to the first runtime to start and reports the medium as held for the rest.
