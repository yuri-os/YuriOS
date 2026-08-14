"""Prompt assembly (SPEC §7) — the single most important function.

Composes the model input from SOUL (static) + Vault (current) + a small raw
window. Block ordering and budgets are normative (§7.1–7.2):

    1. VOICE LAW                       — CONSTITUTION#Voice law
    2. PERSONA BACKBONE                — identity · history · appearance · manner
    3. SCENARIO / PLACE                — SCENARIO#Scenario
    4. LORE                            — matched WORLD.md entries (this turn)
    5. WHO YOU ARE TO HER              — vault/soul/USER.md, whole (it's small)
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
lorebook; NEVER the voice law, persona, USER.md, or the honesty constraint
(§7.2). Hard limits land AFTER the history (V2/V3 post-history semantics) —
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
    citations: list[str] = field(default_factory=list)   # what block 8 carried


def _block(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}"


def assemble(soul: Soul, *, user_md: str, summary: str, memories: list[Memory],
             lore: list[LoreEntry], window: list[dict], user_msg: str,
             user_name: str = "you",
             knowledge: Sequence[Known] = (),
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
    dropped_memories = dropped_lore = dropped_knowledge = 0

    def build_system(mems: list[Memory], lore_now: list[LoreEntry],
                     known: list[Known], include_examples: bool) -> str:
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
    system = build_system(memories, lore, knowledge, include_examples)
    if est_tokens(system) > system_budget_tokens:
        include_examples = False
        system = build_system(memories, lore, knowledge, include_examples)
    while est_tokens(system) > system_budget_tokens and knowledge:
        knowledge.pop()         # lowest-scoring chunk goes first
        dropped_knowledge += 1
        system = build_system(memories, lore, knowledge, include_examples)
    while est_tokens(system) > system_budget_tokens and memories:
        memories.pop()          # lowest-ranked recalled memory goes first
        dropped_memories += 1
        system = build_system(memories, lore, knowledge, include_examples)
    while est_tokens(system) > system_budget_tokens and lore:
        lore.pop()
        dropped_lore += 1
        system = build_system(memories, lore, knowledge, include_examples)

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
                           citations=[c.citation for c in knowledge])
