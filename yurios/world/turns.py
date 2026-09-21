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
    from the shown text; the clean text accumulates token by token with its line
    breaks kept (§10.5) — a text is shown as it was written — and goes out as a
    `draft` on the hub a sentence or a line at a time (`_Drafts`). The parser
    runs here with `strip_narration=False`: dropping `*she leans in*` is a rule
    about what reaches TTS, and on the page it took words out of the middle of
    her sentences (§9.7);
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
import re
from contextlib import nullcontext

from yurios.desktop.voice.emotion import EmotionParser

log = logging.getLogger("world.turns")


def _text_of(chunks: list[str]) -> str:
    """The shown text: line breaks kept, the gap a stripped tag leaves closed.

    `[happy] Hey. [tender] I missed you.` shows as `Hey. I missed you.` — the
    parser drops the tag and both spaces around it survive; runs of spaces
    collapse, and a line never starts or ends with one. Newlines stay: a
    text is shown as it was written (§10.5).

    The gap closes vertically too, and for the same reason. Some of what the
    parsers strip is a *whole line* — `*she reaches for her desk*`, a bracketed
    stage direction like `[warm, settling in]`, a `[[append_note {…}]]` marker
    the tool loop consumed — and what is left of that line is its two newlines.
    Three or four of those in a row is the dead space she never wrote: the model
    put a beat there, not a hole. So a run of blank lines collapses to one,
    which is the paragraph break it meant and the only one markdown renders
    anyway. Single breaks are untouched — three lines are still three bubbles."""
    text = re.sub(r"[ \t]{2,}", " ", "".join(chunks))
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


#: Sentence enders and the line break. Cadence only — this decides when the room
#: gets a repaint, never what it shows — so `Dr.` flushing early costs one event
#: and nothing else. That is why it is this and not the sentence cutter.
_BOUNDARY = re.compile(r"[.!?…\n]")

#: How much unpublished text may pile up before a draft goes out regardless.
#: Prose that offers no boundary — a URL, a code block, a model that forgot its
#: punctuation — still streams instead of arriving whole at commit.
DRAFT_FLUSH_CHARS = 120


class _Drafts:
    """The draft cadence: what the room sees while she is still typing (§10.5).

    Every `draft` carries the whole reply so far, not a delta, so one per token
    sends the text again for every token — quadratic bytes on a bus whose queues
    are bounded at 256 and *drop* when full (`kernel/hub.py`). The drop does not
    land politely on drafts: it lands on whatever is published next, which at the
    end of a turn is her committed `message`. A long reply to a backgrounded tab
    could stream and then never commit, logged at debug and nowhere else. It is
    one bus per subscriber, so every open tab, the CLI and the dashboard each pay
    it.

    So the tokens accumulate one by one — that is what keeps her line breaks —
    and a draft goes out on a sentence, a line break, or once the unpublished
    tail reaches `DRAFT_FLUSH_CHARS`. Roughly one event per line or sentence,
    which is the cadence a reader perceives anyway.
    """

    def __init__(self, hub, *, enabled: bool = True):
        self.hub = hub
        self.enabled = enabled          # a cold open shows its own text instead
        self.shown: list[str] = []      # clean text, tags stripped, breaks kept
        self._unpublished = 0

    def push(self, speakable: str) -> None:
        """Accumulate one token's shown text, publishing if it closes a beat."""
        self.shown.append(speakable)
        self._unpublished += len(speakable)
        if _BOUNDARY.search(speakable) or self._unpublished >= DRAFT_FLUSH_CHARS:
            self.flush()

    def flush(self) -> None:
        """Publish the tail if anything is unpublished. Called once more when the
        stream ends, so the room never sits on a draft cut mid-sentence while the
        commit waits behind a memory-extractor call."""
        if self._unpublished and self.enabled:
            self.hub.publish("draft", {"text": self.text})
        self._unpublished = 0

    @property
    def text(self) -> str:
        return _text_of(self.shown)


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
            # A new session_id is not proof of arrival: an SSE flap or a
            # switch of rooms mints one while they never left (§9.8).
            if rt.still_in_the_room():
                rt.greeted.add(session_id)
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
            # A text turn keeps her narration: `*she leans in*` is how she
            # shows a feeling in writing, and §9.7 already keeps the
            # spoken-style block off this prompt for that reason. Only TTS
            # strips it — there it would be read aloud.
            parser = EmotionParser(default=rt.cfg.expression_default,
                                   strip_narration=False)
            drafts = _Drafts(rt.hub, enabled=not cold)   # …unless the text is given
            prev_events = 0
            try:
                async for token in rt.brain.stream_greeting(session_id):
                    speakable = parser.push(token)
                    while len(parser.events) > prev_events:
                        rt.controller.set_expression(
                            parser.events[prev_events].expression, 1.0, reset_ms=0)
                        prev_events += 1
                    if speakable:
                        drafts.push(speakable)
                tail = parser.finish()
                if tail:
                    drafts.shown.append(tail)
                drafts.flush()
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
            text = cold or drafts.text
            if text:
                entry = rt.post_message("assistant", text,
                                        proactive=True, channel=channel,
                                        session_id=session_id)
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
                                         session_id=session_id,
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
            # A text turn keeps her narration: `*she leans in*` is how she
            # shows a feeling in writing, and §9.7 already keeps the
            # spoken-style block off this prompt for that reason. Only TTS
            # strips it — there it would be read aloud.
            parser = EmotionParser(default=rt.cfg.expression_default,
                                   strip_narration=False)
            raw: list[str] = []          # model output verbatim (tags kept, for persist)
            drafts = _Drafts(rt.hub)     # clean text, tags stripped, line breaks kept
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
                        if speakable:
                            drafts.push(speakable)
                    tail = parser.finish()
                    if tail:
                        drafts.shown.append(tail)
                    drafts.flush()
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
            reply = drafts.text
            if not reply:
                rt.brain.abandon(session_id)   # nothing to commit — same rollback
            if reply:
                entry = rt.post_message("assistant", reply, channel=channel,
                                        session_id=session_id)
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
