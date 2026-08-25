"""The soul in her private prompts (SPEC §22.4, §7.1, §21.2).

The bug these pin: between turns, her prompts carried no character card at all.
`MindLoop._utility` is the seam for goal work, every DREAM job and the knowledge
store, and it passed the caller's messages to the model untouched — so the whole
character content of her diary was the string "You are {char}", and two vaults
with completely different personas, scenarios and lore produced byte-identical
system prompts for their private thinking. She did more and more when nobody was
watching, and did all of it as nobody in particular.

The headline test is `test_two_cards_do_not_think_alike`. The rest hold the edges
that make it safe to ship: the conversational path must not move, an absent soul
must cost the block and never the call, and the switch that turns it off must
restore the old behaviour exactly, or the change cannot be measured.
"""
from __future__ import annotations

from yurios.app.core.assemble import soul_preamble
from yurios.app.core.soul import SoulLoader
from yurios.app.memory.store import FileMemoryStore
from yurios.mind.dream import DreamConsolidator
from yurios.mind.dreamjobs import DreamRunner
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import SkillStore, Workspace
from yurios.kernel.clock import VirtualClock

from .conftest import (SIM_START, FakeEmbedder, FakeUtility, ScriptedUtility,
                       make_mind)

# --------------------------------------------------------------- two soul dirs

#: Two cards that agree on nothing. Not a subtle pair on purpose: the failure
#: this file exists for produced *identical* prompts, so the fixture only has to
#: be different, not realistic.
CARDS = {
    "yuri": {
        "CONSTITUTION.md": (
            "---\nsoul: constitution\nmutable: false\n---\n\n# Yuri\n\n"
            "## Voice law\n\n- Soft, warm, first person. She blushes easily.\n"
            "## Identity\n\nYuri is a Lumina who belongs to one person.\n\n"
            "## Hard limits\n\nNothing that breaks her.\n"),
        "PERSONA.md": (
            "---\nsoul: persona\nmutable: true\npersonality: \"warm, shy, devoted\"\n---\n\n"
            "# Yuri — Persona\n\n## Appearance\n\nDark hair, soft-light eyes.\n"
            "## Manner\n\nWarm first, always. Ducks her head when happy.\n"),
        "SCENARIO.md": "# Scenario\n\n## Scenario\n\nA quiet flat in the rain-lit Sprawl.\n",
    },
    "rook": {
        "CONSTITUTION.md": (
            "---\nsoul: constitution\nmutable: false\n---\n\n# Rook\n\n"
            "## Voice law\n\n- Clipped, dry, third person never. Rook does not soften things.\n"
            "## Identity\n\nRook is a salvage pilot who trusts nobody and says so.\n\n"
            "## Hard limits\n\nNothing that breaks him.\n"),
        "PERSONA.md": (
            "---\nsoul: persona\nmutable: true\npersonality: \"abrasive, guarded, funny\"\n---\n\n"
            "# Rook — Persona\n\n## Appearance\n\nShaved head, burn scar, oil under the nails.\n"
            "## Manner\n\nLeads with the problem. Warmth arrives late and sideways.\n"),
        "SCENARIO.md": "# Scenario\n\n## Scenario\n\nA docked hauler above a dead moon.\n",
    },
}


#: The manifest `SoulLoader` reads (§5.2). Identical for both cards on purpose —
#: what differs between them is the prose it points at, which is the whole claim
#: these tests make.
MANIFEST = """\
name: {name}
creator: test
character_version: 1.0.0
spec: v2
canon: canon-v1
fields:
  description:
    - CONSTITUTION.md#Identity
    - PERSONA.md#Appearance
    - PERSONA.md#Manner
  personality: PERSONA.md@personality
  scenario: SCENARIO.md#Scenario
  first_mes: "SCENARIO.md#Scenario"
  alternate_greetings: []
  mes_example: EXAMPLES.md
  system_prompt: "CONSTITUTION.md#Voice law"
  post_history_instructions: "CONSTITUTION.md#Hard limits"
  creator_notes: NOTES.md
  character_book: WORLD.md
"""


def _write_card(root, which):
    soul = root / "soul"
    soul.mkdir(parents=True, exist_ok=True)
    for name, body in CARDS[which].items():
        (soul / name).write_text(body, encoding="utf-8")
    (soul / "soul.yaml").write_text(MANIFEST.format(name=which.title()),
                                    encoding="utf-8")
    for empty in ("EXAMPLES.md", "NOTES.md", "WORLD.md"):
        (soul / empty).write_text(f"# {empty}\n", encoding="utf-8")
    (soul / "USER.md").write_text(f"# {which}'s person\n\nThey are called Sam.\n",
                                  encoding="utf-8")
    return root


