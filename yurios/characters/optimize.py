"""Rewriting an imported card into this runtime's shape, with a model (SPEC §30.6).

`cardsplit.py` routes a card's lines into the four backbone sections and gets the
common shapes right for nothing. It cannot do the rest, and the rest is most of
it: a lore dump sitting in `scenario` that wants to be lorebook entries; a
jailbreak preamble in `description` that wants to be the voice law; a
`character_version` holding a source URL; a personality field that is empty
because the author wrote her traits as bold headers three thousand characters
into the description. Every card is idiosyncratic and there is no parser for
taste, so this asks a model — the one the user picks, on the machine the user
picked it on.

Three properties make that safe enough to put behind a button:

**It proposes; it never saves.** The route hands the caller a draft and a diff.
Nothing reaches `vault/soul/` until the studio's ordinary PATCH does it, which
means the human sees every moved sentence first. That is also the answer to
prompt injection: a card is a file from the internet, its text reaches the model
as *material*, and the worst a hostile card can do is propose an edit that a
person then declines.

**It moves, it doesn't invent.** The system prompt is mostly a list of things
not to do, because the failure that ruins this feature is a model that
"improves" a character into a different one. Content may be rehomed, split,
compressed or re-registered. It may not be replaced with the model's own idea of
who she should be, and it may not be sanitised — these are adult roleplay cards,
and a tool that quietly softens them is a tool nobody presses twice.

`examples` is the one deliberate exception. Most cards ship none — 7 of 30 in
the sample — and an empty examples field is the difference between a model that
has heard her speak and one guessing from adjectives. So the optimiser may
*compose* exchanges where the card has none, bounded twice over: it may only
demonstrate a voice the card already describes, and an example that asserts a
new fact is an invented fact and is out of scope. Where the card does ship
examples they are kept verbatim, because the author's own are better evidence
than anything a re-filer would write.

**Anything it returns is bounded by the draft's own types.** The model's JSON is
merged key-by-key against `Draft.__slots__` and then re-coerced by
`Draft.from_dict`; an unknown key, a wrong type, a lorebook entry with no keys
are all dropped rather than trusted.

The user's own instruction (`instructions`) rides on top of all of it, because
the second half of what this is for is not repair but preference: *she is too
guarded — make her devoted from the first line, keep the sharp tongue.*
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .studio import Draft

log = logging.getLogger("characters.optimize")


class CardOptimizeError(RuntimeError):
    """The optimisation could not be produced. Always safe to show a user."""


class EmptyAnswer(CardOptimizeError):
    """The model said nothing usable. Internal: the runner retries on it."""


#: Fields the model may set. `description` is excluded on purpose — it is the
#: derived render of the four backbone sections (`Draft.description`), so a model
#: writing it would be writing to a read-only property and silently losing the
#: split that the whole SOUL design rests on.
OPTIMIZABLE: tuple[str, ...] = (
    "name", "nickname", "creator", "character_version", "tags",
    "identity", "history", "appearance", "manner", "personality",
    "scenario", "first_mes", "alternate_greetings", "examples",
    "system_prompt", "post_history_instructions", "creator_notes", "lorebook",
)

#: What each field is *for*, in the model's prompt. This is the actual contract
#: the feature delivers: a card is only "in our format" to the extent that a
#: sentence ends up under the right one of these.
FIELD_GUIDE: tuple[tuple[str, str], ...] = (
    ("name", "Her name alone. Never a title, a scenario or a card title."),
    ("nickname", "What replaces {{char}} in the prompt, if she goes by something "
                 "shorter. Usually empty."),
    ("character_version", "A version string like \"1.0.0\". If the source put a "
                          "URL, a chat name or a changelog here, move it to "
                          "creator_notes and use \"1.0.0\"."),
    ("creator", "The card's author. Leave as found."),
    ("tags", "Short lowercase tags. Keep the source's, drop duplicates."),
    ("identity", "WHO SHE IS, immutable: species, age band, role, station, the "
                 "handful of facts that do not change. Not her looks, not her "
                 "past, not her mood. Short — this is a definition, not an essay."),
    ("history", "HER PAST: backstory, how she got here, what was done to her and "
                "by whom. Everything the source called backstory/background/origin."),
    ("appearance", "HER BODY AND WHAT SHE WEARS: build, hair, eyes, skin, marks, "
                   "species features, habitual outfit. Only what can be seen."),
    ("manner", "HOW SHE COMES ACROSS: temperament, how she treats people, speech "
               "habits, what she wants, fears, likes, dislikes, boundaries, "
               "kinks. The behavioural half of the persona."),
    ("personality", "ONE SHORT LINE — a comma-separated register of adjectives, "
                    "under 15 words. \"sharp-tongued, watchful, avoidant, quietly "
                    "devoted\". Not a paragraph. Derive it from `manner` if the "
                    "source left it empty."),
    ("scenario", "THE PRESENT SITUATION ONLY: where they are, right now, and why. "
                 "Two or three sentences. World-building, history and rules do "
                 "NOT belong here — move them to the lorebook."),
    ("first_mes", "Her opening message, as prose/roleplay. Keep the source's "
                  "voice and formatting; do not shorten it."),
    ("alternate_greetings", "Other openings, for someone she has met before."),
    ("examples", "Example exchanges, one per array item, each demonstrating how "
                 "she actually talks. If the card has them, keep them: the "
                 "source's <START>-separated blocks become the items, verbatim. "
                 "If the card has NONE, write THREE AT MOST — this is the "
                 "single field you may compose rather than move, and every one "
                 "you add is spent out of her prompt budget on every turn "
                 "forever, so three good ones beat eight. Use only "
                 "what the card already establishes and match the prose style of "
                 "her opening message (asterisks, quotes, line breaks and all). "
                 "Format each as `{{user}}: …` / `{{char}}: …`."),
    ("system_prompt", "VOICE LAW: standing instructions to the model about how to "
                      "play her — perspective, tense, length, what never to do. "
                      "Out-of-character preambles and jailbreak text found in the "
                      "description belong HERE, not in her persona."),
    ("post_history_instructions", "HARD LIMITS: the last thing read before she "
                                  "replies. Only the rules that must never be "
                                  "forgotten. Keep it short."),
    ("creator_notes", "For the human who opens the card. Never sent to the model "
                      "— source links, credits and author's notes go here."),
    ("lorebook", "Keyword-triggered world facts. {\"entries\": [{\"name\": ..., "
                 "\"keys\": [...], \"content\": ...}]}. Every place, faction, "
                 "system, rule or side character the card explains belongs here, "
                 "with the words that should trigger it as `keys`."),
)

#: The card is re-filed in **passes**, not in one call, and that is a hard
#: requirement rather than a tidiness preference. A reasoning model spends its
#: `<think>` tokens out of the same window as its answer, and a local model is
#: routinely *loaded* with a window far smaller than it supports — 8k is a
#: common LM Studio default. One call carrying a whole card can leave a 12B
#: model with 2,800 tokens for think-plus-answer, which it spends entirely on
#: thinking and returns nothing at all. Observed, not theorised.
#:
#: So each pass sends only the fields that inform its own decisions, and asks
#: for only its own group. The prompt halves, the answer shortens, and the
#: reasoning pass gets room to run. Passes are sequential and cumulative — pass
#: two sees the draft pass one left behind — which is also what keeps two passes
#: from both claiming `identity`.
@dataclass(frozen=True, slots=True)
class Pass:
    name: str
    label: str
    focus: str
    produce: tuple[str, ...]
    #: Fields sent as material. Always a superset of `produce`: the persona pass
    #: cannot pull an appearance out of `identity` without being shown it.
    material: tuple[str, ...]


PASSES: tuple[Pass, ...] = (
    Pass(
        name="persona", label="who she is",
        focus="Split the persona. Whatever the source crammed into one block "
              "becomes four: the unchanging facts, the past, the body, and the "
              "behaviour — plus the one-line register.",
        produce=("identity", "history", "appearance", "manner", "personality"),
        material=("name", "identity", "history", "appearance", "manner",
                  "personality"),
    ),
    Pass(
        name="scene", label="where she is",
        focus="Separate the situation from the world. `scenario` keeps only what "
              "is happening right now; every place, faction, rule, system and "
              "side character it was carrying becomes a lorebook entry with the "
              "words that should trigger it.",
        produce=("scenario", "first_mes", "alternate_greetings", "lorebook"),
        material=("name", "identity", "manner", "scenario", "first_mes",
                  "alternate_greetings", "lorebook"),
    ),
    Pass(
        name="frame", label="how she is played",
        focus="Sort the out-of-character material. Standing instructions to the "
              "model are voice law, not persona; author's notes, credits and "
              "source links are creator notes; a version field holding a URL is "
              "not a version. Then make sure she has example exchanges.",
        produce=("name", "nickname", "character_version", "creator", "tags",
                 "system_prompt", "post_history_instructions", "examples",
                 "creator_notes"),
        # `manner`, `scenario` and `first_mes` are here for `examples` alone —
        # the one field this pass may compose. Writing an exchange without her
        # manner in front of you produces generic dialogue in nobody's voice,
        # and `first_mes` is the only actual sample of the author's prose in the
        # card. It is the most expensive material in any pass and it buys the
        # difference between her examples and any character's.
        material=("name", "nickname", "character_version", "creator", "tags",
                  "identity", "manner", "scenario", "first_mes",
                  "system_prompt", "post_history_instructions",
                  "examples", "creator_notes"),
    ),
)

_PURPOSE = dict(FIELD_GUIDE)

_HEADER = """\
You re-file character cards. You are given one character card that was written \
for some other frontend, in that author's own idiosyncratic layout, and you \
return the same character re-filed into this runtime's fields.

