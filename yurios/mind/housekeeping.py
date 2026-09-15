"""REGULATE's daily duties — the housekeeping a tick does between decisions.

Not the decisions themselves: this is the bookkeeping that has to happen
*around* them. The engine cursor written after the commit rather than before it
(§15.1), the local-midnight rollover that closes yesterday's day file and files
the leftovers, the maintenance goals that fall out of what the Vault looks like
this morning, and the bootstrap handoff that runs once and then never again.

Split out of `loop.py` because these are read for a different reason than a
tick is: when something did not happen overnight, or happened twice. `loop`
keeps `_persist` as a method because the tick body calls it at a precise point
in its own sequence, and that ordering is the whole of what it is for.

`loop` is unannotated for the reason `world/runtime.py` gives: naming its type
means importing `MindLoop`, and `tests/test_layering.py` reads a
`TYPE_CHECKING` import off the parse tree like any other.
"""
from __future__ import annotations

import logging

from yurios.app.memory import partner

from .selfedit import SoulShapeError
from .util import day_of, iso_of
from .vaultio import ConstitutionReadOnly

log = logging.getLogger("mind.housekeeping")


def day_rollover(loop, now: float) -> list[str]:
    """Once per local day: apply commitment strategies, roll the hands'
    caps, and file what has been left standing (SPEC §22.2, §26).

    `reconsider()` used to run on `suspend_gap` alone — i.e. only after the
    machine had slept two hours — so a goal that went stale while she was
    awake was defended forever by a strategy nobody ever asked. Cheap and
    idempotent, which is why once a day is enough and more would be noise.
    """
    today = day_of(now)
    if loop.reconsidered_on == today:
        return []
    loop.reconsidered_on = today
    loop.hands.roll()
    notes = [f"let go of: {g.text} (the moment for it passed)"
             for g in loop.goals.reconsider()]
    return notes + loop._file_maintenance()


def _note(delta: partner.PersonaDelta) -> str:
    """The journal gets the gist, not the whole approval sentence — the
    stakes clause is for the panel, where there is a button to press."""
    return delta.reason.split(". ", 1)[0]


def propose_learned_persona(loop) -> list[str]:
    """Queue a PERSONA.md edit when USER.md learned how they asked her to be.

    USER.md is the runtime's to write (§6.3). PERSONA.md is gated (§23): a
    preference like "Yandere-lite" sitting in the partner model forever is
    how design-for-evolution stayed aspirational. The door is the same
    self-edit queue every other identity write uses — she proposes, you
    approve, git log records it.
    """
    data = partner.read_persona_delta(loop.cfg.vault_dir)
    if not data or data.get("queued"):
        return []
    if data.get("edit_id"):
        loop.selfedit.withdraw(data["edit_id"])
    elif data.get("superseded_reason"):
        for edit in loop.selfedit.pending():
            if (edit["surface"] == "soul/PERSONA.md"
                    and edit["reason"] == data["superseded_reason"]):
                loop.selfedit.withdraw(edit["id"])
    lines = [str(x).strip() for x in data.get("lines") or [] if str(x).strip()]
    if not lines:
        return []
    delta = partner.PersonaDelta(
        lines=lines,
        phase=str(data.get("phase") or "mid"),
        reason=str(data.get("reason")
                   or partner.persona_reason(lines)),
    )
    try:
        persona = loop.vault.read("soul/PERSONA.md")
    except FileNotFoundError:
        return []
    if partner.learned_already_in_persona(persona, delta):
        partner.mark_persona_delta_queued(loop.cfg.vault_dir)
        return []
    try:
        new = partner.apply_learned_to_persona(persona, delta)
    except Exception:
        log.exception("persona delta could not be applied to PERSONA.md")
        return []
    try:
        result = loop.selfedit.propose(
            "soul/PERSONA.md", new, reason=delta.reason)
    except (SoulShapeError, ConstitutionReadOnly, PermissionError) as exc:
        log.warning("persona delta refused at the door: %s", exc)
        return []
    if result.outcome == "queued":
        partner.mark_persona_delta_queued(loop.cfg.vault_dir, result.id)
        return [f"queued a persona edit: {_note(delta)}"]
    if result.outcome == "applied":
        partner.mark_persona_delta_queued(loop.cfg.vault_dir)
        return [f"persona learned: {_note(delta)}"]
    return []


