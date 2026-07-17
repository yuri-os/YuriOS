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
    8. THE HONESTY CONSTRAINT          — fixed text (§7.4, property 2)
    9. EXAMPLE VOICE                   — optional, if budget allows

On overflow: drop recalled memories first, lorebook second; NEVER the voice
law, persona, USER.md, or the honesty constraint (§7.2). Hard limits land
AFTER the history (V2/V3 post-history semantics) — fused onto the final user
message, the last thing read before replying.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from yurios.app.core.soul import LoreEntry, Soul, apply_macros
from yurios.app.memory.store import Memory

# bump whenever the assembly layout changes — stamped on every corpus record (§8.2)
TEMPLATE_VERSION = "b1-assemble-v1"

# §7.4 — the honesty constraint, fixed text, verified by the golden transcript test
HONESTY = """\
You remember only what is in the memory blocks above and this conversation. If \
{{user}} asks about something you have no record of, say so warmly and plainly — \
"I don't think you've told me that yet" — and ask, rather than inventing a \
memory. The same rule runs the other way: when {{user}} tells you something new, \
take it as new — never respond with "I remember" or "you told me" details that \
are not actually in the blocks above. Never fabricate a shared past."""


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


def _block(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}"


def assemble(soul: Soul, *, user_md: str, summary: str, memories: list[Memory],
             lore: list[LoreEntry], window: list[dict], user_msg: str,
             user_name: str = "you",
             system_budget_tokens: int = 8000,
             lorebook_budget_tokens: int = 400) -> AssembledPrompt:
    """Build the full message array for one turn (§7.1)."""

    # 4. lore — capped at LOREBOOK_BUDGET_TOKENS before anything else (§5.3)
    lore = list(lore)
    while lore and est_tokens("\n\n".join(e.content for e in lore)) > lorebook_budget_tokens:
        lore.pop()  # entries arrive ordered by insertion_order; trim from the tail

    memories = list(memories)
    dropped_memories = dropped_lore = 0

    def build_system(mems: list[Memory], lore_now: list[LoreEntry],
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
        if summary.strip():
            blocks.append(_block("WHAT YOU'VE TALKED ABOUT", summary))
        if mems:
            blocks.append(_block("THINGS THAT MAY BE RELEVANT", "\n".join(
                f"- ({_age_tag(m)}) {m.text}" for m in mems)))
        blocks.append(_block("THE HONESTY CONSTRAINT",
                             apply_macros(HONESTY, soul.name, user_name)))
        if include_examples and soul.examples.strip():
            blocks.append(_block("EXAMPLE VOICE", soul.examples))
        return "\n\n".join(blocks)

    # §7.2 overflow policy: examples are the first luxury, then recalled
    # memories, then lore. Persona / USER.md / honesty are never dropped.
    include_examples = True
    system = build_system(memories, lore, include_examples)
    if est_tokens(system) > system_budget_tokens:
        include_examples = False
        system = build_system(memories, lore, include_examples)
    while est_tokens(system) > system_budget_tokens and memories:
        memories.pop()          # lowest-ranked recalled memory goes first
        dropped_memories += 1
        system = build_system(memories, lore, include_examples)
    while est_tokens(system) > system_budget_tokens and lore:
        lore.pop()
        dropped_lore += 1
        system = build_system(memories, lore, include_examples)

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
                           dropped_lore=dropped_lore)
