"""Emotion tagging (SPEC §6) — the intent-vs-realisation split (→ ch. 02 §1, ch. 25).

The model emits inline expression tags in its reply stream, e.g.

    [happy] Hey, you made it back. [surprised] Wait, is that a new mug?

The tag is the *intent* (BML/FML); the avatar and TTS are the *realisation*. This
module does three jobs, all on the hot path:

  1. Strip tags out of the text before it reaches TTS — she must never *say* the
     word "happy"; the tag drives her face, not her mouth.
  2. Emit an expression event the instant a tag closes, so the avatar changes
     before the matching audio plays (the face leads the voice slightly, which
     reads as natural, → ch. 25).
  3. Strip *asterisk narration* — `*she leans in*`, `*smiles*` — out of the
     spoken text. This is a *voice* conversation: only the words she says aloud
     should reach TTS, never the roleplay stage directions a chat model emits by
     habit. Prompting (SPOKEN_STYLE_DIRECTIVE) asks the model not to narrate;
     this is the belt-and-suspenders that catches what leaks anyway.

    3b. A reasoning model may ignore its no-think setting and emit a
      `<think>…</think>` block. This is private model work, never speech, so it
      is dropped even when its tags arrive in separate stream chunks.
    3c. Gemma 4 marks its output up in *channels* — `<|channel>thought\n…
      \n<channel|>` for private reasoning, then the spoken content — and llama.cpp
      has no Gemma-4-aware output parser, so a completion that regenerates its
      channel markup streams it to the user verbatim. The thought channel is the
      same private work as 3b and is dropped to its `<channel|>` close; the
      markers themselves (`<|channel>`, `<channel|>`, `<|turn>`, `<turn|>`) are
      structure, never speech, and are dropped whatever wraps them.
    3d. A chat model also narrates *inside brackets* — `[She goes still, a long
     breath.]` — reusing the same brackets it was told to use for expression tags.
     A real tag is one short palette word; anything longer in brackets is a stage
     direction and is dropped to the closing `]`, never spoken (this is what TTS
     frontends like SillyTavern do to every `[…]` span). The old code flushed those
     long spans back as *literal spoken text* — that was the bug that made her read
     her own stage directions aloud.

The parser is a **streaming** one: tags and narration can split across token
boundaries (`[ha` … `ppy]`, `*she` … ` waves*`), so it buffers an open `[` until
it sees `]` and drops everything between a pair of `*`. Unknown tags are dropped
silently — a model that invents `[mischievous]` should not leak the literal text
into her speech (SPEC §6.2, tolerant by contract).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A real expression tag is a single short palette word; the longest ("surprised")
# is 9 chars. Anything longer inside brackets is not a tag but *narration* the
# model wrote in brackets (`[She goes still, …]`, `[tender, almost fragile]`) —
# and in a spoken exchange that must be dropped, never spoken. The margin over 9
# leaves room for a stray space/casing without swallowing multi-word directions.
_MAX_TAG_LEN = 14
_REASONING_TAG = re.compile(
    r"<\s*(?P<close>/)?\s*(?P<name>think|thought|thinking|reasoning|scratchpad)\s*>",
    re.IGNORECASE,
)

# Gemma 4's channel markup (job 3c). A `<|channel>` opens a named channel; the
# name runs to the first whitespace, the content to `<channel|>`. Names in
# _PRIVATE_CHANNELS are the model's reasoning — dropped like a <think> block;
# any other channel's content is speech and only the markers go. Turn markers
# bracket whole turns and are never content.
_PRIVATE_CHANNELS = ("thought", "think", "thinking", "reasoning")
_TURN_MARKERS = ("<turn|>", "<|think|>")
#: Role names a `<|turn>` marker carries — structure, never speech.
_TURN_ROLES = ("system", "user", "model", "developer", "tool")

# The palette the model is told to use (SPEC §6.1). Names only — the *mapping*
# from name → Live2D parameters lives in web/avatar.js, so the brain stays
# renderer-agnostic (a VRM avatar in Build #4 consumes the same names).
PALETTE = (
    "neutral", "happy", "sad", "surprised",
    "shy", "thinking", "playful", "tender",
)

# The system directive appended to Build #1's assembled prompt (SPEC §6.1).
# Kept tiny and example-led; the model already has the persona from the SOUL.
EXPRESSION_DIRECTIVE = (
    "Express your feeling with an inline tag in square brackets at the start of "
    "each sentence whose mood differs from the last, using ONLY these tags: "
    + ", ".join(f"[{name}]" for name in PALETTE) + ". "
    "The tags drive your facial expression; never write the feeling word itself "
    "as speech. A tag is exactly one of those words in brackets and nothing else — "
    "never put a sentence, description, or action inside brackets (no '[she goes "
    "still]'). Example: '[happy] There you are. [tender] I missed you.'"
)

# The voice mode is spoken, not written (SPEC §6, → ch. 24). A chat model narrates
# by habit — `*she smiles*`, "She tilts her head" — which is right for text chat
# but wrong out loud: the TTS would read the stage directions. Tell it plainly this
# is speech. The EmotionParser also strips `*...*` as a safety net, but pure-prose
# narration ("She leans back.") can only be prevented here, in the prompt.
SPOKEN_STYLE_DIRECTIVE = (
    "This is a spoken conversation, not text chat. Write ONLY the words you say "
    "out loud — natural, spoken language. Do not narrate: no action descriptions, "
    "no stage directions, no asterisk actions (never write *smiles* or *leans in*) "
    "and no bracketed ones either (never write [she goes still] or [softly]), "
    "and do not describe your movements, face, or surroundings. Your feelings come "
    "through the expression tags and your word choice, not through narration."
)


@dataclass
class ExpressionEvent:
    """A face change to apply, ordered against the spoken text by `at_char`."""
    expression: str
    at_char: int          # index into the clean text where this mood begins


@dataclass
class EmotionParser:
    """Feed it raw model tokens; get clean text + expression events out.

    Stateful and streaming — one instance per reply. `push` returns any *clean*
    text that became safe to speak on this token (i.e. not sitting inside a
    half-open tag); `events` accumulates every expression change."""

    default: str = "neutral"
    clean: str = ""                                  # all clean text so far
    events: list[ExpressionEvent] = field(default_factory=list)
    _buf: str = ""                                   # open-tag buffer (after '[')
    _in_tag: bool = False
    _in_narr: bool = False                           # inside a *…* narration span
    _drop_bracket: bool = False                      # inside an over-long […] span
    _angle_buf: str = ""                             # a possible reasoning tag
    _reasoning_tag: str = ""                         # tag whose contents are private
    _channel_name: str = ""                          # after `<|channel>`: its name

    def push(self, token: str) -> str:
        """Consume one model token; return the newly-speakable clean text.

        `self.clean` is updated *incrementally* (not batched at the end), so a tag
        that closes partway through this token sees the correct `at_char` offset."""
        start = len(self.clean)
        for ch in token:
            if self._reasoning_tag:
                self._push_reasoning(ch)
            elif self._angle_buf == "<|channel>":
                self._push_channel_name(ch)
            elif self._angle_buf == "<|turn>":
                self._push_turn_role(ch)
            elif self._angle_buf:
                self._push_angle(ch)
            elif ch == "<":
                self._angle_buf = ch
            elif self._in_narr:
                # inside *…* narration — drop every char until the closing '*'.
                # Nothing here reaches self.clean, so it is never spoken (job 3).
                if ch == "*":
                    self._in_narr = False
            elif self._drop_bracket:
                # inside an over-long […] span already ruled a stage direction —
                # drop every char until the closing ']', exactly like *…* above.
                if ch == "]":
                    self._drop_bracket = False
            elif self._in_tag:
                if ch == "]":
                    self._close_tag()               # reads live len(self.clean)
                elif ch == "[":
                    # a stray '[' inside a tag: the previous one was junk, restart
                    self._buf = ""
                elif len(self._buf) >= _MAX_TAG_LEN:
                    # too long to be a palette tag — this is bracketed *narration*
                    # (`[She goes still, …]`), which a chat model emits by habit.
                    # Drop it to the closing ']' instead of speaking it, the same
                    # rule TTS frontends apply to every […] span (never flush it
                    # back as literal text — that is what made her read them aloud).
                    self._buf, self._in_tag, self._drop_bracket = "", False, True
                else:
                    self._buf += ch
            elif ch == "[":
                self._in_tag, self._buf = True, ""
            elif ch == "*":
                self._in_narr = True                # open a narration span, drop it
            else:
                self.clean += ch
        return self.clean[start:]

    def _push_reasoning(self, ch: str) -> None:
        """Discard a reasoning block until its matching closing tag arrives."""
        if self._angle_buf:
            self._angle_buf += ch
            if ch == ">":
                if self._closes_reasoning(self._angle_buf):
                    self._reasoning_tag = ""
                self._angle_buf = ""
            elif len(self._angle_buf) > 64:
                self._angle_buf = ""      # a '<' in the reasoning, not a tag
        elif ch == "<":
            self._angle_buf = ch

    def _closes_reasoning(self, tag: str) -> bool:
        """Is `tag` the closer for the private block currently being dropped?"""
        if self._reasoning_tag == "channel":
            return tag == "<channel|>"
        match = _REASONING_TAG.fullmatch(tag)
        return bool(match and match.group("close")
                    and match.group("name").lower() == self._reasoning_tag)

    def _push_channel_name(self, ch: str) -> None:
        """Read the name after `<|channel>`; decide whether its content is speech.

        The name runs to the first whitespace (or to another markup tag). A
        `thought` channel is the model's private reasoning and drops to
        `<channel|>` like a <think> block; anything else's content is kept and
        only the markers are dropped (job 3c)."""
        if ch == "<":
            self._open_channel()
            self._angle_buf = ch
        elif ch.isspace():
            self._open_channel()
            if not self._reasoning_tag:
                self.clean += ch            # a kept channel's separator is content
        else:
            self._channel_name += ch

    def _open_channel(self) -> None:
        if self._channel_name.strip().lower() in _PRIVATE_CHANNELS:
            self._reasoning_tag = "channel"
        self._channel_name = ""
        self._angle_buf = ""

    def _push_turn_role(self, ch: str) -> None:
        """Read the role name after `<|turn>`; it is structure, never speech.

        A known role (`model`, `user`, …) is dropped with its separator. An
        unknown one means the markup was literal text after all — flush it."""
        if ch == "<":
            self._channel_name = ""
            self._angle_buf = ch
        elif ch.isspace():
            if self._channel_name.strip().lower() not in _TURN_ROLES:
                self.clean += "<|turn>" + self._channel_name + ch
            self._channel_name = ""
            self._angle_buf = ""
        else:
            self._channel_name += ch

    def _push_angle(self, ch: str) -> None:
        """Recognise a reasoning tag without swallowing ordinary angle-bracket text."""
        self._angle_buf += ch
        if ch != ">":
            if len(self._angle_buf) > 64:
                self.clean += self._angle_buf
                self._angle_buf = ""
            return
        tag = self._angle_buf
        if tag in ("<|channel>", "<|turn>"):
            return                          # keep buf: a name/role follows
        if tag == "<channel|>" or tag in _TURN_MARKERS:
            # channel/turn markup is structure, never speech (job 3c) — and a
            # stray closer is model markup too, so it is never shown.
            self._angle_buf = ""
            return
        match = _REASONING_TAG.fullmatch(tag)
        if match:
            if not match.group("close"):
                self._reasoning_tag = match.group("name").lower()
        else:
            self.clean += tag
        self._angle_buf = ""

    def finish(self) -> str:
        """End of stream: flush a dangling half-open bracket tag as literal text.

        A dangling `[` is usually real text (a price, a citation), so we flush it.
        A dangling `*`, by contrast, is almost always narration the model was still
        writing when it hit the token limit — so we *drop* it rather than speak a
        stray asterisk and half a stage direction."""
        if self._reasoning_tag:
            self._reasoning_tag, self._angle_buf = "", ""
            return ""
        if self._angle_buf in ("<|channel>", "<|turn>"):
            # a markup opener whose name never finished is markup, not text
            self._angle_buf, self._channel_name = "", ""
            return ""
        if self._in_tag:
            tail = "[" + self._buf
            self._in_tag, self._buf = False, ""
            self.clean += tail
            return tail
        if self._angle_buf:
            tail = self._angle_buf
            self._angle_buf = ""
            self.clean += tail
            return tail
        self._in_narr = False                       # drop any unclosed *…* narration
        self._drop_bracket = False                  # drop any unclosed […] narration
        return ""

    def current_expression(self) -> str:
        return self.events[-1].expression if self.events else self.default

    def _close_tag(self) -> None:
        name = self._buf.strip().lower()
        self._in_tag, self._buf = False, ""
        if name in PALETTE:
            # de-dupe: two identical tags in a row is not a change
            if not self.events or self.events[-1].expression != name:
                self.events.append(ExpressionEvent(name, len(self.clean)))
        # unknown tag → dropped silently (SPEC §6.2)
