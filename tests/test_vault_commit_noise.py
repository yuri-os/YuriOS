"""The Vault log is a diary, not a heartbeat monitor (SPEC §4.1, §15.1).

Three defects once conspired to make 95% of a real Vault's history a single
changed timestamp, at one commit every five seconds:

  * DREAM broke out of consolidation before processing anything when the oldest
    day alone exceeded the token budget, so the backlog wedged permanently and
    every subsequent tick re-broke against the same file;
  * callers re-saved unchanged state and called `mark_dirty()` regardless, so a
    tick that changed nothing still committed;
  * the tick cursor was persisted *after* the commit, so each tick's bookkeeping
    was swept into the next tick's commit and mislabelled.

These tests pin all three.
"""
from __future__ import annotations

import subprocess

from yurios.mind.dream import DreamConsolidator
from yurios.mind.util import day_of
from yurios.mind.vaultio import MindVault

from .conftest import make_mind, run_mind


def _log(vault) -> list[str]:
    out = subprocess.run(["git", "-C", str(vault), "log", "--format=%s"],
                         capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l]


def _tracked(vault) -> set[str]:
    out = subprocess.run(["git", "-C", str(vault), "ls-files"],
                         capture_output=True, text=True)
    return set(out.stdout.split())


# ---------------------------------------------------------------- write is a diff

def test_rewriting_identical_content_is_a_glance_not_a_change(tmp_path):
    v = MindVault(tmp_path)
    v.write("world/state.json", '{"a": 1}')
    assert v._dirty
    v._dirty = False

    v.write("world/state.json", '{"a": 1}')          # byte-identical
    assert not v._dirty, "a rewrite that changed nothing must not dirty the Vault"

    v.write("world/state.json", '{"a": 2}')
    assert v._dirty


def test_write_json_only_dirties_on_a_real_change(tmp_path):
    v = MindVault(tmp_path)
    v.write_json("state/progress.json", {"days": ["a"]})
    v._dirty = False
    v.write_json("state/progress.json", {"days": ["a"]})
    assert not v._dirty
    v.write_json("state/progress.json", {"days": ["a", "b"]})
    assert v._dirty


def test_empty_write_still_creates_the_file(tmp_path):
    """The no-op guard must not swallow the creation of an empty file."""
    v = MindVault(tmp_path)
    p = v.write("world/empty.md", "")
    assert p.exists() and v._dirty


def test_appending_nothing_is_not_a_change(tmp_path):
    v = MindVault(tmp_path)
    v.append("memory/episodic/d.md", "line\n")
    v._dirty = False
    v.append("memory/episodic/d.md", "")
    assert not v._dirty


# ------------------------------------------------------------------- DREAM wedge

class _Store:
    class index:
        @staticmethod
        def upsert(**kw): pass

    class embedder:
        @staticmethod
        def embed(xs): return [[0.0] for _ in xs]


async def test_an_oversized_day_still_consolidates(cfg, seeded_vault, clock):
    """A day bigger than the whole budget must not wedge the backlog: the
    oldest day always runs, or every DREAM tick forever re-breaks on it."""
    vault = MindVault(seeded_vault)
    dream = DreamConsolidator(vault, _Store(), clock)
    yesterday = day_of(clock.now() - 86400)          # a finished day, not the live one
    huge = "### remember this\n" + ("filler line\n" * 4000)
    (seeded_vault / "memory" / "episodic").mkdir(parents=True, exist_ok=True)
    (seeded_vault / "memory" / "episodic" / f"{yesterday}.md").write_text(
        huge, encoding="utf-8")
    assert len(huge) // 4 > 4000, "the fixture must exceed the default budget"

    assert dream.backlog() == [yesterday]
    report = await dream.consolidate()
    assert report.days_processed == [yesterday]
    assert dream.backlog() == [], "the backlog must drain, not wedge"


async def test_an_empty_dream_backlog_writes_nothing(cfg, seeded_vault, clock):
    vault = MindVault(seeded_vault)
    dream = DreamConsolidator(vault, _Store(), clock)
    vault._dirty = False
    report = await dream.consolidate()
    assert report.nothing_to_do
    assert not vault._dirty, "a night with nothing to do must not commit"


# ------------------------------------------------------------ the log stays quiet

async def test_idle_ticks_do_not_commit(cfg, seeded_vault):
    """Hours of an empty room must not add a single commit to the diary."""
    rig = make_mind(cfg, seeded_vault)
    before = _log(seeded_vault)
    await run_mind(rig, hours=6)
    assert _log(seeded_vault) == before, (
        "an uneventful stretch committed to the Vault")


async def test_scheduler_bookkeeping_is_never_versioned(cfg, seeded_vault):
    """engine.json and activity.json are rewritten every tick; versioning them
    buries the diary under one commit per heartbeat.

    The check needs a tick loop that has actually run (the files don't exist
    until then) *and* a real commit (the Vault commits with `git add -A`, so
    only a commit can sweep them in). An exchange supplies the second.
    """
    rig = make_mind(cfg, seeded_vault)
    before = _log(seeded_vault)
    rig.say("hello")
    await run_mind(rig, hours=2)

    assert (seeded_vault / "state" / "engine.json").exists(), "fixture ran no ticks"
    assert _log(seeded_vault) != before, "fixture produced no commit to sweep them in"

    tracked = _tracked(seeded_vault)
    assert "state/engine.json" not in tracked
    assert "state/activity.json" not in tracked
    # ...but the durable half of state/ is still versioned
    assert "state/sessions.json" in tracked
