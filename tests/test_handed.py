"""A document you hand her (SPEC §34.6): read, and hers to decide about.

Not the shelf: nothing here is indexed. It lands in her desk's inbox, SENSE
notices it with no model, and a sitting — when `MIND_INBOX_WAKE` says — reads it
and decides what it means for what she is doing. These pin both timings, the
fences around the sitting, and that the goal she files carries the document
into every step it gets.
"""
from __future__ import annotations

from yurios.mind import goalwork, handed
from yurios.mind.goals import HANDED_GOAL
from yurios.mind.policy import DORMANT

from .conftest import ScriptedUtility, make_mind

DOC = "# Q3 numbers\n\nRevenue rose 12% on the back of the new tea line.\n"
FILE = ('think This changes the quarterly report — the tea line is the story.\n'
        'use create_goal {"text": "work the Q3 numbers in inbox/q3.md into my '
        'quarterly report", "kind": "task"}')


def hand(rig, name: str = "q3.md", text: str = DOC) -> str:
    """What the upload route does: the file on the desk, then the signal."""
    path = f"inbox/{name}"
    rig.mind.workspace.write(path, text)
    rig.mind.bus.post("handed", {"path": path, "name": name}, source="user")
    return path


def asleep(rig) -> None:
    """3am, nobody here, nothing said for hours."""
    rig.mind.activity.state = DORMANT
    rig.mind.activity.last_user_msg = rig.clock.now() - 6 * 3600
    rig.mind._last_turn_end = rig.clock.now() - 6 * 3600


def journal(rig) -> str:
    from yurios.mind.util import day_of
    entries = rig.mind.journal.day_entries(day_of(rig.clock.now()))
    return "\n".join(str(e.get("text", e)) for e in entries)


def sat_down(utility: ScriptedUtility) -> list[list[dict]]:
    return [call for call in utility.calls
            if "handed you a document" in call[0].get("content", "")]


async def test_a_handed_document_wakes_her_and_she_decides(cfg, seeded_vault):
    utility = ScriptedUtility(handed=(FILE, "think that's it"))
    rig = make_mind(cfg, seeded_vault, utility=utility)
    asleep(rig)
    path = hand(rig)

    trace = await rig.mind.tick()

    assert trace["decided"]["intention"] == "handed"
    goal = rig.mind.goals.get(trace["acted"]["goal"])
    assert goal is not None and goal.provenance == HANDED_GOAL + path
    assert goal.meta["handed"] == path
    assert "tea line is the story" in goal.meta["rationale"]
    assert handed.waiting(rig.mind) == []
    (record,) = rig.mind.handed            # kept, for the inner-life panel
    assert record["state"] == "read" and record["read_at"]
    assert record["goal"] == goal.id
    assert record["outcome"].startswith("decided to “work the Q3 numbers")
    (call, *_rest) = sat_down(utility)
    assert "Revenue rose 12%" in call[1]["content"], "she was shown the document"
    said = journal(rig)
    assert "you handed me q3.md" in said
    assert "decided to" in said


async def test_quietly_she_notices_now_and_decides_when_you_are_back(
        cfg, seeded_vault):
    utility = ScriptedUtility(handed=(FILE, "think that's it"))
    rig = make_mind(cfg.model_copy(update={"mind_inbox_wake": False}),
                    seeded_vault, utility=utility)
    asleep(rig)
    path = hand(rig)

    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] != "handed"
    assert [h["path"] for h in handed.waiting(rig.mind)] == [path]
    assert "when you're back" in journal(rig), "noticed at once, with no model"
    assert sat_down(utility) == []

    rig.say("morning!")                    # you're back
    intentions = []
    for _ in range(4):
        rig.clock.advance(max(rig.mind.cadence(), 1.0))
        intentions.append((await rig.mind.tick())["decided"]["intention"])
    assert "handed" in intentions
    assert handed.waiting(rig.mind) == [] and sat_down(utility)


async def test_never_in_the_middle_of_a_turn(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(handed=("think noted",)))
    asleep(rig)
    hand(rig)
    rig.mind.turn_started()
    assert (await rig.mind.tick())["decided"]["intention"] != "handed"
    rig.mind.turn_ended()
    rig.clock.advance(rig.mind.cfg.idle_settle_s + 1)
    assert (await rig.mind.tick())["decided"]["intention"] == "handed"


async def test_a_spent_day_makes_it_wait(cfg, seeded_vault, monkeypatch):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(handed=("think noted",)))
    asleep(rig)
    hand(rig)
    monkeypatch.setattr(rig.mind.budget, "pressure", lambda: 1.0)
    assert (await rig.mind.tick())["decided"]["intention"] != "handed"
    assert handed.waiting(rig.mind), "still waiting, not dropped"


async def test_its_goal_is_yours_not_hers_so_the_switch_and_cap_do_not_apply(
        cfg, seeded_vault):
    """The switch and the cap keep what she pursues traceable to you. This
    goal is — to the document you handed her."""
    rig = make_mind(cfg.model_copy(update={"mind_goal_filing_enabled": False,
                                           "mind_self_goals_max": 0}),
                    seeded_vault,
                    utility=ScriptedUtility(handed=(FILE, "think that's it")))
    asleep(rig)
    hand(rig)
    trace = await rig.mind.tick()
    assert trace["acted"].get("goal"), trace["acted"]


