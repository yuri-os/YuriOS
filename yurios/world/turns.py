"""Channel-agnostic text turns (SPEC §10.5) — the inbound half of the seam.

The voice route owns the *audio* turn: mic → STT → `TurnController` → TTS,
with barge-in and latency masking. Every other medium — the terminal, Telegram,
a plain HTTP caller, one day a game engine's NPC — is a **text** conversation,
and they all share this one runner. It is the YuriOS frontend rule
("user input becomes a `user_message` signal; frontends talk to the host,
never to the brain directly") made concrete: a channel hands text in here and
renders `message` events off the EventHub; it never touches the brain.

One turn, end to end, mirroring the voice route's forks minus the audio:

  - the user's line joins the transcript (`message` on the hub) and the
    SignalBus (`user_message` — the mind's ENGAGED preempt, SPEC §15.3);
  - `turn_started`/`turn_ended` bracket the turn (the mind knows she's talking);
  - brain tokens stream through the `EmotionParser`: tags drive the face on the
    puppet lane (`controller.set_expression`, voice fork #5) and are stripped
    from the shown text; completed sentences accumulate as a `draft` on the hub;
  - a clean turn persists the *verbatim* reply (tags kept, B2's corpus rule),
    commits the shown text as a `message`, and tees `turn_committed` onto the
    bus (the mind's REFLECT share: world model, promise extraction);
  - a mid-stream failure publishes `draft_cancel` and leaves **no trace** —
    no transcript entry, no persist, no signal (B2 §4.4's rule, kept).

Turns from every text channel serialise on one lock: the persist path and the
session store aren't concurrent-safe, and one companion holds one conversation
at a time. The voice route's turns are per-connection and already torn down
before a new one starts; text turns simply queue behind each other.
"""
from __future__ import annotations

import asyncio
import logging

from yurios.desktop.voice.emotion import EmotionParser
from yurios.desktop.voice.sentences import cut_sentences

log = logging.getLogger("world.turns")


class TextTurns:
    """The one text-turn runner every channel shares (built once, on Runtime)."""

    def __init__(self, rt):
        self.rt = rt
        self._lock = asyncio.Lock()

    async def run(self, text: str, *, channel: str,
                  session_id: str | None = None) -> dict:
        """Drive one text turn. Returns {"session_id": …, "message": entry}
        (`message` is None for an empty reply). Raises on a mid-stream brain
        failure — the caller decides how to surface it; nothing was committed."""
        rt = self.rt
        async with self._lock:
            session_id = rt.brain.resolve_session(session_id)
            rt.post_message("user", text, channel=channel)
            rt.signals.post("user_message", {"text": text}, source=channel)
            rt.turn_started()
            parser = EmotionParser(default=rt.cfg.expression_default)
            raw: list[str] = []          # model output verbatim (tags kept, for persist)
            shown: list[str] = []        # committed sentences, tags stripped
            buf = ""
            prev_events = 0
            try:
                async for token in rt.brain.stream_reply(session_id, text):
                    raw.append(token)
                    speakable = parser.push(token)
                    # a closed tag drives the face before the text after it
                    while len(parser.events) > prev_events:
                        rt.controller.set_expression(
                            parser.events[prev_events].expression, 1.0, reset_ms=0)
                        prev_events += 1
                    buf += speakable
                    done, buf = cut_sentences(buf)
                    for s in done:
                        shown.append(s)
                        rt.hub.publish("draft", {"text": " ".join(shown)})
                parser.finish()
                if buf.strip():
                    shown.append(buf.strip())
            except Exception:
                # a turn that didn't happen leaves no trace (B2 §4.4)
                rt.hub.publish("draft_cancel", {})
                log.exception("text turn failed mid-stream (channel %s)", channel)
                raise
            finally:
                rt.turn_ended()

            entry = None
            if shown:
                reply = " ".join(shown)
                entry = rt.post_message("assistant", reply, channel=channel)
                await rt.brain.persist(session_id, text, "".join(raw))
                rt.signals.post("turn_committed",
                                {"text": text, "reply": reply}, source=channel)
            return {"session_id": session_id, "message": entry}
