"""The partner model is a living file, not an append-only log (SPEC §6.3).

The live vault taught this: USER.md grew a preference soup, the phase line
stayed on the seeded 'early' for a month after she knew his name, a skill was
wanted and then not and both bullets survived, and 'Yandere-lite' never reached
PERSONA.md. These pin the fixes, not a live model's obedience.
"""
from __future__ import annotations

import json
import subprocess

from yurios.app.core.soul import SoulLoader
from yurios.characters.soulfiles import parse_md_text
from yurios.app.memory import partner
from yurios.app.memory.partner import Op
from yurios.app.memory.store import FileMemoryStore, Record
from yurios.mind.housekeeping import propose_learned_persona
from yurios.mind.selfedit import SelfEdit
from yurios.mind.vaultio import MindVault
from yurios.kernel.clock import VirtualClock

from .conftest import ROOT, FakeEmbedder, SIM_START

SOUL_SRC = ROOT / "soul-src"

# The live-vault failure, shortened: append-only soup, frozen phase, a
# contradiction, a character-direction that never left Stable.
SOUP = """\
---
soul: user
runtime_only: true
---

## Who {{user}} seems to be

## What helps, and what doesn't

## Current relationship phase

early — careful courtesy; the formality has not yet thawed.

## Stable

- Prefers to be understood through observation rather than just direct description.
- Takes tea strong with no sugar.
- Name is Grant (implied by research/about_grant.md).
- Works late.
- Hates being asked if they are okay.
- Does not want to keep the 'calming-grant' skill.
- Wants Yuri to have a specific skill called 'calming-grant'.
- Prefers direct action and execution over mere verbal promises.
- Describes their desired level of devotion/personality as 'Yandere-lite'.
- Wants Yuri to be bold about reaching out — no five-day silences.

## Ongoing

- Has been busy with work and upgrades to YuriOS.
"""


def ops_json(*ops):
    return json.dumps({"ops": list(ops)})


class ScriptedUtility:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, **params):
        self.calls.append(messages)
        if not self.replies:
            return '{"ops": []}'
        return self.replies.pop(0)


def _store(tmp_path, utility=None) -> FileMemoryStore:
    (tmp_path / "memory" / "semantic").mkdir(parents=True, exist_ok=True)
    (tmp_path / "soul").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "soul" / "USER.md").write_text(
        (SOUL_SRC / "USER.md").read_text(encoding="utf-8")
        if (SOUL_SRC / "USER.md").is_file()
        else "---\nsoul: user\nruntime_only: true\nphase: early\n---\n\n"
             "## Who {{user}} seems to be\n\n"
             "## What helps, and what doesn't\n\n"
             "## Current relationship phase\n\n"
             "early — careful courtesy; the formality has not yet thawed.\n",
        encoding="utf-8")
    return FileMemoryStore(tmp_path, FakeEmbedder(), utility,
                           char_name="yuri", user_name="you",
                           embed_dim=FakeEmbedder.dim)


# --- apply_ops: merge, contradict, phase --------------------------------------

def test_apply_ops_merges_instead_of_duplicating():
    md = "## Stable\n\n- prefers mornings quiet\n"
    md = partner.apply_ops(md, [Op("Stable", "prefers quiet mornings", "add", 0.9)])
    assert md.count("mornings") == 1
    md = partner.apply_ops(md, [Op("Stable", "prefers loud mornings now", "update", 0.9)])
    assert "loud mornings" in md and "quiet" not in md
    md = partner.apply_ops(md, [Op("Stable", "prefers loud mornings now", "remove", 0.9)])
    assert "mornings" not in md


def test_a_contradicting_add_replaces_the_old_bullet():
    """The calming-grant pair: wanted, then not, both survived as adds."""
    md = ("## Stable\n\n"
          "- Does not want to keep the 'calming-grant' skill.\n")
    md = partner.apply_ops(md, [Op(
        "Stable",
        "Wants Yuri to have a specific skill called 'calming-grant'",
        "add", 0.9)])
    assert md.count("calming-grant") == 1
    assert "Does not want" not in md
    assert "Wants Yuri" in md


def test_extractor_provenance_is_scrubbed():
    md = partner.apply_ops(
        "## Stable\n",
        [Op("Stable", "Name is Grant (implied by research/about_grant.md).",
            "add", 0.9)])
    assert "Name is Grant" in md
    assert "implied by" not in md
    assert "about_grant" not in md