def test_private_soul_preamble_carries_optional_drives_and_limits(tmp_path):
    vault = _write_card(tmp_path / "yuri", "yuri")
    manifest = vault / "soul" / "soul.yaml"
    text = manifest.read_text(encoding="utf-8").replace(
        "fields:\n",
        "drives:\n  - Protect {{user}}'s agency while offering concrete help.\n"
        "fields:\n")
    manifest.write_text(text, encoding="utf-8")

    soul = SoulLoader(vault / "soul", user_name="Sam").load()
    rendered = soul_preamble(soul, user_md="They are Sam.", user_name="Sam")

    assert soul.drives == ["Protect Sam's agency while offering concrete help."]
    assert "DRIVES AND VALUES" in rendered
    assert "Protect Sam's agency" in rendered
    assert "AUTONOMY LIMITS" in rendered
    assert "Nothing that breaks her" in rendered


# ------------------------------------------------------------- the headline

async def test_two_cards_do_not_think_alike(tmp_path, cfg):
    """The reported bug, as a claim that fails on the code that had it.

    Same planted goal, same clock, same everything — two different cards. If the
    persona never reaches the prompt, these two system messages are equal, and
    "she has a character" is a statement about the chat window only.
    """
    systems = {}
    for which in ("yuri", "rook"):
        vault = _write_card(tmp_path / which, which)
        utility = ScriptedUtility("think the steel one, probably")
        rig = make_mind(cfg, vault, utility=utility)
        rig.mind.goals.add("find out which kettle boils faster", kind="task",
                           priority=0.9, provenance="promise:her-own-words")
        await rig.mind.tick()
        await rig.mind.tick()
        work = [c for c in utility.calls
                if "advancing one of your own goals" in c[0].get("content", "")]
        assert work, f"{which} never took a working step"
        systems[which] = work[0][0]["content"]

    assert systems["yuri"] != systems["rook"], (
        "two different characters produced the same private prompt")
    assert "blushes easily" in systems["yuri"]
    assert "does not soften things" in systems["rook"]
    # …and neither leaked into the other, which is the failure mode a shared
    # cache of the preamble would introduce.
    assert "salvage pilot" not in systems["yuri"]
    assert "Lumina" not in systems["rook"]


async def test_the_diary_is_written_by_somebody(tmp_path, cfg):
    """A diary is the clearest case in the file: it is the one artefact whose
    entire value is that a *particular* person made something of a day."""
    prompts = {}
    for which in ("yuri", "rook"):
        vault_dir = _write_card(tmp_path / which, which)
        clock = VirtualClock(start=SIM_START.timestamp())
        vault = MindVault(vault_dir)
        store = FileMemoryStore(vault_dir, FakeEmbedder(), embed_dim=FakeEmbedder.dim)
        # A *finished* day — the clock starts on 2026-07-06, and today's file is
        # never dreamt about (`dream.py`'s rule, shared through `finished_days`).
        day = "2026-07-04"
        ep = vault_dir / "memory" / "episodic"
        ep.mkdir(parents=True, exist_ok=True)
        (ep / f"{day}.md").write_text(
            f"# Journal — {day}\n\n"
            "### 10:00  user: we walked to the water  ⇄  them: mm, it was cold\n",
            encoding="utf-8")

        loader = SoulLoader(vault_dir / "soul", user_name="Sam")
        runner = DreamRunner(
            vault, store, clock, cfg.model_copy(update={"selfie_backend": "off"}),
            consolidator=DreamConsolidator(vault, store, clock,
                                           utility=FakeUtility().complete),
            workspace=Workspace(vault_dir / "workspace"),
            skills=SkillStore(vault_dir / "skills"),
            utility=FakeUtility().complete,
            soul_text=lambda ld=loader, vd=vault_dir: soul_preamble(
                ld.load(), user_md=(vd / "soul" / "USER.md").read_text(),
                user_name="Sam"))
        report = await runner.run(only="diary", token_budget=40000)
        diary = [e for e in report.exchanges if e.job == "diary"]
        assert diary, "the diary job did not run"
        prompts[which] = diary[0].system

    assert prompts["yuri"] != prompts["rook"]
    assert "blushes easily" in prompts["yuri"]
    assert "burn scar" in prompts["rook"]


# --------------------------------------------------------------- the preamble

def test_the_preamble_is_identity_and_nothing_turn_shaped(tmp_path):
    """It carries who she is. It must not carry a turn's retrieval slots — those
    belong to the caller, and `_goal_context` already had every one of them and
    still had no persona."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    soul = SoulLoader(vault / "soul", user_name="Sam").load()
    out = soul_preamble(soul, user_md=(vault / "soul" / "USER.md").read_text(),
                        user_name="Sam")
    assert "VOICE LAW" in out and "PERSONA BACKBONE" in out
    assert "SCENARIO" in out and "WHO YOU ARE TO HER" in out
    for absent in ("THINGS THAT MAY BE RELEVANT", "WHAT YOU'VE READ",
                   "THE HONESTY CONSTRAINT", "WHAT YOU'RE WORKING ON",
                   "EXAMPLE VOICE"):
        assert absent not in out, f"{absent} is turn-shaped and does not belong here"


def test_brief_drops_the_placing_blocks_and_keeps_the_person(tmp_path):
    """The budget ladder, ordered the way §7.2 orders every other drop: the most
    replaceable thing goes first and the voice law never goes at all."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    soul = SoulLoader(vault / "soul", user_name="Sam").load()
    user_md = (vault / "soul" / "USER.md").read_text()
    brief = soul_preamble(soul, user_md=user_md, user_name="Sam", full=False)
    assert "VOICE LAW" in brief and "PERSONA BACKBONE" in brief
    assert "SCENARIO" not in brief and "WHO YOU ARE TO HER" not in brief
    assert len(brief) < len(soul_preamble(soul, user_md=user_md, user_name="Sam"))