This is one step of several. Set ONLY the fields listed below; the rest of the \
card is being handled separately and anything you write outside this list is \
discarded."""

_METHOD = """\
WHAT YOU ARE DOING
{focus}

Move the author's material to where it belongs, and only that. Most sentences \
should come out recognisably as the sentences that went in. When a field is a \
wall of text covering three of the fields above, split it. When something is in \
the wrong field, move it. When a field the source left empty can be filled from \
material already in the card, fill it. When the same fact is in two places, \
keep it once."""

_RULES = """\
HARD RULES
- Do NOT invent facts about her. No new backstory, no new relatives, no new \
powers, no new appearance details. If the card never says what colour her eyes \
are, they stay unsaid. `examples` is the one exception and it is a narrow one: \
you may compose exchanges when the card has none, but they may only demonstrate \
a voice and a manner the card already describes. An example that states a new \
fact is an invented fact.
- Do NOT censor, soften, moralise or add warnings. Adult, violent and \
transgressive content is in scope and is copied through unchanged. You are \
re-filing a document, not reviewing it.
- Do NOT drop material. If something belongs in a field you were not given, \
leave it where it is — another step will move it.
- Do NOT expand or rename {{user}} and {{char}}. They are placeholders and must \
survive verbatim.
- Keep her voice. Her prose stays her prose; you are not rewriting for style.
- first_mes and examples keep the source's formatting — asterisks, quotes, line \
breaks, all of it."""