async def test_every_step_of_that_goal_is_shown_the_document(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(handed=(FILE, "think that's it")))
    asleep(rig)
    hand(rig)
    goal = rig.mind.goals.get((await rig.mind.tick())["acted"]["goal"])
    shown = await goalwork.context(rig.mind, goal)
    assert "THE DOCUMENT THEY HANDED YOU — inbox/q3.md" in shown
    assert "Revenue rose 12%" in shown


async def test_one_she_only_thinks_about_is_kept_in_the_journal(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        handed=("think Nice to know — nothing I'm writing needs it yet.",)))
    asleep(rig)
    hand(rig)
    trace = await rig.mind.tick()
    assert "goal" not in trace["acted"]
    assert "nothing I'm writing needs it yet" in journal(rig)


async def test_a_document_cleared_before_she_got_to_it(cfg, seeded_vault):
    utility = ScriptedUtility(handed=("think noted",))
    rig = make_mind(cfg.model_copy(update={"mind_inbox_wake": False}),
                    seeded_vault, utility=utility)
    asleep(rig)
    path = hand(rig)
    await rig.mind.tick()
    rig.mind.workspace.resolve(path).unlink()
    rig.mind.activity.state = "IDLE"
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "handed"
    assert "gone" in trace["acted"]["result"]
    assert sat_down(utility) == [], "no model call for a file that isn't there"
    assert [h["state"] for h in rig.mind.handed] == ["gone"]


async def test_one_she_read_herself_in_conversation_is_read(cfg, seeded_vault):
    """Live, 2 Oct: handed mid-conversation, read and rewritten in the replies
    it came up in — and still "not read yet" while the budget held the sitting
    off. Her own successful `read_note` of the path is the read."""
    utility = ScriptedUtility(handed=("think noted",))
    rig = make_mind(cfg, seeded_vault, utility=utility)
    path = hand(rig)
    rig.mind.turn_started()
    await rig.mind.tick()                       # sensed; the room comes first

    rig.mind.tool_called({"tool": "read_note", "verdict": "ok",
                          "args": {"path": "inbox/other.md"}}, talking=True)
    rig.mind.tool_called({"tool": "read_note", "verdict": "error",
                          "args": {"path": path}}, talking=True)
    rig.mind.tool_called({"tool": "count_note_lines", "verdict": "ok",
                          "args": {"path": path}}, talking=True)
    assert handed.waiting(rig.mind), "only her reading it, and it landing, counts"

    rig.mind.tool_called({"tool": "read_note", "verdict": "ok",
                          "args": {"path": path}}, talking=True)
    record = rig.mind.handed[0]
    assert record["state"] == handed.READ and record["read_at"]
    assert "conversation" in record["outcome"]

    rig.mind.turn_ended()
    rig.clock.advance(rig.mind.cfg.idle_settle_s + 1)
    assert (await rig.mind.tick())["decided"]["intention"] != "handed"
    assert not sat_down(utility), "no second reading of what she already read"
    restarted = make_mind(cfg, seeded_vault, clock=rig.clock)
    assert restarted.mind.handed[0]["state"] == handed.READ, "persisted at once"


async def test_waiting_documents_survive_a_restart(cfg, seeded_vault):
    quiet = cfg.model_copy(update={"mind_inbox_wake": False})
    rig = make_mind(quiet, seeded_vault)
    asleep(rig)
    path = hand(rig)
    await rig.mind.tick()
    restarted = make_mind(quiet, seeded_vault, clock=rig.clock)
    assert [h["path"] for h in restarted.mind.handed] == [path]


async def test_the_same_file_handed_twice_is_one_document(cfg, seeded_vault):
    rig = make_mind(cfg.model_copy(update={"mind_inbox_wake": False}),
                    seeded_vault)
    asleep(rig)
    hand(rig)
    hand(rig, text=DOC + "\nRevised.\n")
    await rig.mind.tick()
    assert len(rig.mind.handed) == 1


async def test_one_she_only_thought_about_keeps_the_thought(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        handed=("think Nice to know — nothing I'm writing needs it yet.",)))
    asleep(rig)
    hand(rig)
    await rig.mind.tick()
    (record,) = rig.mind.handed
    assert record["state"] == "read" and "goal" not in record
    assert "nothing I'm writing needs it yet" in record["outcome"]


async def test_a_new_copy_of_something_she_read_waits_again(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(handed=("think noted",)))
    asleep(rig)
    hand(rig)
    await rig.mind.tick()
    assert handed.waiting(rig.mind) == []
    hand(rig, text=DOC + "\nRevised.\n")
    rig.mind.cfg.mind_inbox_wake = False   # sense it, but don't sit down yet
    await rig.mind.tick()
    (record,) = rig.mind.handed
    assert record["state"] == "waiting" and "outcome" not in record


def test_old_reads_make_room_but_nothing_waiting_is_dropped(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    read = [{"path": f"inbox/r{i}.md", "name": f"r{i}.md", "state": "read"}
            for i in range(handed.MAX_KEPT)]
    unread = [{"path": f"inbox/w{i}.md", "name": f"w{i}.md", "state": "waiting"}
              for i in range(3)]
    rig.mind.handed = unread + read
    handed._keep(rig.mind)
    assert len(rig.mind.handed) == handed.MAX_KEPT
    assert handed.waiting(rig.mind) == unread
    assert rig.mind.handed[3]["path"] == "inbox/r3.md", "the oldest reads went"
