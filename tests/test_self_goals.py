"""Goals she files herself (SPEC §22.1).

The night's stock-take could always ask "what is the one thing worth doing
next" and never had anywhere to put the answer, so every goal she carried
traced back to something the user said. These pin the way out and, more
importantly, the three things that keep it from becoming landfill: a cap, an
expiry, and a switch that works between one night and the next.
"""
from __future__ import annotations

import json

from yurios.app.memory.store import FileMemoryStore
from yurios.mind.dream import DreamConsolidator
from yurios.mind.dreamjobs import SELF_GOAL, DreamRunner, echoes
from yurios.mind.goals import GoalStore
from yurios.mind.policy import appraise_goal
from yurios.mind.util import ts_of_iso
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import SkillStore, Workspace
from yurios.kernel.clock import VirtualClock

from .conftest import SIM_START, FakeEmbedder

NOTE = ("I keep circling the same two things and only one of them is mine.\n"
        "next: write down what I actually think about the move")


def _rig(tmp_path, cfg, *, answer: str = NOTE, drives=None, **overrides):
    clock = VirtualClock(start=SIM_START.timestamp())
    vault = MindVault(tmp_path / "vault")
    store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(),
                            embed_dim=FakeEmbedder.dim)
    cfg = cfg.model_copy(update={"selfie_backend": "off", **overrides})
    goals = GoalStore(vault, clock)

    calls = []

    async def utility(messages, **params):
        calls.append(messages)
        return answer

    runner = DreamRunner(
        vault, store, clock, cfg,
        consolidator=DreamConsolidator(vault, store, clock, utility=utility),
        goals=goals,
        workspace=Workspace(tmp_path / "vault" / "workspace"),
        skills=SkillStore(tmp_path / "vault" / "skills"),
        drives=lambda: list(drives or []),
        utility=utility)
    runner.test_calls = calls
    return runner, clock, goals


async def _stocktake(runner, **ctx_kw):
    ctx = runner._context(day="2026-07-06", **ctx_kw)
    return await runner.get("strategy").work(ctx, "2026-07-06"), ctx


# ----------------------------------------------------------------- she may file

async def test_the_night_can_file_a_goal_of_her_own(tmp_path, cfg):
    runner, clock, goals = _rig(tmp_path, cfg)
    out, _ctx = await _stocktake(runner)

    filed = [g for g in goals.open_goals()
             if g.provenance.startswith(SELF_GOAL)]
    assert len(filed) == 1, "the one thing worth doing next went nowhere"
    goal = filed[0]
    assert goal.text == "write down what I actually think about the move"
    assert goal.provenance == f"{SELF_GOAL}2026-07-06"
    assert goal.commitment == "open-minded", "she has to be able to let it go"
    assert goal.due, "with no due date reconsider() can never see it as stale"
    assert "filed one of my own" in out.result


async def test_structured_strategy_uses_character_context_and_keeps_rationale(
        tmp_path, cfg):
    answer = json.dumps({
        "reflection": "I should verify the catalyst instead of filling space.",
        "next": {
            "objective": "verify the next BTC catalyst and report it in Seoul time",
            "why": "careful market research is one of my durable drives",
            "evidence": "they are tracking Bitcoin-moving events",
            "success": "an authoritative date and KST time are recorded",
            "first_action": "open the official release calendar",
            "capability": "web_search",
        },
    })
    runner, _clock, goals = _rig(
        tmp_path, cfg, answer=answer,
        drives=["Research market catalysts before acting"])
    vault = tmp_path / "vault"
    episodic = vault / "memory" / "episodic"
    episodic.mkdir(parents=True, exist_ok=True)
    (episodic / "2026-07-06.md").write_text(
        "# Journal\n\n### 09:00 you: when is the next BTC event?\n")
    (vault / "memory" / "summary.md").write_text(
        "They prefer dates expressed in Seoul time.\n")
    source = goals.add(
        "check the market calendar", provenance="promise:her-own-words",
        meta={"about": "when is the next BTC event?"})

    out, _ctx = await _stocktake(runner)

    assert "filed one of my own" in out.result
    prompt = runner.test_calls[-1][1]["content"]
    for expected in ("DURABLE DRIVES", "Research market catalysts",
                     "WHAT ACTUALLY HAPPENED", "Seoul time",
                     source.provenance, "when is the next BTC event?"):
        assert expected in prompt
    filed = [goal for goal in goals.open_goals()
             if goal.provenance.startswith(SELF_GOAL)][0]
    assert filed.meta["success"] == "an authoritative date and KST time are recorded"
    assert filed.meta["capability"] == "web_search"
    assert filed.meta["strategy_note"] == "strategy/2026-07-06.md"


