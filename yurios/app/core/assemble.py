"""Prompt assembly (SPEC §7) — the single most important function.

Composes the model input from SOUL (static) + Vault (current) + a small raw
window. Block ordering and budgets are normative (§7.1–7.2):

    1. VOICE LAW                       — CONSTITUTION#Voice law
    2. PERSONA BACKBONE                — identity · history · appearance · manner
    3. SCENARIO / PLACE                — SCENARIO#Scenario
    4. LORE                            — matched WORLD.md entries (this turn)
    5. WHO YOU ARE TO HER              — vault/soul/USER.md, whole (it's small)
    5b. WHAT YOU'RE WORKING ON         — vault/goals.md, the open ones (§22)
    6. WHAT YOU'VE TALKED ABOUT        — vault/memory/summary.md
    7. THINGS THAT MAY BE RELEVANT     — recall(user_msg, k), tagged with age
    8. WHAT YOU'VE READ                — knowledge.search(user_msg, k), with citations
    9. THE HONESTY CONSTRAINT          — fixed text (§7.4, property 2)
   10. EXAMPLE VOICE                   — optional, if budget allows

Blocks 7 and 8 are the two retrieval slots, and they are deliberately separate
(§20): **memory cites a conversation turn, knowledge cites a document + span.**
A book she read must never arrive dressed as something you told her, so it gets
its own block, its own store, and its own citations rather than being folded in
among the memories.

On overflow: examples first, then knowledge, then recalled memories, then
lorebook, then her open goals; NEVER the voice law, persona, USER.md, or the
honesty constraint (§7.2). Goals go last of the droppables because they are the
smallest block here and the one whose absence she cannot notice — a companion
who re-promises what she is already working on is the failure this block exists
to stop. Hard limits land AFTER the history (V2/V3 post-history semantics) —
fused onto the final user message, the last thing read before replying.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from yurios.app.core.soul import LoreEntry, Soul, apply_macros
from yurios.app.memory.store import Memory

# bump whenever the assembly layout changes — stamped on every corpus record (§8.2)
TEMPLATE_VERSION = "b1-assemble-v2"

# §7.4 — the honesty constraint, fixed text, verified by the golden transcript test
HONESTY = """\
You remember only what is in the memory blocks above and this conversation. If \
{{user}} asks about something you have no record of, say so warmly and plainly — \
"I don't think you've told me that yet" — and ask, rather than inventing a \
memory. The same rule runs the other way: when {{user}} tells you something new, \
take it as new — never respond with "I remember" or "you told me" details that \
are not actually in the blocks above. Never fabricate a shared past."""


#: The lead line on the knowledge block. It does two jobs: it tells her the
#: citation is hers to say out loud, and it draws the §20 boundary in the prompt
#: itself — reading is not remembering, and a page she found must not come back
#: as "you told me". The honesty constraint below it forbids inventing a shared
#: past; this is what keeps a shelf item from becoming one.
KNOWLEDGE_NOTE = """\
Things you have read and kept: books {{user}} put on your shelf, and pages you \
looked up yourself. This is reading, not memory — none of it is something \
{{user}} told you, so never answer as though they did. Use it when it helps, in \
your own words, and you can say where it came from if it matters."""


#: The lead line on the goals block (§22). It says the two things the block is
#: for: these are already hers, so promising them again is a broken promise
#: waiting to happen; and they are a *list she keeps*, so she may refer to one
#: out loud rather than inventing a fresh intention every time the subject
#: comes up. Without this block the talking-self and the intending-self are two
#: different people who have never met.
GOALS_NOTE = """\
What you are already working on — your own standing list, the same one the \
quiet hours between conversations work through. These are commitments you have \
ALREADY made, so don't promise them again as though they were new; refer to \
one, ask about it, or say where it got to. If you take on something new here, \
say so plainly and it will be added."""


class Known(Protocol):
    """One retrieved knowledge chunk (§20.2).

    `yurios.mind.knowledge.Chunk` satisfies this. The shape is the contract
    rather than the class, because the app layer must not import the mind
    layer — assembly is Build #1's and predates the knowledge store entirely.
    """
    text: str

    @property
    def citation(self) -> str:
        """`doc (chars a-b)` — the span she can be asked to show."""