_OUTPUT = """\
OUTPUT
Think briefly, then return ONE JSON object and nothing else. No prose around \
it, no markdown fence. Keep the thinking short: it is spent out of the same \
budget as the answer, and an answer that never arrives is the only real failure \
here.

{"notes": "<one short sentence: what you moved>",
 "fields": {"<field>": <new value>, ...}}

Include a field ONLY if you are changing it — a field you leave out keeps what \
it has, which is how you keep this response short. Strings for text fields, \
arrays of strings for tags/alternate_greetings/examples.{lorebook}"""

_LOREBOOK_SHAPE = (' The lorebook is '
                   '{"entries": [{"name": ..., "keys": [...], "content": ...}]}.')

_USER_INSTRUCTION = """

THE USER'S OWN INSTRUCTION FOR THIS CARD
The following comes from the person who owns this character. It outranks your \
default of minimal change — where it asks for a different characterisation, \
rewrite to deliver it, in every field you were given that it touches. It does \
not override the hard rules above.

{instructions}"""


def _wrap(value: object, limit: int = 0) -> str:
    text = "" if value is None else str(value)
    return text[:limit] if limit and len(text) > limit else text


def card_material(draft: Draft, fields: tuple[str, ...] = OPTIMIZABLE) -> str:
    """The card as the model sees it: the named non-empty fields, labelled.

    Sent as JSON rather than prose because the whole task is about which field a
    sentence is in, and a prose rendering blurs exactly that. Fields are sent
    **whole** — never truncated to fit — because the model rewrites what it is
    shown, and showing it half a section is how a section loses its second half.
    """
    data = draft.to_dict()
    payload: dict[str, Any] = {}
    for name in fields:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            payload[name] = value
        elif isinstance(value, list) and value:
            payload[name] = value
        elif isinstance(value, Mapping) and value.get("entries"):
            payload[name] = {"entries": value["entries"]}
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _is_empty(value: object) -> bool:
    if isinstance(value, Mapping):
        return not value.get("entries")
    if isinstance(value, list):
        return not value
    return not str(value or "").strip()