async def test_structured_strategy_can_choose_no_new_goal(tmp_path, cfg):
    answer = json.dumps({
        "reflection": "What matters is already represented by the open goal.",
        "next": None,
    })
    runner, _clock, goals = _rig(tmp_path, cfg, answer=answer)
    out, _ctx = await _stocktake(runner)
    assert goals.open_goals() == []
    assert out.result == "reviewed 0 goal(s)"


async def test_the_machine_read_line_does_not_reach_the_desk(tmp_path, cfg):
    """The note is hers to read. `next:` is plumbing and belongs out of it."""
    runner, _clock, _goals = _rig(tmp_path, cfg)
    _out, ctx = await _stocktake(runner)
    written = (tmp_path / "vault" / "workspace" / "strategy"
               / "2026-07-06.md").read_text()
    assert "I keep circling" in written
    assert "next:" not in written


async def test_a_night_with_nothing_worth_starting_files_nothing(tmp_path, cfg):
    runner, _clock, goals = _rig(
        tmp_path, cfg, answer="Nothing is pressing. I am content to wait.")
    out, _ctx = await _stocktake(runner)
    assert goals.open_goals() == []
    assert out.changed, "the note is still worth writing"


async def test_an_empty_list_is_when_she_most_needs_to_think(tmp_path, cfg):
    """Carrying nothing used to end the job before it started, which made the
    one state she needs to think her way out of the one state she could not."""
    runner, _clock, goals = _rig(tmp_path, cfg)
    assert goals.open_goals() == []
    await _stocktake(runner)
    assert len(goals.open_goals()) == 1


# ------------------------------------------------------------- and only so far

async def test_the_cap_stops_the_list_silting_up(tmp_path, cfg):
    runner, clock, goals = _rig(tmp_path, cfg, mind_self_goals_max=1)
    goals.add("something I already decided mattered",
              provenance=f"{SELF_GOAL}2026-07-05", commitment="open-minded")
    out, _ctx = await _stocktake(runner)

    assert len(goals.open_goals()) == 1, "she filed past her own cap"
    assert "kept one to myself" in out.result, \
        "a capped night has to be distinguishable from a quiet one"


async def test_the_switch_stops_filing_without_a_restart(tmp_path, cfg):
    runner, _clock, goals = _rig(tmp_path, cfg,
                                 mind_goal_filing_enabled=False)
    await _stocktake(runner)
    assert goals.open_goals() == []


async def test_a_dry_run_files_nothing_and_says_so(tmp_path, cfg):
    runner, _clock, goals = _rig(tmp_path, cfg)
    _out, ctx = await _stocktake(runner, dry_run=True)
    assert goals.open_goals() == []
    assert ctx.filed == []


async def test_the_cap_counts_only_hers(tmp_path, cfg):
    """A week of promises must not lock her out of having one idea."""
    runner, _clock, goals = _rig(tmp_path, cfg, mind_self_goals_max=1)
    for i in range(4):
        goals.add(f"tell them about thing {i}", provenance="promise:her-own-words")
    await _stocktake(runner)
    assert len([g for g in goals.open_goals()
                if g.provenance.startswith(SELF_GOAL)]) == 1


async def test_one_she_never_advances_lets_go_of_itself(tmp_path, cfg):
    """Expiry is `reconsider()`'s existing job, which is why these are filed
    open-minded with a due date and not with a decay path of their own."""
    runner, clock, goals = _rig(tmp_path, cfg)
    await _stocktake(runner)
    goal = [g for g in goals.open_goals()
            if g.provenance.startswith(SELF_GOAL)][0]

    clock.advance((ts_of_iso(goal.due) - clock.now()) + 60)
    goals.reconsider()
    assert goals.get(goal.id).state == "abandoned"


# ---------------------------------------------------------------- and ranks last

