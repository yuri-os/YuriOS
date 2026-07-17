"""The real-time loop (SPEC §4) — the one genuinely new thing in Build #2.

Build #1 is the brain; this is the loop that feeds it speech and renders it as
voice + motion. Everything hard about a voice companion lives in two disciplines,
and both are in this file (ch. 32):

  1. **Stream every stage into the next.** Reply tokens are pulled from the brain
     while the last sentence is still being synthesized; the first audio chunk
     goes out the instant sentence one renders, not when the reply finishes. A
     producer coroutine drains the brain into a sentence queue; the main loop
     synthesizes and emits — so the LLM writing sentence two overlaps the TTS of
     sentence one (the §4.2 trap).

  2. **Barge-in is a pipeline cancel, not a pause.** When the user talks over her,
     `cancel()` must tear down TTS playback *and* the in-flight generation
     together — or she keeps talking with words she already committed to. Here
     that is one `asyncio.Event`: setting it breaks the producer's `async for`
     (which `aclose()`s the brain's token stream — generation aborts) and stops
     the emit loop. A barged-in turn writes **no** corpus and **no** commit,
     exactly like Build #1's mid-stream failure: a turn that didn't happen leaves
     no trace (SPEC §4.4).

The controller is transport-agnostic: it yields `OutEvent`s and the websocket
route (or a test) serializes them. That seam is why `test_turn_bargein.py` can
drive the whole loop with fake backends and assert the cancel reached the brain.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from .emotion import EmotionParser
from .fillers import FillerBank
from .latency import TurnTrace
from .protocols import TTS, AudioChunk, ReplyBrain
from .sentences import cut_sentences

log = logging.getLogger("desktop.turn")


@dataclass
class OutEvent:
    """One thing to send to the client. `kind` picks the payload field."""
    kind: str                       # filler | expression | audio | caption | done | cancelled | error
    expression: str | None = None
    audio: AudioChunk | None = None
    text: str | None = None
    detail: dict | None = None

    @staticmethod
    def filler(chunk: AudioChunk) -> "OutEvent":
        return OutEvent("filler", audio=chunk, text=chunk.text)

    @staticmethod
    def expr(name: str) -> "OutEvent":
        return OutEvent("expression", expression=name)

    @staticmethod
    def say(chunk: AudioChunk) -> "OutEvent":
        return OutEvent("audio", audio=chunk, text=chunk.text)


@dataclass
class TurnController:
    """One instance per session; `run_turn` is one utterance, `cancel` is barge-in."""
    brain: ReplyBrain
    tts: TTS
    filler_bank: FillerBank | None = None
    expression_default: str = "neutral"
    mask_latency: bool = True
    max_sentence_chars: int = 240   # flush an over-long run even without a terminator
    trace_dir: Path | None = None

    _cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        """Barge-in. Idempotent; safe to call from the mic handler at any instant."""
        self._cancel.set()

    async def run_turn(self, session_id: str, text: str,
                       trace: TurnTrace | None = None,
                       persist: bool = True,
                       tokens: AsyncIterator[str] | None = None) -> AsyncIterator[OutEvent]:
        """Drive one turn end to end. Caller has already endpointed + transcribed.

        `persist=False` + a `tokens` override is the greeting path (SPEC §7): she
        speaks first from `brain.stream_greeting`, but an opener is not a turn the
        user took, so it is not remembered as one. With no override, tokens come
        from `brain.stream_reply` — the normal turn."""
        self._cancel = asyncio.Event()      # fresh cancel token per turn
        trace = trace or TurnTrace()
        trace.mark("endpoint")

        # --- §5: mask the first-audio gap with an instant acknowledgment ---
        if self.mask_latency and self.filler_bank is not None and not self._cancel.is_set():
            clip = self.filler_bank.pick()
            if clip is not None:
                trace.masked = True
                yield OutEvent.filler(clip)

        parser = EmotionParser(default=self.expression_default)
        sentence_q: asyncio.Queue = asyncio.Queue()
        raw_reply: list[str] = []           # model output verbatim (tags kept, for the corpus)
        source = tokens if tokens is not None else self.brain.stream_reply(session_id, text)

        async def produce() -> None:
            """Drain brain tokens → expression events + sentences onto the queue."""
            buf = ""
            prev_events = 0
            try:
                async for token in source:
                    if self._cancel.is_set():
                        break
                    trace.mark("first_token")
                    raw_reply.append(token)
                    speakable = parser.push(token)
                    # a closed tag precedes the text after it: emit expr first
                    while len(parser.events) > prev_events:
                        await sentence_q.put(("expr", parser.events[prev_events].expression))
                        prev_events += 1
                    buf += speakable
                    done, buf = cut_sentences(buf)
                    for s in done:
                        await sentence_q.put(("say", s))
                    if len(buf) > self.max_sentence_chars:  # runaway with no terminator
                        await sentence_q.put(("say", buf.strip()))
                        buf = ""
                parser.finish()
                if buf.strip() and not self._cancel.is_set():
                    await sentence_q.put(("say", buf.strip()))
            except Exception as e:                      # brain blew up mid-stream
                await sentence_q.put(("error", str(e)))
            finally:
                await sentence_q.put(None)              # sentinel: producer done

        producer = asyncio.create_task(produce())
        errored: str | None = None
        try:
            while True:
                item = await sentence_q.get()
                if item is None or self._cancel.is_set():
                    break
                kind, payload = item
                if kind == "error":
                    errored = payload
                    break
                if kind == "expr":
                    yield OutEvent.expr(payload)         # face leads the voice
                    continue
                # kind == "say": synthesize this sentence (off the event loop) and
                # emit its audio. The producer keeps pulling tokens meanwhile —
                # sentence two is written while sentence one is spoken (§4.2).
                for chunk in await asyncio.to_thread(self._synth, payload):
                    if self._cancel.is_set():
                        break
                    trace.mark("first_audio")
                    yield OutEvent.say(chunk)
        finally:
            if not producer.done():                     # barge-in path: stop the brain
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

        # --- close out: error / barge-in write nothing; a clean turn persists ---
        if errored is not None:
            yield OutEvent("error", detail={"message": errored})
            return
        if self._cancel.is_set():
            trace.finish(barged_in=True, trace_dir=self.trace_dir)
            yield OutEvent("cancelled")
            return

        rep = trace.finish(barged_in=False, trace_dir=self.trace_dir)
        # persist off the hot path (Build #1's post-turn pipeline), verbatim reply
        if persist:
            await self.brain.persist(session_id, text, "".join(raw_reply))
        yield OutEvent("done", detail={"latency": rep,
                                       "expression": parser.current_expression()})

    def _synth(self, sentence: str) -> list[AudioChunk]:
        """Synthesize one sentence to audio chunks (runs in a worker thread)."""
        try:
            return list(self.tts.stream(sentence))
        except Exception:
            log.exception("TTS failed for a sentence (turn continues, muted)")
            return [AudioChunk(0, sentence, np.zeros(0, dtype=np.float32),
                               self.tts.sample_rate)]