def test_knowing_their_name_thaws_the_phase():
    md = partner.apply_ops(
        (SOUL_SRC / "USER.md").read_text(encoding="utf-8"),
        [Op("Stable", "Name is Grant.", "add", 0.95)])
    assert "Name is Grant" in md
    assert "mid —" in md
    assert "early —" not in md
    assert "Grant." in md  # Who they seem to be is no longer an empty slot


def test_an_unrelated_fact_does_not_evict_a_short_one():
    """`_collapse` runs over the whole file on every turn. It used to score
    "Likes blue." against "Likes dogs." at 0.5 — a short bullet is a subset of
    any longer one sharing a word — and delete the first. Merging is a nicety;
    losing a fact the user told her is the thing this file exists to prevent."""
    md = ("## Stable\n\n- Likes blue.\n- Allergic to peanuts.\n")
    md = partner.apply_ops(md, [Op("Stable", "Likes dogs.", "add", 0.9)])
    for kept in ("Likes blue.", "Allergic to peanuts.", "Likes dogs."):
        assert kept in md, f"{kept!r} was eaten:\n{md}"
    md = partner.apply_ops(md, [Op("Stable", "Allergic to cats.", "add", 0.9)])
    assert "Allergic to peanuts." in md and "Allergic to cats." in md


def test_a_bullet_marker_in_the_op_text_is_not_rendered_twice():
    """GLM returns "- Likes dogs." for a bullet section often enough that a live
    USER.md grew "- - Likes dogs." The renderer owns the marker."""
    md = partner.apply_ops("## Stable\n", [Op("Stable", "- Likes dogs.", "add", 0.9)])
    assert "- Likes dogs." in md
    assert "- - " not in md


def test_how_they_asked_her_to_be_is_the_models_call_not_a_keyword_list():
    """`about_her` comes from the extractor that already read the turn. There is
    no substring list: a user who says "be more playful" opens the same door as
    one who says "Yandere-lite"."""
    for text in ("Describes their desired level of devotion as 'Yandere-lite'.",
                 "Wants her to be more playful and flirty, less formal.",
                 "Wants her to text first sometimes rather than waiting."):
        md = partner.apply_ops(
            "## Stable\n",
            [Op("What helps, and what doesn't", text, "add", 0.95, about_her=True)])
        helps = md.split("## What helps, and what doesn't", 1)[1].split("\n## ")[0]
        assert text in helps, md


def test_dream_refiles_a_stable_bullet_the_model_says_belongs_elsewhere():
    """Per-turn trusts the extractor's section. The filing pass is DREAM's, and
    it is the model's answer — `classified` — not a cue list."""
    md = ("## Stable\n\n"
          "- Takes tea strong with no sugar.\n"
          "- Wants her to be more playful and flirty, less formal.\n"
          "- Busy with a house move this month.\n")
    new_md, delta = partner.compact_user_md(md, days_together=9, classified={
        "Takes tea strong with no sugar.": "stable",
        "Wants her to be more playful and flirty, less formal.": "learned",
        "Busy with a house move this month.": "ongoing",
    })
    stable = new_md.split("## Stable", 1)[1].split("\n## ")[0]
    helps = new_md.split("## What helps, and what doesn't", 1)[1].split("\n## ")[0]
    ongoing = new_md.split("## Ongoing", 1)[1].split("\n## ")[0]
    assert "tea" in stable and "playful" not in stable and "house move" not in stable
    assert "playful" in helps
    assert "house move" in ongoing
    assert delta is not None and any("playful" in line for line in delta.lines)


def test_no_classification_means_no_persona_delta():
    """Silence beats a guess: nothing has read the bullets, so nothing claims to
    know which of them are directions about her."""
    md = ("## What helps, and what doesn't\n\n"
          "- Wants her to be more playful and flirty.\n")
    _, delta = partner.compact_user_md(md, days_together=9)
    assert delta is None


# --- compact: the live soup ---------------------------------------------------

# How the model files the live soup — stated here, so these tests exercise the
# merge machinery rather than a keyword list's opinion of it. `test_live_filing`
# in the bench checks the real model reaches roughly this answer.
SOUP_LABELS = {
    "Prefers to be understood through observation rather than just direct description.": "helps",
    "Takes tea strong with no sugar.": "stable",
    "Name is Grant.": "stable",
    "Works late.": "ongoing",
    "Hates being asked if they are okay.": "helps",
    "Does not want to keep the 'calming-grant' skill.": "helps",
    "Wants Yuri to have a specific skill called 'calming-grant'.": "helps",
    "Prefers direct action and execution over mere verbal promises.": "helps",
    "Describes their desired level of devotion/personality as 'Yandere-lite'.": "learned",
    "Wants Yuri to be bold about reaching out — no five-day silences.": "learned",
    "Has been busy with work and upgrades to YuriOS.": "ongoing",
}