def build_messages(draft: Draft, instructions: str = "",
                   step: Pass = PASSES[0]) -> list[dict[str, str]]:
    guide = "\n".join(f"- {name}: {_PURPOSE[name]}" for name in step.produce)
    system = "\n\n".join((
        _HEADER,
        f"THE FIELDS YOU MAY SET\n{guide}",
        _METHOD.format(focus=step.focus),
        _RULES,
        # `.replace`, not `.format` — the template shows a literal JSON object,
        # and every brace in it would have to be doubled to survive formatting.
        _OUTPUT.replace("{lorebook}",
                        _LOREBOOK_SHAPE if "lorebook" in step.produce else ""),
    ))
    if instructions.strip():
        system += _USER_INSTRUCTION.format(instructions=instructions.strip()[:4000])
    data = draft.to_dict()
    # Naming the holes is worth its tokens: the single most common thing wrong
    # with an imported card is a field the source format had nowhere to put.
    empty = [name for name in step.produce if _is_empty(data.get(name))]
    user = (f"The card, as it currently sits in the studio:\n\n"
            f"{card_material(draft, step.material)}\n\n"
            f"Of the fields you may set, these are currently empty and may be "
            f"fillable from the material above: {', '.join(empty) or 'none'}.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _first_balanced_object(text: str) -> dict[str, Any] | None:
    """The first `{...}` whose braces balance, ignoring braces inside strings.

    `{{user}}` is everywhere in this material, and a naive brace count that also
    counted the ones inside JSON strings would close the object in the middle of
    her first message.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _repair_truncated(text: str) -> dict[str, Any] | None:
    """Salvage an answer that stopped mid-object.

    This is the common failure, not an exotic one: the material is long, the
    answer is long, and a model that hits its ceiling stops wherever it is —
    routinely one closing brace short of a perfectly good response. Every field
    in the proposal is independent, so a cut answer still carries real work, and
    throwing it away means the user pays for the call twice.

    So: rewind to the last point where a value was completely written, drop the
    half-finished field, and close what is still open. What survives is exactly
    the fields the model actually finished.
    """
    start = text.find("{")
    if start < 0:
        return None
    safe = -1
    in_string = escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                safe = index + 1          # a string value just closed cleanly
            continue
        if character == '"':
            in_string = True
        elif character in "}]":
            safe = index + 1
        elif character == ",":
            safe = index                  # cut *before* it; the comma dangles
    if safe <= start:
        return None

    body = text[start:safe].rstrip().rstrip(",")
    stack: list[str] = []
    in_string = escaped = False
    for character in body:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]" and stack:
            stack.pop()
    closers = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    # A rewind landing just after a closing quote can leave a key with no value
    # — `…, "scenario"` — which no amount of bracket-closing makes valid. It is
    # only tried on failure, because the same trailing string is a perfectly
    # good *element* when the open bracket is an array, and dropping it there
    # would throw away a field the model did finish.
    for candidate in (body, re.sub(r',\s*"(?:[^"\\]|\\.)*"\s*$', "", body)):
        try:
            parsed = json.loads(candidate + closers)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_object(raw: str) -> tuple[dict[str, Any], bool]:
    """The model's answer as an object, plus whether it had to be salvaged.

    A fenced block is what we sometimes get instead of the bare object we asked
    for, and a reasoning model may leave its `<think>` pass in front of it. The
    last balanced `{...}` in the text is the answer in every one of those cases.
    """
    # Reasoning models wrap their pass in a tag, and which tag depends on the
    # family — <think> for qwen/r1, <thought> for gemma. Both the block and a
    # stray unmatched closer have to go: the closer alone, trailing a complete
    # object, is enough to fail `json.loads` on an answer that is otherwise fine.
    tags = r"think|thought|thinking|reasoning|scratchpad"
    text = re.sub(rf"<({tags})>.*?</\1>", " ", str(raw or ""),
                  flags=re.DOTALL | re.I)
    text = re.sub(rf"</?(?:{tags})[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(),
                  flags=re.MULTILINE).strip()
    if not text:
        raise EmptyAnswer("the model produced no answer outside its reasoning")
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        value = _first_balanced_object(text)
    if isinstance(value, dict):
        return value, False
    value = _repair_truncated(text)
    if isinstance(value, dict):
        log.warning("optimize: the model's answer was cut off — kept the %d "
                    "field(s) it finished", len(value.get("fields") or value))
        return value, True
    raise CardOptimizeError(
        "the model's answer was not valid JSON — it may have been cut off "
        "before it finished. Try a larger model, or ask for less at once.")


def merge(draft: Draft, proposal: Mapping[str, Any]) -> Draft:
    """A new draft: the current one, with the model's accepted fields laid over.

    Only `OPTIMIZABLE` keys are read, and the result goes back through
    `Draft.from_dict`, so the model's output is coerced by exactly the same code
    that coerces a browser's. A field the model omitted, or set to null, or set
    to the wrong type, keeps what it had.
    """
    merged = draft.to_dict()
    fields = proposal.get("fields")
    if not isinstance(fields, Mapping):
        fields = proposal            # a model that skipped the envelope
    for name in OPTIMIZABLE:
        if name not in fields:
            continue
        value = fields[name]
        if value is None:
            continue
        current = merged.get(name)
        if isinstance(current, str):
            if not isinstance(value, (str, int, float)):
                continue
            merged[name] = str(value)
        elif isinstance(current, list):
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                continue
            merged[name] = [str(item) for item in value if str(item).strip()]
        elif isinstance(current, dict):
            if isinstance(value, list):
                value = {"entries": value}
            if not isinstance(value, Mapping):
                continue
            merged[name] = {**current, **value}
    # A card with no name is not importable anywhere and no instruction is worth
    # producing one, so the one field that is never allowed to go blank doesn't.
    if not str(merged.get("name") or "").strip():
        merged["name"] = draft.name
    merged.pop("description", None)      # derived, never assigned
    return Draft.from_dict(merged)


def changes(before: Draft, after: Draft) -> list[dict[str, Any]]:
    """Field-by-field, what moved — the diff the studio makes you read."""
    old, new = before.to_dict(), after.to_dict()
    out: list[dict[str, Any]] = []
    for name in OPTIMIZABLE:
        was, now = old.get(name), new.get(name)
        if was == now:
            continue
        out.append({
            "field": name,
            "before": _render(was),
            "after": _render(now),
            "filled": not _render(was).strip() and bool(_render(now).strip()),
            "emptied": bool(_render(was).strip()) and not _render(now).strip(),
        })
    return out


def _render(value: object) -> str:
    if isinstance(value, list):
        return "\n\n".join(str(item) for item in value)
    if isinstance(value, Mapping):
        return "\n\n".join(
            f"{entry.get('name') or ', '.join(entry.get('keys') or [])}\n"
            f"{entry.get('content') or ''}"
            for entry in value.get("entries") or [])
    return "" if value is None else str(value)


#: What a reasoning model spends before it says anything. Measured, not guessed:
#: gemma-4-12b spent 5,800–6,300 tokens thinking about passes of this size, and
#: spent them whether the pass was large or small — so it is a flat allowance
#: added on top of the answer rather than a multiplier, and a budget without it
#: is a budget the model never reaches the end of. Asking for more than the
#: server has is free (it clamps), so this errs high on purpose.
REASONING_ALLOWANCE = 6144


def token_budget(draft: Draft, step: Pass = PASSES[0], *,
                 ceiling: int = 32768, reasoning: int = REASONING_ALLOWANCE) -> int:
    """Room for one pass: enough for its answer, plus room to think first.

    Asking for more than the server will give is free — it clamps — so this errs
    high. The answer itself is bounded by the material: a pass returns only the
    fields it changed, so it cannot be much longer than what it was shown.
    """
    answer = int(len(card_material(draft, step.material)) / 4 * 1.2) + 512
    return min(ceiling, answer + reasoning)


@dataclass(slots=True)
class Optimization:
    draft: Draft
    changes: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    model: str = ""
    #: The answer arrived cut off and was salvaged (`_repair_truncated`). The
    #: fields below are real; the ones the model never reached are simply
    #: missing, and the studio says so rather than presenting a partial pass as
    #: a complete one.
    truncated: bool = False
    #: Passes that failed while others succeeded, each already a sentence. The
    #: studio shows them beside the diff: a partly-optimised card is a real
    #: result, and pretending otherwise would throw away the passes that worked.
    failed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"draft": self.draft.to_dict(), "changes": self.changes,
                "notes": self.notes, "model": self.model,
                "truncated": self.truncated, "failed": self.failed}


#: A listener for "where has this got to". Sync or async, and never load-bearing:
#: `_emit` swallows whatever it raises, because a browser that hung up mid-run
#: must not be able to fail an optimisation that is otherwise going fine.
Progress = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _emit(listener: Progress | None, **event: Any) -> None:
    if listener is None:
        return
    try:
        sent = listener(event)
        if inspect.isawaitable(sent):
            await sent
    except Exception:                                  # pragma: no cover - defensive
        log.debug("optimize: a progress listener raised", exc_info=True)


def _notifier(listener: Progress | None, step: Pass, index: int, total: int):
    """`notify(state=…)` for one pass, with its identity already filled in."""
    async def notify(**extra: Any) -> None:
        await _emit(listener, event="pass", index=index, total=total,
                    name=step.name, label=step.label, **extra)
    return notify


async def _run_pass(utility, draft: Draft, step: Pass, *, instructions: str,
                    timeout: float = 600.0,
                    notify: Callable[..., Awaitable[None]] | None = None,
                    ) -> tuple[dict[str, Any], bool]:
    """One pass, with the one retry that is worth making.

    A reasoning model that answers with nothing has almost always done the same
    thing: thought until `max_tokens` ran out. The retry keeps the reasoning
    pass — it is asked for a shorter one (`reasoning_effort`), which is the knob
    that leaves room for the answer without turning the model into a different
    one. Only if *that* comes back empty too is it a failure worth raising, and
    then the usage record says why in numbers the user can act on.
    """
    messages = build_messages(draft, instructions, step)

    async def ask(budget: int, effort: str = "") -> tuple[str, dict]:
        params: dict[str, Any] = {"max_tokens": budget, "temperature": 0.3}
        if effort:
            params["reasoning_effort"] = effort
        detailed = getattr(utility, "complete_detailed", None)
        if detailed is not None:
            return await asyncio.wait_for(detailed(messages, **params), timeout)
        return await asyncio.wait_for(utility.complete(messages, **params),
                                      timeout), {}

    # Two attempts: the ordinary one, then one with more room and a shorter
    # reasoning pass. Both keep thinking on — the retry asks the model to think
    # less, not to stop, because the answer is what ran out of room, not the
    # reasoning that produced it.
    attempts = ((token_budget(draft, step), ""),
                (token_budget(draft, step, reasoning=REASONING_ALLOWANCE * 2), "low"))
    for budget, effort in attempts:
        try:
            raw, meta = await ask(budget, effort)
        except asyncio.TimeoutError as exc:
            raise CardOptimizeError(
                f"the model did not answer within {int(timeout)}s — a local "
                "model on a long card can need longer than that") from exc
        except Exception as exc:
            log.exception("optimize: the %s pass failed to reach the model",
                          step.name)
            raise CardOptimizeError(
                f"the model could not be reached: {exc}") from exc
        try:
            return _json_object(raw)
        except EmptyAnswer:
            last = (budget, effort) == attempts[-1]
            log.warning("optimize: the %s pass came back empty (%s) — %s",
                        step.name, _usage(meta), "giving up on this pass" if last
                        else "retrying with more room and shorter reasoning")
            if last:
                raise CardOptimizeError(_no_room(meta, budget)) from None
            # The retry doubles this pass's wall time. Someone watching a bar
            # that has not moved in four minutes deserves to know why.
            if notify is not None:
                await notify(state="retry")
    raise CardOptimizeError("the model produced no answer")            # unreachable


def _usage(meta: Mapping[str, Any]) -> str:
    if not meta:
        return "no usage reported"
    return (f"prompt {meta.get('prompt_tokens', 0)}, "
            f"reasoning {meta.get('reasoning_tokens', 0)}, "
            f"answer {meta.get('completion_tokens', 0)}, "
            f"finish {meta.get('finish_reason') or '?'}")


def _no_room(meta: Mapping[str, Any], budget: int) -> str:
    """Why an empty answer was empty, in numbers rather than a shrug.

    The cause is nearly always a local model *loaded* with a smaller context
    than it supports — LM Studio's per-model default is often 8k — so the
    remedy is a setting, not a different card, and naming the numbers is what
    lets someone find it.
    """
    prompt = int(meta.get("prompt_tokens") or 0)
    total = int(meta.get("total_tokens") or 0)
    completion = int(meta.get("completion_tokens") or 0)
    reasoning = int(meta.get("reasoning_tokens") or 0)
    if not (prompt and total):
        return ("the model spent its whole budget on its reasoning pass and "
                "never answered. Load it with a larger context window, or pick "
                "a model that reasons less.")
    if completion and completion < budget - 8:
        # We asked for `budget` and got less, so something upstream stopped it:
        # the context window it was *loaded* with, which is usually far below
        # what the model itself supports and is a setting someone can change.
        return (f"the model thought for {reasoning} tokens and had none left to "
                f"answer with. It stopped at {total} tokens, of which this "
                f"card's prompt was {prompt} — that is the context window it "
                f"was loaded with, not the model's limit. Load it with a larger "
                f"one (CONTEXT_LENGTH in the settings panel, or the model's own "
                f"load config in LM Studio / Ollama), or pick a bigger model.")
    return (f"the model reasoned for {reasoning} tokens, twice, and never "
            f"reached an answer — even with {budget} to spend. This card needs "
            f"a model that thinks less on its way to a reply; try a hosted one, "
            f"or turn UTILITY_THINKING off in the settings panel.")


async def optimize_draft(utility, draft: Draft, *, instructions: str = "",
                         model: str = "", timeout: float = 600.0,
                         steps: tuple[Pass, ...] = PASSES,
                         on_progress: Progress | None = None) -> Optimization:
    """Re-file this card, one pass per group of fields.

    Passes are cumulative: each is built from the draft the previous one left,
    so the scene pass reads the identity the persona pass just tidied rather
    than the blob it came from. A pass that fails does not lose the ones that
    already succeeded — the work is kept, the failure is named, and only a run
    where *every* pass failed raises.

    Unlike `appearance.derive_identity`, which falls back silently because an
    import must not fail on a busy model, this is a button someone pressed: a
    failure has to say so, out loud, with something to act on.

    `on_progress` is told when each pass starts, retries, finishes or fails.
    Three sequential calls to a reasoning model is minutes of silence, and a
    button that shows nothing for minutes reads as a broken button — so the
    route streams these to the dialog. It is decoration only: the return value
    is identical with or without a listener.
    """
    if utility is None:
        raise CardOptimizeError(
            "no model is configured to optimise with — pick one in the dialog, "
            "or set UTILITY_MODEL in the settings panel")

    working = draft
    notes: list[str] = []
    failures: list[str] = []
    truncated = False
    for index, step in enumerate(steps, 1):
        notify = _notifier(on_progress, step, index, len(steps))
        await notify(state="start")
        try:
            proposal, cut = await _run_pass(utility, working, step,
                                            instructions=instructions,
                                            timeout=timeout, notify=notify)
        except CardOptimizeError as exc:
            log.warning("optimize: the %s pass failed: %s", step.name, exc)
            failures.append(f"{step.label}: {exc}")
            await notify(state="failed", message=str(exc))
            continue
        truncated = truncated or cut
        # A pass may only write the fields it was given. Without this a model
        # that answers helpfully with a field from the next pass would have that
        # answer overwritten two calls later, which looks like the feature
        # losing edits at random.
        fields = proposal.get("fields")
        allowed = {name: value for name, value in
                   (fields if isinstance(fields, Mapping) else proposal).items()
                   if name in step.produce}
        before, working = working, merge(working, {"fields": allowed})
        moved = [item["field"] for item in changes(before, working)]
        await notify(state="done", fields=moved)
        note = _wrap(proposal.get("notes"), 300).strip()
        # Only a pass that moved something gets to narrate. A model asked for
        # the scene while it is still thinking about the persona will answer
        # with the persona again — correctly discarded above — and its note
        # would otherwise claim credit for work this pass did not do.
        if note and note not in notes and moved:
            notes.append(note)

    diff = changes(draft, working)
    if not diff:
        raise CardOptimizeError(
            "; ".join(failures) if failures else
            "the model returned no changes — it may consider the card already "
            "well-formed. Try saying what you want changed.")
    return Optimization(draft=working, changes=diff, model=model,
                        notes=" ".join(notes)[:900], truncated=truncated,
                        failed=failures)
