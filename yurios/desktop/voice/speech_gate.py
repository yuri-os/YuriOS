"""SpeechGate (SPEC §3.4) — debounced turn-taking over a raw VAD.

A raw per-frame VAD answers "is *this* 32 ms frame speech?" — but a single
mechanical keystroke, a mouse click, or a lip-smack is a high-energy transient
that a plain energy gate (and even a frame-level model, for a frame or two) will
call speech. Acting on one such frame is the "I typed and she stopped" bug: the
mic hears the keyboard over her voice and fires a barge-in (SPEC §4.3), or STT
hallucinates a turn out of the clatter.

The gate fixes it with *hysteresis on the frame count*: it only reports speech
after N consecutive speech frames, and — because interrupting her should cost
more confidence than merely starting a fresh turn — it confirms a barge-in with a
*higher* count than an onset. A keystroke is ~1–2 frames; real speech sustains
for dozens, so the gate passes speech and rejects transients.

This is the normative reference for the edge VAD debounce in `web/voice.js`
(barge-in is decided at the edge for latency — ch. 24 — so the browser runs the
same algorithm on the same constants). The server uses it to *validate* that an
endpointed utterance actually contained speech (`confirmed`), closing the last
gap when a client's edge gate is naive (SPEC §4.2 defense-in-depth).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpeechGate:
    onset_frames: int = 3        # consecutive speech frames to confirm a new turn
    bargein_frames: int = 5      # stricter: consecutive frames to interrupt her
    hangover_frames: int = 8     # consecutive silence frames before endpoint

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Clear all state for a fresh utterance."""
        self._run_speech = 0     # consecutive speech frames seen right now
        self._run_silence = 0    # consecutive silence frames seen right now
        self._active = False     # are we inside a confirmed utterance?
        self._confirmed = False  # did this utterance ever clear the onset bar?

    @property
    def active(self) -> bool:
        """True between a confirmed onset/barge-in and the following endpoint."""
        return self._active

    @property
    def confirmed(self) -> bool:
        """True once this utterance has cleared the onset threshold at least once
        — i.e. it held real, sustained speech, not just a transient. Stays True
        until `reset()`, so it can be read after the endpoint to decide whether the
        utterance was worth transcribing."""
        return self._confirmed

    def push(self, is_speech: bool, *, speaking: bool = False) -> str | None:
        """Feed one frame's raw speech/no-speech verdict.

        `speaking` is whether *she* is talking right now (playback live); it selects
        the stricter barge-in threshold so key-clatter under her voice doesn't cut
        her off. Returns the event the instant one fires, else None:
            "onset"    a new user turn has begun (enough sustained speech)
            "bargein"  the user is talking over her (stricter confirmation)
            "endpoint" the user has stopped (enough sustained silence)
        """
        if is_speech:
            self._run_speech += 1
            self._run_silence = 0
        else:
            self._run_silence += 1
            self._run_speech = 0

        if not self._active:
            need = self.bargein_frames if speaking else self.onset_frames
            if self._run_speech >= need:
                self._active = True
                self._confirmed = True
                return "bargein" if speaking else "onset"
            return None

        if self._run_silence >= self.hangover_frames:
            self._active = False
            return "endpoint"
        return None
