"""The channel contract (SPEC §10.5) — how a new medium plugs in.

A channel is an in-process adapter between one outside medium (Telegram, one
day WhatsApp, a game engine) and the two seams every conversation already
rides:

  - **inbound**: hand user text to `rt.turns.run(text, channel=self.name)` —
    which does the whole turn (transcript, `user_message` signal, the brain,
    persist, `turn_committed`). A channel never touches the brain, the Vault,
    or the mind directly; it is a thin view, exactly like the web page.
  - **outbound**: subscribe `rt.hub` and render the `message` events it cares
    about. Her replies, greetings, and the mind's proactive reach-outs
    (`proactive: true` — a SUGGEST line, a SPEAK that found no page open) all
    arrive on that one bus, so a channel gets her *initiative* for free.

What a channel decides for itself is presence semantics: an attached terminal
is "someone is in the room" (`user_present` on the SignalBus, like an open
page); an asynchronous inbox like Telegram is *reachable, not present* — it
posts no presence, and the `user_message` signal a real message produces is
what wakes the mind.

Planned adapters on this same contract (not yet implemented):

  - **WhatsApp** — the Telegram shape with a different transport (Cloud API
    webhooks instead of long polling; a webhook needs a public URL, so it will
    arrive together with an opt-in tunnel/ingress story).
  - **Game-engine NPC API** — a WebSocket the engine (Unity/Unreal/Godot)
    connects to, presenting her as an NPC: player utterances come in as text
    turns (plus scene context the engine supplies), and the outbound render is
    richer than text — `message` events become dialogue lines, and the same
    `avatar`/expression events the VRM stage consumes become animation cues.
    The research spec's rule holds: a game is just another frontend + effector
    set on the same wire shapes, never a second brain.
"""
from __future__ import annotations


class Channel:
    """One outside medium. Subclasses set `name` and implement start/stop."""

    name: str = "channel"

    async def start(self, rt) -> str:
        """Bring the channel up on the server's event loop. Returns a short
        human detail for the boot panel (e.g. "@yuri_bot"). Raise to mark the
        channel failed — the rest of the host is unaffected."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Tear down tasks and connections. Must be idempotent."""
        raise NotImplementedError