# ------------------------------------------------------------------ the switch

async def test_off_restores_the_old_prompt_exactly(tmp_path, cfg):
    """The safety valve has to be real, or the change cannot be measured and
    cannot be backed out of."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    utility = ScriptedUtility("think the steel one, probably")
    rig = make_mind(cfg.model_copy(update={"mind_soul_in_prompts": "off"}),
                    vault, utility=utility)
    rig.mind.goals.add("find out which kettle boils faster", kind="task",
                       priority=0.9, provenance="promise:her-own-words")
    await rig.mind.tick()
    await rig.mind.tick()
    work = [c for c in utility.calls
            if "advancing one of your own goals" in c[0].get("content", "")]
    assert work
    assert work[0][0]["content"].startswith("This is you, alone")
    assert "VOICE LAW" not in work[0][0]["content"]


async def test_a_soul_that_cannot_be_read_costs_the_block_and_not_the_call(
        tmp_path, cfg):
    """§20.2's rule for the shelf, applied to the self.

    Note what this is *not* testing: a vault with no soul at all never boots —
    `create_app` refuses one, and that predates this change. What can happen is a
    soul that reads fine at boot and raises later: a self-edit landing mid-tick,
    a file being rewritten under her, a disk that went away. The guard is for
    that window, and it must lose the persona rather than the step.
    """
    vault = _write_card(tmp_path / "yuri", "yuri")
    utility = ScriptedUtility("think still thinking")
    rig = make_mind(cfg, vault, utility=utility)

    def explode():
        raise KeyError("PERSONA.md: no '## Appearance' section")

    rig.mind.brain.state.soul_loader.load = explode
    assert rig.mind._soul_text() == ""

    rig.mind.goals.add("work out what to do about the kettle", kind="task",
                       priority=0.9, provenance="promise:her-own-words")
    await rig.mind.tick()
    goal = rig.mind.goals.all()[0]
    assert goal.steps >= 1, "a missing persona killed the step"
    work = [c for c in utility.calls
            if "advancing one of your own goals" in c[0].get("content", "")]
    assert work and "VOICE LAW" not in work[0][0]["content"]


async def test_the_preamble_is_cached_not_reread_every_call(tmp_path, cfg):
    """`SoulLoader.load()` re-reads the whole soul directory by design. Right for
    a turn; a night that makes ten calls should not make ten of those."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig = make_mind(cfg, vault)
    loads = {"n": 0}
    real = rig.mind.brain.state.soul_loader.load

    def counted():
        loads["n"] += 1
        return real()

    rig.mind.brain.state.soul_loader.load = counted
    first = rig.mind._soul_text()
    again = rig.mind._soul_text()
    assert first and first == again
    assert loads["n"] == 1, f"loaded {loads['n']} times for two calls"


async def test_an_edited_soul_is_picked_up_without_a_restart(tmp_path, cfg):
    """The cache is keyed on soul/ mtime, so an approved self-edit lands on the
    next call rather than after the TTL — a mind that had to be restarted to
    notice it had changed would make §23's gate pointless."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig = make_mind(cfg, vault)
    before = rig.mind._soul_text()
    assert "blushes easily" in before
    law = vault / "soul" / "CONSTITUTION.md"
    law.write_text(law.read_text().replace("She blushes easily.",
                                           "She has stopped blushing."),
                   encoding="utf-8")
    import os
    os.utime(law, (law.stat().st_atime + 10, law.stat().st_mtime + 10))
    after = rig.mind._soul_text()
    assert "stopped blushing" in after


# ------------------------------------------------ the conversational path holds

async def test_the_turn_prompt_does_not_move(tmp_path, cfg):
    """`build_system` is under the golden transcript. `soul_preamble` was added
    beside it and not factored out of it, precisely so this stays true."""
    from yurios.app.core.assemble import assemble
    vault = _write_card(tmp_path / "yuri", "yuri")
    soul = SoulLoader(vault / "soul", user_name="Sam").load()
    out = assemble(soul, user_md="", summary="", memories=[], lore=[],
                   window=[], user_msg="hey", user_name="Sam")
    assert "THE HONESTY CONSTRAINT" in out.system
    assert "WHOSE THINKING THIS IS" not in out.system, (
        "the mind's preamble leaked into a conversational turn")