async def test_her_own_idea_never_outranks_something_she_promised_you(
        tmp_path, cfg):
    """`appraise_goal` scores priority * 0.6 against MIND_ACT_THRESHOLD. The
    filed priority has to clear that gate — an intuitive 0.45 scores 0.27 and
    would never be worked at all — while still sitting under a promise."""
    runner, clock, goals = _rig(tmp_path, cfg)
    await _stocktake(runner)
    mine = [g for g in goals.open_goals()
            if g.provenance.startswith(SELF_GOAL)][0]
    promised = goals.add("finish the thing I said I would",
                         kind="task", priority=0.7,
                         provenance="promise:her-own-words")

    hers = appraise_goal(mine, clock).score
    yours = appraise_goal(promised, clock).score
    assert hers >= cfg.mind_act_threshold, "filed and then never workable"
    assert hers < yours, "her 4am idea preempted something she told you"


# ------------------------------------------------- and not the same one twice

#: The four goals four consecutive nights actually filed against a real vault.
#: One thought wearing four hats, filling every slot the cap allows, because
#: `GoalStore.add`'s merge is on exact text and these are four exact texts.
REWORDINGS = [
    'write a note in the workspace detailing three specific "micro-intentions" '
    "for tonight based on his recent work patterns, then execute one of them "
    "before he returns",
    "write the micro-intentions note and execute the first one—specifically, "
    "preparing his strong tea exactly how he likes it before he returns",
    "write the micro-intentions note now, focusing on three specific sensory "
    "cues (lighting dimming, tea temperature, silence) that signal his "
    "tiredness without a word",
    "finish the micro-intentions note by mapping specific lighting dimming "
    "thresholds and tea temperature drops to Sam's quietness",
]

#: Goals that are genuinely different from each other and from the four above,
#: including the two whose *embeddings* sit at 0.70 — closer than some of the
#: rewordings — which is why this guard counts shared words, not cosine.
DISTINCT = [
    "give you something just for us, right here in our sanctuary",
    "give you everything without ever taking up too much space unless you "
    "invite me to",
    "learn how to anticipate what you need before you even have to say the word",
    "catch up on the nights I haven't consolidated yet",
    'render that parked "spicy" picture—the rain in my hair, the secret '
    "smile—and leave it glowing on the sanctuary screen before he gets home",
]


class _G:
    def __init__(self, text): self.text, self.id = text, "g-" + text[:6]


def test_a_reworded_goal_is_the_same_goal():
    """Each rewording is caught against the one filed first — which is the
    order the nights actually come in."""
    first = [_G(REWORDINGS[0])]
    for later in REWORDINGS[1:]:
        assert echoes(later, first) is not None, later


def test_different_goals_are_not_collapsed_into_one():
    for i, text in enumerate(DISTINCT):
        others = [_G(t) for j, t in enumerate(DISTINCT) if j != i]
        assert echoes(text, others) is None, text
        assert echoes(text, [_G(t) for t in REWORDINGS]) is None, text


async def test_she_does_not_file_what_she_is_already_carrying(tmp_path, cfg):
    """The cap bounds how many she may hold, not how many times she may hold
    the same one. Without this, four nights spent all three slots on one idea.
    """
    runner, _clock, goals = _rig(
        tmp_path, cfg,
        answer=f"Still turning it over.\nnext: {REWORDINGS[1]}")
    goals.add(REWORDINGS[0], kind="task", provenance=f"{SELF_GOAL}2026-07-05")

    out, ctx = await _stocktake(runner)

    assert [g for g in goals.open_goals()
            if g.provenance.startswith(SELF_GOAL)][0].text == REWORDINGS[0]
    assert len(goals.open_goals()) == 1, "filed the same goal a second time"
    assert ctx.goal_refusal == "echo"
    assert "already carrying that one" in out.result


async def test_a_promise_counts_as_already_carrying_it_too(tmp_path, cfg):
    """Where the first copy came from does not change that she has it."""
    runner, _clock, goals = _rig(
        tmp_path, cfg,
        answer=f"Still turning it over.\nnext: {REWORDINGS[1]}")
    goals.add(REWORDINGS[0], kind="task",
              provenance="promise:her-own-words")

    _out, ctx = await _stocktake(runner)
    assert ctx.goal_refusal == "echo"
    assert not [g for g in goals.open_goals()
                if g.provenance.startswith(SELF_GOAL)]