def leftover(loop, which: str) -> bool:
    if which == "shelf":
        return bool(loop.knowledge.pending_docs())
    return bool(loop.cfg.dream_enabled and loop.cfg.utility_enabled
                and loop.dreams.backlog())


def file_maintenance(loop) -> list[str]:
    """Standing leftovers become goals; cleared ones close themselves.

    Ingest and DREAM stay impulses — that is the cheap path and it is
    right. What was missing is that a shelf nobody got round to for three
    days is a *commitment*, and the goals page is where commitments are
    visible. Documented in `goals.py` as `maintenance:*` since the store was
    written, and never created until now.
    """
    notes: list[str] = []
    # Keyed on `meta.auto`, which only this method ever writes — NOT on the
    # provenance, which a person or a dream job may reasonably reuse. A
    # reconciler that closed every goal wearing a familiar label would close
    # work she filed herself, which is the opposite of a maintenance sweep.
    open_now = {g.meta.get("auto"): g for g in loop.goals.open_goals()
                if g.meta.get("auto")}
    for which, provenance, text, due_hours in loop.MAINTENANCE:
        existing = open_now.get(which)
        if loop._leftover(which):
            if existing is not None:
                continue
            loop.goals.add(
                text, kind="maintenance", priority=0.4,
                due=(iso_of(loop.clock.now() + due_hours * 3600)
                     if due_hours else None),
                # `single-minded`, not `open-minded`, and the due time is
                # why: `reconsider()` abandons an open-minded goal the
                # moment it goes stale, which for a due-dated one is the
                # very tick it becomes worth doing. A leftover drops when it
                # is moot, and the reconciler above is what decides that —
                # the shelf is empty, so the goal closes `done`.
                commitment="single-minded", provenance=provenance,
                meta={"auto": which})
            notes.append(f"put it on my own list: {text}")
        elif existing is not None:
            loop.goals.set_state(existing.id, "done")
            notes.append(f"cleared: {existing.text}")
    return notes


def bootstrap_handoff(loop, now: float) -> list[str]:
    """The first session's handoff, once (SPEC §5.4, `soul-src/BOOTSTRAP.md`).

    The bootstrap file names three things the runtime should be left holding
    when it retires: a seeded `USER.md`, a first `MEMORY.md` line, and one
    standing `reach_out` in `goals.md`. The first two are the post-turn
    pipeline's, and it already does them (`app/memory/partner.py`,
    `store.remember`). The third was prose and nothing else — so the very
    first thing she ever learned about you produced no intention at all.

    Detected here rather than pushed from the retirement site because
    retirement happens in three places across two servers, none of which can
    reach a GoalStore without the app layer importing the mind layer. File
    presence is the flag, exactly as it is for the greeting; the date is
    persisted so this is a one-shot, not a thing that re-files whenever the
    goal is completed.
    """
    if loop.bootstrapped_on:
        return []
    soul = loop.cfg.vault_dir / "soul"
    if (soul / "BOOTSTRAP.md").is_file():
        return []                          # she has not met you yet
    if not (soul / "onboarded" / "BOOTSTRAP.done.md").is_file():
        # No bootstrap was ever installed (an imported card, a hand-built
        # vault). Nothing to hand off, and nothing to keep checking for.
        loop.bootstrapped_on = day_of(now)
        return []
    loop.bootstrapped_on = day_of(now)
    user_model = loop.vault.read("soul/USER.md")
    ongoing = ""
    in_ongoing = False
    for line in user_model.splitlines():
        if line.strip().lower() == "## ongoing":
            in_ongoing = True
            continue
        if in_ongoing and line.startswith("## "):
            break
        if in_ongoing and line.lstrip().startswith("-"):
            candidate = line.lstrip()[1:].strip()
            if candidate and not candidate.startswith("_("):
                ongoing = candidate
                break
    if not ongoing:
        return ["our first conversation is done; no concrete follow-up was left open"]
    goal = loop.goals.add(
        f"ask {loop.cfg.user_name} about this ongoing thread: {ongoing}",
        kind="reach_out", priority=0.55,
        due=iso_of(now + 36 * 3600), commitment="open-minded",
        provenance="bootstrap:first-session", meta={"about": ongoing})
    return [f"our first conversation is done; I want to follow it up: {goal.text}"]
