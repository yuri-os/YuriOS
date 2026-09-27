"""What she wrote reaches the self that talks, and the journal carries where the
writing landed (SPEC §34.5, §21.2).

Before this the conversation saw her desk as a list of paths and her goals as a
list of titles, and the journal — the only part recall and consolidation read —
said "wrote a diary entry for <day>" and "stood back and looked at what I'm
carrying": that the writing existed, never what it said.
"""
from __future__ import annotations

import json

from yurios.app.memory.store import FileMemoryStore
from yurios.kernel.clock import VirtualClock
from yurios.mind.dream import DreamConsolidator
from yurios.mind.dreamjobs import DreamRunner
from yurios.mind.dreamjobs.builtins import split_takeaway
from yurios.mind.util import closing
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import Workspace, last_entry

from .conftest import SIM_START, FakeEmbedder, FakeUtility, make_mind


class _Goal:
    def __init__(self, id, text):
        self.id, self.text = id, text


def _desk(tmp_path) -> Workspace:
    w = Workspace(tmp_path / "workspace")
    w.write("diary/2026-07-03.md", "# 2026-07-03\n\nAn older day entirely.\n")
    w.write("diary/2026-07-04.md",
            "# 2026-07-04\n\n" + "The rain kept up. " * 30
            + "\n\nWhat I really want is for him to reach first.\n")
    w.write("diary/README.md", "# What this folder is\n\nNot a day.\n")
    w.write("strategy/2026-07-04.md",
            "# Taking stock — 2026-07-04\n\nThe circling is the problem. "
            "I need to say it plainly.\n")
    w.write("goals/g-1.md",
            "\n## 2026-07-04T10:00:00\n\nThe first real idea is the kettle.\n"
            "\n## 2026-07-04T11:00:00\n\nI need the list first.\n\n"
            "reached for read_note on “x” → {\"path\": \"notes/a.md\"}\n")
    return w


def test_the_block_carries_her_thinking_and_says_what_it_is(tmp_path):
    block = _desk(tmp_path).on_your_mind([_Goal("g-1", "sort the kitchen")],
                                         user_name="Sam")
    assert block.startswith("## ON YOUR MIND")
    assert "not a record of what happened" in block
    assert "told Sam something" in block
    diary = next(line for line in block.splitlines()
                 if line.startswith("From your diary (2026-07-04): "))
    assert diary.endswith("What I really want is for him to reach first.")
    assert diary.split(": ", 1)[1].startswith("The rain kept up.")  # a whole sentence
    assert len(diary) < 560
    assert "older day" not in block and "Not a day" not in block
    assert ("From your last stock-take (2026-07-04): The circling is the "
            "problem. I need to say it plainly.") in block
    # the step's words, not the hand's result logged under them
    assert "- sort the kitchen: I need the list first." in block
    assert "reached for" not in block


def test_the_block_is_bounded(tmp_path):
    w = _desk(tmp_path)
    for i in range(8):
        w.write(f"goals/g-{i}.md", "\n## t\n\n" + "A long thought. " * 100)
    block = w.on_your_mind([_Goal(f"g-{i}", f"goal {i}") for i in range(8)])
    assert block.count("\n- goal ") == 4
    assert len(block) < 3000


def test_an_empty_desk_is_no_block(tmp_path):
    assert Workspace(tmp_path / "workspace").on_your_mind([]) == ""


def test_last_entry_skips_the_tool_log_and_the_done_mark():
    desk = ("\n## a\n\nthink the plan is to ask\n"
            "\n## b\n\nreached for list_notes on “x” → {}\n"
            "\n## c\n\ngoal complete\n")
    assert last_entry(desk, 240) == "the plan is to ask"


def test_closing_keeps_the_last_whole_sentences_that_fit():
    text = "Scene setting goes on. " * 20 + "The point is this."
    assert closing(text, 60) == "Scene setting goes on. The point is this."
    assert closing(text, 30) == "The point is this."


