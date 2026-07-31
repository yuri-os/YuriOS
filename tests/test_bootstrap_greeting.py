"""BOOTSTRAP.md, consumed once (SPEC §5.4) — on the path she is actually greeted
through.

The cold open used to live only in Build #1's `GET /api/greeting`, a route the
world server never mounts: every real greeting came from `BrainAdapter.
stream_greeting`, which asked the model for a continuity opener and never looked
at the bootstrap. So the first-ever meeting never happened, the file was never
retired, and "has she met you yet?" answered *no* forever. These pin the fork
where it now lives, one level down, where both servers and every channel share
it.
"""
from __future__ import annotations

from tests.conftest import CannedChat, FakeEmbedder, FakeUtility, collect
from yurios.world.brain import ToolBrain
from yurios.world.tools.guard import Guard
from yurios.world.tools.timers import TimerBoard


def make_brain(cfg, vault, chat, clock, controller):
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": FakeEmbedder.dim,
        "corpus_dir": vault.parent / "corpus",
        "trace_dir": vault.parent / "traces",
        "tool_log_dir": vault.parent / "tool-logs"})
    return ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=controller, chat_model=chat,
        utility_model=FakeUtility(), embedder=FakeEmbedder())


async def test_the_first_greeting_is_the_authored_cold_open(
        cfg, seeded_vault, clock, controller):
    """No journal → she has never met you → she speaks the card's first message,
    verbatim, with no model call anywhere near it."""
    chat = CannedChat()
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    session = brain.resolve_session(None)

    cold = brain.cold_open()
    spoken = "".join(await collect(brain.stream_greeting(session)))

    assert cold.strip() == brain.state.soul_loader.load().bootstrap.strip()
    assert spoken.strip() == cold.strip()
    assert chat.calls == [], "the cold open is authored SOUL, not a completion"
    # the one opener that joins the window: it is the scene her first reply has
    # to answer from (every other greeting stays out of it, §7)
    window = brain.state.sessions.window(session, 4)
    assert [m["role"] for m in window] == ["assistant"]
    assert window[0]["content"].strip() == cold.strip()
    # still present: file-presence is the flag, and she has not met you until
    # the journal says so — a headset put on twice before the first turn opens
    # the same way both times.
    assert (seeded_vault / "soul" / "BOOTSTRAP.md").is_file()


async def test_the_first_persisted_turn_retires_the_bootstrap(
        cfg, seeded_vault, clock, controller):
    """The first completed exchange creates the journal and consumes the bootstrap
    in that same post-turn transaction, without waiting for another greeting."""
    chat = CannedChat("[tender] I'm glad you stayed.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    session = brain.resolve_session(None)

    await collect(brain.stream_greeting(session))
    reply = "".join(await collect(brain.stream_reply(session, "I'm Sam.")))
    await brain.persist(session, "I'm Sam.", reply)

    assert any((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert not (seeded_vault / "soul" / "BOOTSTRAP.md").exists()
    done = seeded_vault / "soul" / "onboarded" / "BOOTSTRAP.done.md"
    assert done.is_file()
    assert brain.cold_open() is None


async def test_the_bootstrap_retires_once_the_journal_shows_a_meeting(
        cfg, seeded_vault, clock, controller):
    """One episodic entry is the whole exit condition: from here the greeting is
    memory-grounded and the bootstrap is git-mv'd out of the way, once."""
    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text(
        "# Journal — 2026-07-01\n\n### 09:00  you: hello  ⇄  her: hello\n")
    chat = CannedChat("[tender] You're back.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)

    spoken = "".join(await collect(brain.stream_greeting("s1")))

    assert spoken.strip() == "[tender] You're back."
    assert len(chat.calls) == 1, "she greets from memory now, through the model"
    assert not (seeded_vault / "soul" / "BOOTSTRAP.md").exists()
    done = seeded_vault / "soul" / "onboarded" / "BOOTSTRAP.done.md"
    assert done.is_file(), "retired, not deleted — git log keeps the script"

    # and it stays retired: the second greeting finds no bootstrap to consume
    # and no `git mv` left to fail on.
    assert "".join(await collect(brain.stream_greeting("s1"))).strip() \
        == "[tender] You're back."
    assert len(chat.calls) == 2


async def test_a_failed_retirement_does_not_swallow_the_greeting(
        cfg, seeded_vault, clock, controller, monkeypatch):
    """Git can fail (no repo, a lock, a read-only mount). She still greets — the
    retirement simply gets retried next time."""
    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text("### 09:00\n")
    chat = CannedChat("[neutral] Hey.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)

    def boom(*a, **k):
        raise RuntimeError("vault mv failed: no such file")
    monkeypatch.setattr("yurios.desktop.brain.vaultgit.mv", boom)

    assert "".join(await collect(brain.stream_greeting("s1"))).strip() == "[neutral] Hey."
    assert (seeded_vault / "soul" / "BOOTSTRAP.md").is_file()


async def test_a_restored_bootstrap_can_retire_a_second_time(
        cfg, seeded_vault, clock, controller):
    """Restoring the bootstrap to re-run onboarding is a supported move (§5.4),
    so the retirement after it must not fail on the first one's leftovers —
    `git mv` refuses an occupied destination, and the greeting would then try
    and fail on every arrival, for good."""
    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text("### 09:00\n")
    chat = CannedChat("[neutral] Hey.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    done = seeded_vault / "soul" / "onboarded" / "BOOTSTRAP.done.md"

    await collect(brain.stream_greeting("s1"))
    assert done.is_file()

    # she is restored from the card (or from git) to be met again
    (seeded_vault / "soul" / "BOOTSTRAP.md").write_text(
        "---\nsoul: bootstrap\n---\n\n# Bootstrap\n\n## Cold open\n\nHello again.\n")

    await collect(brain.stream_greeting("s2"))

    assert not (seeded_vault / "soul" / "BOOTSTRAP.md").exists()
    assert "Hello again." in done.read_text(encoding="utf-8")