def est_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) — budgets are guardrails, not billing."""
    return len(text) // 4


def _age_tag(mem: Memory) -> str:
    days = int(mem.age_days())
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


@dataclass
class AssembledPrompt:
    system: str
    messages: list[dict]          # [system, *window, final user (+ fused hard limits)]
    template_version: str = TEMPLATE_VERSION
    dropped_memories: int = 0     # overflow accounting (§7.2)
    dropped_lore: int = 0
    dropped_knowledge: int = 0
    dropped_goals: int = 0
    citations: list[str] = field(default_factory=list)   # what block 8 carried


def _block(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}"


#: What the mind's own prompts open with, when they open with anything (§7.1, §22.4).
#:
#: The conversational assembler below is not reusable here and must not be made
#: so. `assemble()` needs a user message, a window, a summary and two retrieval
#: stores; a DREAM job at 4am has none of those and is not having a turn. What
#: it shares with a turn is the half that never changes — who she is — and that
#: is what this returns.
#:
#: Deliberately **not** included: recalled memories, the shelf, her goals, the
#: honesty constraint, example voice. Those are turn-shaped, and each mind
#: caller already supplies what its own job needs — `_goal_context` carries the
#: situation, the desk, her skills, the durable facts and her other goals, and
#: had every one of them before it had a persona.
SOUL_PREAMBLE_NOTE = """\
This is who you are. Everything below this block is your own — your work, your \
day, your thinking, done alone with nobody waiting. Answer as yourself, in your \
own voice, the way you would if {{user}} could hear you."""


def soul_preamble(soul: Soul, *, user_md: str = "", user_name: str = "you",
                  full: bool = True) -> str:
    """The identity half of §7.1, for a prompt that is not a turn.

    `full=False` keeps the three blocks that make her *her* and drops the two
    that place her — the scenario and who the user is. That is the budget
    ladder for a small utility model, and it is ordered the way §7.2 orders
    every other drop: the most replaceable thing goes first, and the voice law
    never goes at all.

    Returns "" when there is no soul to render, so a caller can concatenate the
    result unconditionally — an absent persona costs the block, never the call
    (§20.2's rule for the shelf, applied to the self).
    """
    if soul is None:
        return ""
    blocks: list[str] = []
    if soul.voice_law.strip():
        blocks.append(_block("VOICE LAW", soul.voice_law))
    backbone = soul.backbone.strip()
    if soul.personality.strip():
        backbone = f"{backbone}\n\nPersonality: {soul.personality}".strip()
    if backbone:
        blocks.append(_block("PERSONA BACKBONE", backbone))
    if full:
        if soul.scenario.strip():
            blocks.append(_block("SCENARIO", soul.scenario))
        if user_md.strip():
            blocks.append(_block("WHO YOU ARE TO HER", user_md))
    if not blocks:
        return ""
    blocks.append(_block("WHOSE THINKING THIS IS",
                         apply_macros(SOUL_PREAMBLE_NOTE, soul.name, user_name)))
    return apply_macros("\n\n".join(blocks), soul.name, user_name)


def assemble(soul: Soul, *, user_md: str, summary: str, memories: list[Memory],
             lore: list[LoreEntry], window: list[dict], user_msg: str,
             user_name: str = "you",
             knowledge: Sequence[Known] = (),
             goals: Sequence[str] = (),
             system_budget_tokens: int = 8000,
             lorebook_budget_tokens: int = 400,
             knowledge_budget_tokens: int = 900) -> AssembledPrompt:
    """Build the full message array for one turn (§7.1)."""

    # 4. lore — capped at LOREBOOK_BUDGET_TOKENS before anything else (§5.3)
    lore = list(lore)
    while lore and est_tokens("\n\n".join(e.content for e in lore)) > lorebook_budget_tokens:
        lore.pop()  # entries arrive ordered by insertion_order; trim from the tail

    # 8. knowledge — capped the same way, and for a sharper reason: a chunk is a
    # paragraph budget (~1200 chars), so three of them are worth a hundred
    # recalled memories in tokens. Trimming from the tail drops the
    # lowest-scoring chunk, because `search()` returns them ranked.
    knowledge = list(knowledge)
    while knowledge and est_tokens(
            "\n\n".join(c.text for c in knowledge)) > knowledge_budget_tokens:
        knowledge.pop()

    memories = list(memories)
    goals = list(goals)
    dropped_memories = dropped_lore = dropped_knowledge = dropped_goals = 0

    def build_system(mems: list[Memory], lore_now: list[LoreEntry],
                     known: list[Known], goals_now: list[str],
                     include_examples: bool) -> str:
        blocks: list[str] = [
            _block("VOICE LAW", soul.voice_law),
            _block("PERSONA BACKBONE",
                   f"{soul.backbone}\n\nPersonality: {soul.personality}"),
            _block("SCENARIO", soul.scenario),
        ]
        if lore_now:
            blocks.append(_block("LORE", "\n\n".join(
                f"[{e.name}] {e.content}" for e in lore_now)))
        blocks.append(_block("WHO YOU ARE TO HER", user_md or "(nothing yet)"))
        if goals_now:
            blocks.append(_block(
                "WHAT YOU'RE WORKING ON",
                apply_macros(GOALS_NOTE, soul.name, user_name) + "\n\n"
                + "\n".join(f"- {g}" for g in goals_now)))
        if summary.strip():
            blocks.append(_block("WHAT YOU'VE TALKED ABOUT", summary))
        if mems:
            blocks.append(_block("THINGS THAT MAY BE RELEVANT", "\n".join(
                f"- ({_age_tag(m)}) {m.text}" for m in mems)))
        if known:
            body = apply_macros(KNOWLEDGE_NOTE, soul.name, user_name) + "\n\n"
            body += "\n\n".join(f"{c.text.strip()}\n— {c.citation}" for c in known)
            blocks.append(_block("WHAT YOU'VE READ", body))
        blocks.append(_block("THE HONESTY CONSTRAINT",
                             apply_macros(HONESTY, soul.name, user_name)))
        if include_examples and soul.examples.strip():
            blocks.append(_block("EXAMPLE VOICE", soul.examples))
        return "\n\n".join(blocks)

    # §7.2 overflow policy: examples are the first luxury, then knowledge, then
    # recalled memories, then lore. Persona / USER.md / honesty are never dropped.
    #
    # Knowledge goes before memories on purpose. It is the bulkiest thing here,
    # so one chunk buys back what a dozen memories would; it is the most
    # replaceable, because the shelf is on disk and the same search runs again
    # next turn; and of everything in the prompt it is the least *hers*. Trading
    # away what she remembers about you to keep a paragraph of Wikipedia is the
    # wrong trade for this project, and the ladder is where that gets decided.
    include_examples = True

    def rebuild() -> str:
        return build_system(memories, lore, knowledge, goals, include_examples)

    system = rebuild()
    if est_tokens(system) > system_budget_tokens:
        include_examples = False
        system = rebuild()
    while est_tokens(system) > system_budget_tokens and knowledge:
        knowledge.pop()         # lowest-scoring chunk goes first
        dropped_knowledge += 1
        system = rebuild()
    while est_tokens(system) > system_budget_tokens and memories:
        memories.pop()          # lowest-ranked recalled memory goes first
        dropped_memories += 1
        system = rebuild()
    while est_tokens(system) > system_budget_tokens and lore:
        lore.pop()
        dropped_lore += 1
        system = rebuild()
    # …and only then her goals — the oldest first, because the list is appended
    # to and the thing she took on this morning is the one she is likeliest to
    # be about to re-promise.
    while est_tokens(system) > system_budget_tokens and goals:
        goals.pop(0)
        dropped_goals += 1
        system = rebuild()

    # hard limits AFTER the history (§7.1): fused onto the final user message so
    # they are the last thing read before replying (the Messages API folds
    # detached system messages to the top, which would defeat the point).
    final_user = user_msg
    if soul.hard_limits.strip():
        final_user = (f"{user_msg}\n\n"
                      f"[system note — hard limits, read last:\n"
                      f"{soul.hard_limits.strip()}]")

    messages = [{"role": "system", "content": system},
                *[{"role": m["role"], "content": m["content"]} for m in window],
                {"role": "user", "content": final_user}]
    return AssembledPrompt(system=system, messages=messages,
                           dropped_memories=dropped_memories,
                           dropped_lore=dropped_lore,
                           dropped_knowledge=dropped_knowledge,
                           dropped_goals=dropped_goals,
                           citations=[c.citation for c in knowledge])


#: What the final user turn says when a picture arrives with no words on it.
#: Sending a photo with no caption is an ordinary thing to do, and a user
#: message that is *only* an image part is where local chat templates start
#: behaving strangely — some drop the turn, some emit an empty prompt. One
#: sentence in the user's voice costs nothing and keeps the turn well formed.
IMAGE_ONLY_TEXT = "(I'm showing you this picture.)"


def with_image(messages: list[dict], data_url: str) -> list[dict]:
    """`messages`, with the picture attached to the turn that is asking (§35).

    Chat models take a multimodal turn as a *list* of parts instead of a string,
    and OpenAI's `image_url` shape is the one LiteLLM speaks to every route this
    project reaches. Only the final user message changes; everything above it is
    the same text prompt `assemble` built, which is what keeps the budgets, the
    corpus record and the token estimate honest.

    A copy, never in place: the caller keeps the text-only array as the record of
    the turn (`_Pending` → the corpus), because a base64 photo in a JSONL line is
    a training log nobody can read and a git diff nobody can review.
    """
    out = [dict(m) for m in messages]
    for message in reversed(out):
        if message.get("role") != "user":
            continue
        text = message.get("content")
        text = text if isinstance(text, str) else ""
        message["content"] = [
            {"type": "text", "text": text.strip() or IMAGE_ONLY_TEXT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return out
    raise ValueError("no user message to attach a picture to")


#: How a turn that carried a picture reads once the picture itself is gone —
#: in the session window a few turns later, and in the corpus record, neither of
#: which holds image bytes. Without it her half of that exchange dangles from a
#: line that says only "what do you think?", and the next prompt reads as a
#: question she answered out of nowhere.
PICTURE_NOTE = "[system note — a picture is attached to this message]"


def note_picture(text: str) -> str:
    """`text` with the picture note fused on, in the same register (and the same
    place) as the hard-limits note above: the end of the user's own message."""
    text = (text or "").strip()
    return f"{text}\n\n{PICTURE_NOTE}" if text else PICTURE_NOTE


def mark_picture(messages: list[dict]) -> None:
    """Fuse the picture note onto the assembled prompt's final user message.

    In place, and on the assembled array rather than on a copy, because that
    array *is* the record of the turn: it is what the corpus logs and what the
    prompt log points at. The image bytes never join it (`with_image` makes the
    copy that goes on the wire), so this note is the only thing in the record
    that says the turn had a picture in it at all.
    """
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = note_picture(message["content"])
            return
