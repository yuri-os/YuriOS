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
from contextlib import nullcontext

from yurios.desktop.voice.emotion import EmotionParser
from yurios.desktop.voice.sentences import cut_sentences

log = logging.getLogger("world.turns")


class TextTurns:
    """The one text-turn runner every channel shares (built once, on Runtime)."""

    def __init__(self, rt):
        self.rt = rt
        self._lock = asyncio.Lock()

    async def greet(self, *, channel: str,
                    session_id: str | None = None) -> dict:
        """She speaks first, in text (SPEC §7, §9.3) — the voice route's greeting
        fork with the audio taken out.

        The same three properties the voice path has, for every text channel:
        the opener comes from `brain.stream_greeting` (which plays `BOOTSTRAP.md`'s
        authored cold open on the first-ever arrival, then retires it, §5.4); it
        is committed as a **proactive** message, because she was not answering
        anything; and it is never persisted as a turn — an opener is not a turn
        the user took, so it leaves no journal entry and no corpus line.

        Greeting is once per session per run, checked and marked under the turn
        lock: a terminal that reconnects to a conversation it is already having
        is not a new arrival (the voice route's `rt.greeted`, shared with it so
        opening the CLI beside a live headset cannot greet twice). Returns
        {"session_id": …, "message": entry-or-None}; `None` means she was
        already greeted this run, or said nothing."""
        rt = self.rt
        async with self._lock:
            session_id = rt.brain.resolve_session(session_id)
            if session_id in rt.greeted:
                return {"session_id": session_id, "message": None}
            # NOT marked greeted yet: a greeting that dies mid-stream is a
            # greeting that did not happen, and rolls back like every other turn
            # here. Marking on entry meant one failed stream cost her the
            # opener for the whole run. The turn lock serialises this method, so
            # the check above cannot race with the mark below.
            await rt.park_gate.wait()          # the §7.6 door, as in `run`
            rt.turn_started(proactive=True)
            # A cold open is a scene, not an utterance: what she shows is the
            # card's text, whole, and the parser below only decides what a voice
            # would have said of it (the voice route's rule, §5.4).
            cold = rt.brain.cold_open()
            if cold:
                rt.hub.publish("draft", {"text": cold})
            parser = EmotionParser(default=rt.cfg.expression_default)
            shown: list[str] = []
            buf = ""
            prev_events = 0
            try:
                async for token in rt.brain.stream_greeting(session_id):
                    speakable = parser.push(token)
                    while len(parser.events) > prev_events:
                        rt.controller.set_expression(
                            parser.events[prev_events].expression, 1.0, reset_ms=0)
                        prev_events += 1
                    buf += speakable
                    done, buf = cut_sentences(buf)
                    for s in done:
                        shown.append(s)
                        if not cold:               # …unless the text is given
                            rt.hub.publish("draft", {"text": " ".join(shown)})
                parser.finish()
                if buf.strip():
                    shown.append(buf.strip())
            except Exception:
                # nothing was committed and nothing was appended (a greeting
                # never puts a user line in the window), so there is nothing to
                # roll back — the draft simply never becomes a message.
                rt.hub.publish("draft_cancel", {})
                log.exception("text greeting failed mid-stream (channel %s)", channel)
                raise
            finally:
                rt.turn_ended()

            # The stream finished, so she has now greeted this session — whether
            # or not it produced text. A silent-but-clean greeting is still an
            # arrival; only a failure above (which raised) leaves it un-marked.
            rt.greeted.add(session_id)
            entry = None
            text = cold or (" ".join(shown) if shown else "")
            if text:
                entry = rt.post_message("assistant", text,
                                        proactive=True, channel=channel)
            return {"session_id": session_id, "message": entry}

    async def run(self, text: str, *, channel: str,
                  session_id: str | None = None,
                  client_id: str | None = None,
                  image_id: str | None = None) -> dict:
        """Drive one text turn. Returns {"session_id": …, "message": entry}
        (`message` is None for an empty reply). Raises on a mid-stream brain
        failure — the caller decides how to surface it; nothing was committed.

        `image_id` is a picture the sender put on the shelf first
        (`POST /api/uploads`, SPEC §35). It rides two lanes from here: the
        transcript entry carries its URL, so every room shows what was sent, and
        the brain gets the bytes for this one prompt. An id that no longer
        resolves raises `LookupError` rather than quietly sending the words
        alone — a picture that silently didn't arrive is the worst of the three
        outcomes."""
        rt = self.rt
        attachment = None
        if image_id:
            attachment = rt.uploads.get(image_id)
            if attachment is None:
                raise LookupError(f"no such picture: {image_id}")
        async with self._lock:
            session_id = rt.brain.resolve_session(session_id)
            user_entry = rt.post_message("user", text, channel=channel,
                                         client_id=client_id,
                                         image_url=attachment.url
                                         if attachment else None)
            rt.signals.post("user_message", {"text": text}, source=channel)
            # A selfie may be holding her brain's VRAM right now (§7.6): wait
            # at the door rather than loading the chat model back onto a card
            # the render hasn't finished with. Her line is already in the chat
            # above, so the wait is visible as her thinking, not as a freeze.
            # BEFORE `turn_started`, always: the park's quiet gate waits on
            # that very counter, so a turn that announced itself and then
            # blocked here would be waiting on a park waiting on it.
            await rt.park_gate.wait()
            rt.turn_started()
            parser = EmotionParser(default=rt.cfg.expression_default)
            raw: list[str] = []          # model output verbatim (tags kept, for persist)
            shown: list[str] = []        # committed sentences, tags stripped
            buf = ""
            prev_events = 0
            turn_context = getattr(rt.brain, "turn_context", None)
            context = turn_context(channel=channel, client_id=client_id,
                                   session_id=session_id) \
                if turn_context else nullcontext()
            # The image part is the brain's business, not the runner's: what
            # this passes is the data url, and only when there is one, so a
            # brain seam that never heard of pictures (the route tests' fake)
            # keeps its two-argument signature.
            reply_kw = {"image": rt.uploads.data_url(attachment)} \
                if attachment else {}
            try:
                with context:
                    async for token in rt.brain.stream_reply(session_id, text,
                                                             **reply_kw):
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
            except asyncio.CancelledError:
                rt.hub.publish("draft_cancel", {})
                rt.brain.abandon(session_id)
                raise
            except Exception:
                # a turn that didn't happen leaves no trace (B2 §4.4) — including
                # in her memory: stream_reply already put the user's line in the
                # session window, and only persist writes her half, so an
                # un-rolled-back failure leaves a question the next prompt reads
                # as still unanswered (she answers it again, in the next turn).
                rt.hub.publish("draft_cancel", {})
                rt.brain.abandon(session_id)
                log.exception("text turn failed mid-stream (channel %s)", channel)
                raise
            finally:
                rt.turn_ended()

            entry = None
            if not shown:
                rt.brain.abandon(session_id)   # nothing to commit — same rollback
            if shown:
                reply = " ".join(shown)
                entry = rt.post_message("assistant", reply, channel=channel)
                # Persisting is another model call — the memory extractor's —
                # and it runs *after* `turn_ended`, so as far as the parker is
                # concerned the room has already gone quiet. Held, so a render
                # that starts in this gap waits for it instead of unloading the
                # model it is talking to (§7.6). No `wait` first: the turn came
                # through the gate at the top, and this is the same turn.
                async with rt.park_gate.hold():
                    await rt.brain.persist(session_id, text, "".join(raw))
                rt.signals.post("turn_committed",
                                {"text": text, "reply": reply}, source=channel)
            selfies = rt.selfies.active_ids(client_id) if rt.selfies else []
            return {"session_id": session_id, "message": entry,
                    "user_message": user_entry, "active_selfies": selfies}