def test_compact_collapses_the_live_vault_soup():
    new_md, delta = partner.compact_user_md(SOUP, days_together=30,
                                            classified=SOUP_LABELS)
    assert new_md.count("calming-grant") == 1
    assert "implied by" not in new_md
    assert "mid —" in new_md
    assert "early —" not in new_md
    who = new_md.split("## Who {{user}} seems to be", 1)[1].split("##")[0]
    assert "Grant" in who
    assert "_(" not in who
    assert delta is not None
    assert any("Yandere-lite" in line for line in delta.lines)
    assert any("reach out" in line.lower() or "five-day" in line.lower()
               for line in delta.lines)
    assert delta.phase == "mid"


def test_compact_of_the_seed_is_a_noop():
    """A quiet DREAM on a fresh vault must not dirty USER.md just to add chrome."""
    seed = (SOUL_SRC / "USER.md").read_text(encoding="utf-8")
    new_md, delta = partner.compact_user_md(seed, days_together=0)
    assert delta is None
    assert new_md == seed


# --- remember() wires merge + delta ------------------------------------------

async def test_durable_fact_lands_and_the_utility_saw_current_user_md(tmp_path):
    utility = ScriptedUtility(ops_json(
        {"section": "Don't forget", "text": "anniversary: 14 Feb",
         "op": "add", "confidence": 0.95}))
    store = _store(tmp_path, utility)
    await store.remember(Record(session_id="s", turn_index=0,
                                user_msg="our anniversary is the 14th of Feb — remember it",
                                reply="the fourteenth. kept, always."))
    user_md = store.read_user_md()
    assert "## Don't forget" in user_md
    assert "anniversary: 14 Feb" in user_md
    prompt_text = json.dumps(utility.calls[0])
    assert "Current USER.md" in prompt_text


async def test_a_character_direction_writes_a_persona_delta(tmp_path):
    utility = ScriptedUtility(ops_json(
        {"section": "What helps, and what doesn't",
         "text": "Describes their desired level of devotion/personality as 'Yandere-lite'.",
         "op": "add", "confidence": 0.95, "about_her": True}))
    store = _store(tmp_path, utility)
    await store.remember(Record(session_id="s", turn_index=0,
                                user_msg="I want you Yandere-lite",
                                reply="then that's how I'll be."))
    delta = partner.read_persona_delta(tmp_path)
    assert delta is not None
    assert any("Yandere-lite" in line for line in delta["lines"])
    assert delta["queued"] is False


# --- PERSONA.md coupling ------------------------------------------------------

def test_loader_puts_learned_in_the_backbone_only_once_it_has_content(tmp_path):
    """Empty Learned is chrome and stays out of the prompt; filled Learned is
    identity, so it rides the backbone that never drops."""
    import shutil
    soul = tmp_path / "soul"
    shutil.copytree(SOUL_SRC, soul)
    loaded = SoulLoader(soul, user_name="Grant").load()
    assert "Yandere-lite" not in loaded.backbone
    persona = (soul / "PERSONA.md").read_text(encoding="utf-8")
    grown = partner.apply_learned_to_persona(
        persona, partner.PersonaDelta(
            lines=["Yandere-lite: bold about reaching out."],
            phase="mid", reason="asked"))
    (soul / "PERSONA.md").write_text(grown, encoding="utf-8")
    loaded = SoulLoader(soul, user_name="Grant").load()
    assert "Yandere-lite" in loaded.backbone


def test_learned_section_is_a_slice_not_a_new_character():
    persona = (SOUL_SRC / "PERSONA.md").read_text(encoding="utf-8")
    delta = partner.PersonaDelta(
        lines=["Yandere-lite: bold about reaching out, no five-day silences."],
        phase="mid",
        reason="they asked for a Yandere-lite register",
    )
    grown = partner.apply_learned_to_persona(persona, delta)
    assert "## Appearance" in grown and "## Manner" in grown
    assert "## Inner life" in grown and "## Growth" in grown
    assert "## Learned" in grown
    assert "Yandere-lite" in grown
    assert "relationship_phase: mid" in grown
    assert partner.learned_already_in_persona(grown, delta)
    assert not partner.learned_already_in_persona(persona, delta)