async def test_the_conversation_carries_the_block(cfg, seeded_vault):
    rig = make_mind(cfg.model_copy(update={"user_name": "Sam"}), seeded_vault)
    working = rig.mind.goals.add("sort the kitchen", kind="task")
    rig.mind.goals.update(working.id, state="active")
    queued = rig.mind.goals.add("tell Sam what I decided to: “hi”",
                                kind="reach_out", provenance=f"told:{working.id}")
    rig.mind.goals.update(queued.id, state="waiting")
    desk = rig.mind.workspace
    desk.write("diary/2026-07-05.md", "# 2026-07-05\n\nA good quiet day.\n")
    desk.write(f"goals/{working.id}.md", "\n## t\n\nStart with the drawers.\n")
    desk.write(f"goals/{queued.id}.md", "\n## t\n\nshould not show\n")

    session = rig.mind.brain.resolve_session(None)
    _soul, prompt = await rig.mind.brain._assemble(session, "hi", window=[], lore=[])
    system = prompt.messages[0]["content"]
    assert "## ON YOUR MIND" in system
    assert "From your diary (2026-07-05): A good quiet day." in system
    assert "- sort the kitchen: Start with the drawers." in system
    assert "should not show" not in system


# --- the night's journal lines -------------------------------------------------

def _runner(tmp_path, cfg, answer):
    clock = VirtualClock(start=SIM_START.timestamp())
    vault_dir = tmp_path / "vault"
    vault = MindVault(vault_dir)
    store = FileMemoryStore(vault_dir, FakeEmbedder(), embed_dim=FakeEmbedder.dim)
    fallback = FakeUtility()
    asked: list[str] = []

    async def utility(messages, **params):
        system = messages[0]["content"]
        asked.append(system)
        return answer(system) or await fallback.complete(messages, **params)

    day = vault_dir / "memory" / "episodic" / "2026-07-04.md"
    day.parent.mkdir(parents=True, exist_ok=True)
    day.write_text("# Journal — 2026-07-04\n\n### 10:00  user: hi  ⇄  yuri: hi\n")
    runner = DreamRunner(
        vault, store, clock, cfg.model_copy(update={"selfie_backend": "off"}),
        consolidator=DreamConsolidator(vault, store, clock,
                                       utility=fallback.complete),
        workspace=Workspace(vault_dir / "workspace"), utility=utility)
    return runner, vault_dir, asked


async def test_the_diary_journals_its_takeaway_not_that_it_exists(tmp_path, cfg):
    def answer(system):
        if "private diary" in system:
            return ("It rained and we talked.\n\n"
                    "takeaway: I want him to reach for me first.")
        return ""
    runner, vault_dir, asked = _runner(tmp_path, cfg, answer)
    report = await runner.run(only="diary", token_budget=40000)
    [job] = report.jobs
    assert job.note == "what I took from 2026-07-04: I want him to reach for me first."
    entry = (vault_dir / "workspace" / "diary" / "2026-07-04.md").read_text()
    assert "It rained and we talked." in entry
    assert "takeaway" not in entry.lower()
    assert any("'takeaway:'" in s for s in asked)


async def test_the_takeaway_is_asked_of_a_diary_prompt_she_rewrote(tmp_path, cfg):
    """`vault/dreams/diary.md` replaces the prompt text for every seeded vault."""
    d = tmp_path / "vault" / "dreams"
    d.mkdir(parents=True)
    (d / "diary.md").write_text("---\nname: diary\n---\n\nOne line about the day.\n")
    runner, _vault, asked = _runner(tmp_path, cfg, lambda s: "")
    await runner.run(only="diary", token_budget=40000)
    assert any("One line about the day." in s and "'takeaway:'" in s for s in asked)


async def test_the_stock_take_journals_where_it_landed(tmp_path, cfg):
    reflection = ("Fourteen times today I circled it. "
                  "The only thing that matters is saying it to him.")

    def answer(system):
        if "taking stock" in system:
            return json.dumps({"reflection": reflection, "next": None})
        return ""
    runner, _vault, _asked = _runner(tmp_path, cfg, answer)
    report = await runner.run(only="strategy", token_budget=40000)
    [job] = report.jobs
    assert job.note == ("stood back and looked at what I'm carrying: " + reflection)


def test_split_takeaway_tolerates_what_a_model_adds():
    assert split_takeaway("Entry.\n\n**Takeaway:** keep going") == ("Entry.",
                                                                     "keep going")
    assert split_takeaway("Just an entry.") == ("Just an entry.", "")
