"""The Vault log is a diary, not a heartbeat monitor (B1 §4.1, SPEC §15.1).

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
    oldest day always runs, or every DREAM tick forever re-breaks on it.

    The budget is pinned here rather than taken from the default, so that
    raising MIND_DREAM_TICK_TOKENS can never quietly stop this from being a
    test of the oversized-day path.
    """
    budget = 1000
    vault = MindVault(seeded_vault)
    dream = DreamConsolidator(vault, _Store(), clock)
    yesterday = day_of(clock.now() - 86400)          # a finished day, not the live one
    huge = "### remember this\n" + ("filler line\n" * 4000)
    (seeded_vault / "memory" / "episodic").mkdir(parents=True, exist_ok=True)
    (seeded_vault / "memory" / "episodic" / f"{yesterday}.md").write_text(
        huge, encoding="utf-8")
    assert len(huge) // 4 > budget, "the fixture must exceed the whole budget"

    assert dream.backlog() == [yesterday]
    report = await dream.consolidate(token_budget=budget)
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


async def test_scheduler_bookkeeping_is_never_versioned(cfg, seeded_vault,
                                                        open_vault_window):
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


# ---- the day's window (SPEC §6.5) -------------------------------------------

def _commit(vault, message):
    from yurios.app import vaultgit
    return vaultgit.commit(vault, message)


def test_the_vault_takes_one_snapshot_a_day_not_one_per_turn(seeded_vault):
    """The diary is a diary. A day of ordinary conversation used to bury the two
    entries that mattered under three hundred that did not, so the writes go
    down immediately and the *history entry* waits out a day."""
    before = _log(seeded_vault)

    (seeded_vault / "memory" / "semantic" / "facts.md").write_text("the kettle\n")
    assert _commit(seeded_vault, "turn: one") is not None
    (seeded_vault / "goals.md").write_text("- water the plant\n")
    _commit(seeded_vault, "turn: two")

    assert _log(seeded_vault) == before, "a turn inside the window wrote history"
    # …and nothing was lost doing it: both writes are on disk, now
    assert "the kettle" in (seeded_vault / "memory" / "semantic" / "facts.md").read_text()
    assert "water the plant" in (seeded_vault / "goals.md").read_text()


def test_the_day_after_sweeps_up_everything_since(seeded_vault, monkeypatch):
    """One commit, holding every change the window held back — `git add -A` on
    the far side does not care which write asked for it."""
    from yurios.app import vaultgit

    (seeded_vault / "memory" / "semantic" / "facts.md").write_text("the kettle\n")
    _commit(seeded_vault, "turn: one")
    (seeded_vault / "goals.md").write_text("- water the plant\n")
    _commit(seeded_vault, "turn: two")
    before = _log(seeded_vault)

    monkeypatch.setattr(vaultgit, "COMMIT_INTERVAL_S", 0)     # …a day later
    _commit(seeded_vault, "turn: three")

    after = _log(seeded_vault)
    assert len(after) == len(before) + 1, "one commit, not one per held-back write"
    assert after[0] == "turn: three"          # named for what tripped the window
    changed = subprocess.run(
        ["git", "-C", str(seeded_vault), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True).stdout.split()
    assert "goals.md" in changed and "memory/semantic/facts.md" in changed


def test_a_quiet_day_still_commits_nothing(seeded_vault, open_vault_window):
    """The window is a ceiling on commits, never a reason to make one: an
    uneventful turn has nothing staged and leaves no entry (§4.4)."""
    before = _log(seeded_vault)
    _commit(seeded_vault, "turn: nothing happened")
    assert _log(seeded_vault) == before


def test_a_vault_with_no_commits_yet_does_not_wait_a_day(tmp_path):
    """A fresh seed or a freshly imported card has no window to be inside — and
    that first entry is what starts the clock. Waiting a day here would leave a
    new character's whole SOUL untracked until tomorrow."""
    from yurios.app import vaultgit

    vault = tmp_path / "fresh"
    vault.mkdir()
    vaultgit.ensure_repo(vault)
    (vault / "USER.md").write_text("her first day\n")

    assert _log(vault) == []
    assert vaultgit.commit(vault, "seed: initial vault") is not None
    assert _log(vault) == ["seed: initial vault"]


def test_the_window_belongs_to_the_vault_not_to_the_process(seeded_vault):
    """Measured against the Vault's own HEAD, so it survives a restart, is
    shared by every writer of this Vault, and cannot be reset by bouncing the
    daemon — which an in-process timer would be, on both counts."""
    from yurios.app import vaultgit

    sha, when = vaultgit.head_at(seeded_vault)
    assert sha is not None and when > 0

    (seeded_vault / "goals.md").write_text("- something\n")
    # a "restart" is just another call: nothing in this module holds state
    assert vaultgit.commit(seeded_vault, "turn: after a restart") == sha
    assert vaultgit.commit(seeded_vault, "turn: and another") == sha