def test_a_persona_delta_is_queued_not_silently_applied(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / "soul").mkdir(parents=True)
    (vault_dir / "state").mkdir(parents=True)
    persona = (SOUL_SRC / "PERSONA.md").read_text(encoding="utf-8")
    (vault_dir / "soul" / "PERSONA.md").write_text(persona, encoding="utf-8")
    (vault_dir / "soul" / "soul.yaml").write_text(
        (SOUL_SRC / "soul.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "-C", str(vault_dir), "init", "-q"], check=True)
    delta = partner.PersonaDelta(
        lines=["Yandere-lite: bold about reaching out."],
        phase="mid",
        reason="they asked for a Yandere-lite register",
    )
    partner.write_persona_delta(vault_dir, delta)
    vault = MindVault(vault_dir)
    clock = VirtualClock(start=SIM_START.timestamp())
    selfedit = SelfEdit(vault, clock)

    class Loop:
        def __init__(self):
            self.cfg = type("C", (), {"vault_dir": vault_dir})()
            self.selfedit = selfedit
            self.vault = vault

    notes = propose_learned_persona(Loop())
    assert notes and "queued" in notes[0]
    assert selfedit.pending()
    assert "Yandere-lite" in selfedit.pending()[0]["content"]
    # the reason rides through to the panel you approve it in
    assert delta.reason == selfedit.pending()[0]["reason"]
    assert vault.read("soul/PERSONA.md") == persona  # untouched until you approve
    # a second pass must not nag
    assert propose_learned_persona(Loop()) == []


async def test_dreams_filing_pass_is_cached_on_the_bullets_it_read(tmp_path):
    """Most nights the bullets are yesterday's. Re-asking the model where they
    go buys the same answer for the price of a call, every night, forever."""
    labels = json.dumps({"labels": {"1": "stable"}})
    utility = ScriptedUtility(labels, labels)
    store = _store(tmp_path, utility)
    (tmp_path / "soul" / "USER.md").write_text(
        "---\nsoul: user\n---\n\n## Stable\n\n- Takes tea strong with no sugar.\n",
        encoding="utf-8")

    await store.evolve_partner(days_together=9)
    assert len(utility.calls) == 1
    await store.evolve_partner(days_together=9)
    assert len(utility.calls) == 1, "a quiet night must not re-ask"

    # a new bullet is the only thing worth a call, and only it is sent
    store.user_md_path.write_text(
        store.read_user_md() + "- Likes dogs.\n", encoding="utf-8")
    await store.evolve_partner(days_together=9)
    assert len(utility.calls) == 2
    asked = json.dumps(utility.calls[-1])
    assert "Likes dogs" in asked and "strong with no sugar" not in asked


# --- the file must survive being written ------------------------------------

def test_frontmatter_survives_a_round_trip():
    """USER.md is re-parsed on every turn, and `remember()` swallows the
    exception — so an unquoted value that breaks YAML does not crash anything,
    it just stops the partner model updating, silently, forever."""
    hostile = {
        "soul": "user", "runtime_only": True,
        "note": "a: colon inside", "quoted": 'she said "hi"',
        "hash": "trailing # hash", "empty": "", "padded": "  spaces  ",
        "numberish": "2.0", "booleanish": "yes", "dashed": "- leading dash",
        "brace": "{not a map}", "nl": "two\nlines",
    }
    md = partner.render_user_md(hostile, {"Stable": "- Name is Grant."})
    front, sections = partner.parse_user_md(md)
    for key, value in hostile.items():
        assert front[key] == value, f"{key}: {front[key]!r} != {value!r}"
    assert "Name is Grant." in sections["Stable"]


def test_persona_frontmatter_survives_a_round_trip():
    """The real PERSONA.md carries a `personality:` value full of commas. A
    gated edit rewrites that frontmatter, and the loader parses it next wake."""
    persona = (SOUL_SRC / "PERSONA.md").read_text(encoding="utf-8")
    original, _ = parse_md_text(persona)
    grown = partner.apply_learned_to_persona(
        persona, partner.PersonaDelta(lines=["Be warmer."], phase="mid", reason="r"))
    front, _ = parse_md_text(grown)           # must not raise
    assert front["relationship_phase"] == "mid"
    for key, value in original.items():
        if key != "relationship_phase":
            assert front[key] == value, f"{key} changed: {front[key]!r}"


# --- the matcher is not one character's ------------------------------------

def test_the_companions_name_is_discounted_whoever_she_is():
    """`_STOP` used to end "...you your yuri user". On a host holding every
    character on the node, that made Yuri's name invisible to the matcher and
    left every other character's name as a content word they were compared on."""
    a, b = "Wants Yuri to be warmer.", "Wants Yuri to reach out first."
    c, d = "Wants Noor to be warmer.", "Wants Noor to reach out first."
    # whoever she is, the two requests are as distinct as each other
    assert partner.topic_key(a, ("yuri",)) == partner.topic_key(c, ("noor",))
    assert partner.topic_key(b, ("yuri",)) == partner.topic_key(d, ("noor",))
    assert (partner.same_slot(a, b, names=("yuri",))
            is partner.same_slot(c, d, names=("noor",)))
    # and no name is special-cased in the module any more
    assert "yuri" not in partner._STOP


def test_the_importer_writes_the_headings_the_partner_model_reads(tmp_path):
    """The one thing a soul-src-seeded test cannot see.

    `USER.md` is ours — `CharacterImporter` writes it on every card import — so
    its headings and `partner.py`'s vocabulary are one fact in two files. They
    drifted ("What helps, and what does **not**" against "...doesn't") and every
    imported character grew a second What-helps heading beside the template's,
    fed the new one, and carried the old one's placeholder into every prompt.
    """
    from yurios.characters.importer import _write_partner_model
    soul = tmp_path / "soul"
    soul.mkdir()
    _write_partner_model(soul)
    written = (soul / "USER.md").read_text(encoding="utf-8")

    _, sections = partner.parse_user_md(written)
    for heading in sections:
        assert partner.canon_section(heading) == heading, (
            f"the importer writes '## {heading}', which the partner model does "
            f"not recognise as one of its own sections")
    # and a turn against it must not invent a second copy of anything
    after = partner.apply_ops(written, [
        Op("Stable", "Name is Kai.", "add", 0.95),
        Op(partner.HELPS, "Wants her blunt.", "add", 0.9, about_her=True)])
    for heading in partner.CANONICAL_ORDER:
        assert after.count(f"## {heading}") <= 1, f"'## {heading}' twice:\n{after}"
    assert "_(" not in after
    assert written.count("## ") == len(sections)   # no heading lost to a preamble


def test_the_approval_reason_says_what_you_asked_for():
    """It is the sentence beside an identity edit you are being asked to
    approve, so it has to name what changed. It used to be a constant —
    identical for every proposal, in the third person about the reader, and
    half of it ("USER.md is not PERSONA.md") was plumbing."""
    reason = partner.persona_reason([
        "Drop formal address like 'my lord'; be warmer, less servant-like.",
        "Come find her on your own initiative — don't wait to be summoned.",
    ])
    assert "Drop formal address like 'my lord'" in reason
    assert "Come find her on your own initiative" in reason
    assert reason.startswith("You asked her")          # second person
    assert "USER.md" not in reason and "PERSONA.md" not in reason
    assert "they asked" not in reason.lower()
    # the clause after the first sentence is what saying yes costs
    assert "who she is" in reason

    # long lists are summarised, not dumped
    many = partner.persona_reason([f"Direction number {i} about her." for i in range(9)])
    assert "6 more" in many
    assert len(many) < 320

    # a merge rewrites the reason, because the reason describes the lines
    assert partner.persona_reason(["Be warmer."]) != partner.persona_reason(
        ["Be warmer.", "Start things yourself."])


async def test_tightening_the_filing_rules_invalidates_the_cached_labels(tmp_path):
    """A cached label is only as good as the prompt that produced it. Without
    the stamp, every bullet already in the file keeps the old wording's verdict
    forever while new bullets get the new one — two rules in one file."""
    store = _store(tmp_path, ScriptedUtility(json.dumps({"labels": {"1": "stable"}})))
    store.user_md_path.write_text(
        "---\nsoul: user\n---\n\n## Stable\n\n- Takes tea strong.\n", encoding="utf-8")
    await store.evolve_partner(days_together=9)
    assert json.loads(store.filing_path.read_text())["labels"] == {"Takes tea strong.": "stable"}

    # same rules → no second call
    store.utility = ScriptedUtility(json.dumps({"labels": {"1": "learned"}}))
    await store.evolve_partner(days_together=9)
    assert store.utility.calls == []

    # rules changed under it → the label is re-asked, not inherited
    stamped = json.loads(store.filing_path.read_text())
    stamped["rules"] = "stale-prompt"
    store.filing_path.write_text(json.dumps(stamped), encoding="utf-8")
    store.utility = ScriptedUtility(json.dumps({"labels": {"1": "helps"}}))
    await store.evolve_partner(days_together=9)
    assert store.utility.calls, "a changed prompt must be a cold cache"
    assert json.loads(store.filing_path.read_text())["labels"] == {"Takes tea strong.": "helps"}
