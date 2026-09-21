"""The split brain, closed, and the goal lifecycle that follows from it.

Phase 1 of the autonomy work is one claim: the talking-self and the
intending-self are the same person. Phase 2 is what that person can then
actually do — work a goal across ticks instead of writing one paragraph about
it and marking it done. Both are asserted here against the real MindLoop on a
VirtualClock, because "she does things while you're gone" is a behaviour over
hours and nothing shorter can check it.
"""
from __future__ import annotations

import json

from yurios.app.core.assemble import assemble
from yurios.app.core.soul import Soul

from .conftest import ScriptedUtility, make_mind, run_mind


# --- the conversational prompt knows what she is already working on ------------

def _soul() -> Soul:
    return Soul(name="Yuri", voice_law="Speak plainly.", backbone="A person.",
                personality="warm", scenario="A room.", examples="",
                hard_limits="", card_version="test", return_greetings=[])


def test_assemble_carries_her_open_goals():
    prompt = assemble(_soul(), user_md="They are called Sam.", summary="",
                      memories=[], lore=[], window=[], user_msg="hey",
                      goals=["ask how the interview went",
                             "read the paddleboard thing (waiting)"])
    assert "WHAT YOU'RE WORKING ON" in prompt.system
    assert "ask how the interview went" in prompt.system
    assert "List status: COMPLETE" in prompt.system
    assert "still open" in prompt.system
    assert "working notes" in prompt.system
    # the state is in the line, because "waiting" and "not started" are
    # different things to bring up
    assert "(waiting)" in prompt.system
    assert prompt.dropped_goals == 0


def test_a_capped_goal_snapshot_says_it_is_partial():
    prompt = assemble(_soul(), user_md="They are called Sam.", summary="",
                      memories=[], lore=[], window=[], user_msg="review these",
                      goals=["newer goal", "newest goal"], goals_complete=False)
    assert "List status: PARTIAL" in prompt.system
    assert "Do not present this as a complete review" in prompt.system


def test_goals_are_dropped_before_user_md_on_overflow():
    """The §7.2 ladder, extended: goals are a luxury; USER.md never is."""
    prompt = assemble(_soul(), user_md="They are called Sam.", summary="",
                      memories=[], lore=[], window=[], user_msg="hey",
                      goals=[f"a standing goal number {i}" for i in range(80)],
                      system_budget_tokens=180)
    assert prompt.dropped_goals > 0
    assert "They are called Sam." in prompt.system, "USER.md is never dropped"
    assert "THE HONESTY CONSTRAINT" in prompt.system


async def test_the_prompt_she_talks_through_carries_the_goal_store(cfg, seeded_vault):
    """End to end on the real brain: a goal in `goals.md` reaches the system
    block of the very next turn she assembles."""
    rig = make_mind(cfg, seeded_vault)
    rig.mind.goals.add("find out what happened with the landlord",
                       kind="task", provenance="promise:her-own-words")
    session = rig.mind.brain.resolve_session(None)
    _soul_, prompt = rig.mind.brain._assemble(session, "hi", window=[], lore=[])
    system = prompt.messages[0]["content"]
    assert "WHAT YOU'RE WORKING ON" in system
    assert "find out what happened with the landlord" in system


async def test_goals_in_prompt_cap_marks_the_snapshot_partial(cfg, seeded_vault):
    cfg = cfg.model_copy(update={"goals_in_prompt": 1})
    rig = make_mind(cfg, seeded_vault)
    rig.mind.goals.add("the older open goal", kind="task")
    rig.mind.goals.add("the newest open goal", kind="task")
    session = rig.mind.brain.resolve_session(None)

    _soul_, prompt = rig.mind.brain._assemble(session, "review your goals",
                                               window=[], lore=[])

    system = prompt.messages[0]["content"]
    assert "the newest open goal" in system
    assert "the older open goal" not in system
    assert "List status: PARTIAL" in system


# --- the promise scan, in the register she actually promises in ----------------

async def test_a_soft_offer_is_a_promise_too(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.say("do you know what a springbone is?",
            reply="Let me look that up properly tonight.")
    await rig.mind.tick()
    assert any("look that up" in g.text for g in rig.mind.goals.open_goals())


async def test_an_offer_waiting_on_an_answer_is_not_a_promise(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.say("the landlord thing",
            reply="I can read up on tenancy law, if you want.")
    await rig.mind.tick()
    assert not [g for g in rig.mind.goals.open_goals()
                if "tenancy" in g.text]


# --- the bootstrap handoff (SPEC §5.4) ------------------------------------------

async def test_bootstrap_retirement_files_one_standing_goal(cfg, seeded_vault):
    """`BOOTSTRAP.md` says the runtime should be left holding one standing
    reach-out. It was prose; now it is a goal."""
    soul = seeded_vault / "soul"
    (soul / "onboarded").mkdir(parents=True, exist_ok=True)
    (soul / "BOOTSTRAP.md").rename(soul / "onboarded" / "BOOTSTRAP.done.md")
    (soul / "USER.md").write_text(
        "# User model\n\n## Ongoing\n\n- Waiting to hear about a job interview.\n")

    rig = make_mind(cfg, seeded_vault)
    await rig.mind.tick()
    filed = [g for g in rig.mind.goals.all()
             if g.provenance == "bootstrap:first-session"]
    assert len(filed) == 1
    assert filed[0].kind == "reach_out"
    assert "job interview" in filed[0].text

    # …once. A day of ticking does not file it again.
    for _ in range(5):
        rig.clock.advance(3600)
        await rig.mind.tick()
    assert len([g for g in rig.mind.goals.all()
                if g.provenance == "bootstrap:first-session"]) == 1


async def test_no_bootstrap_no_handoff(cfg, seeded_vault):
    """A vault that never had one (an imported card) files nothing, and stops
    looking rather than checking the same absent file forever."""
    (seeded_vault / "soul" / "BOOTSTRAP.md").unlink()
    rig = make_mind(cfg, seeded_vault)
    await rig.mind.tick()
    assert not [g for g in rig.mind.goals.all()
                if g.provenance == "bootstrap:first-session"]
    assert rig.mind.bootstrapped_on


# --- the lifecycle (SPEC §22) ---------------------------------------------------

async def test_a_goal_stays_active_across_ticks_and_reads_its_own_desk(
        cfg, seeded_vault):
    utility = ScriptedUtility(
        "think the tiles come off with a heat gun, not a chisel.",
        "think and the grout needs replacing after.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add("work out how to take the bathroom tiles off",
                              kind="task", priority=0.9)
    events = rig.mind.hub.subscribe()

    await rig.mind.tick()
    after_one = rig.mind.goals.get(goal.id)
    assert after_one.state == "active", "one step is not the whole goal"
    assert after_one.steps == 1
    # the working note went to her desk, not only the journal
    desk = rig.mind.workspace.read(f"goals/{goal.id}.md", default="")
    assert "heat gun" in desk
    published = []
    while not events.empty():
        published.append(events.get_nowait())
    rig.mind.hub.unsubscribe(events)
    assert {"type": "workspace", "action": "append",
            "path": f"goals/{goal.id}.md"} in published

    # the next step reads it back — which is the whole point of the desk
    rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 60)
    await rig.mind.tick()
    context = utility.calls[-1][-1]["content"]
    assert "heat gun" in context
    assert "WHAT YOU HAVE ALREADY WORKED OUT ON THIS" in context


async def test_goal_work_gets_the_same_context_as_conversation(cfg, seeded_vault):
    """She was measurably dumber alone than she is talking to you. The private
    step now carries the desk, the skills, the situation and the facts."""
    utility = ScriptedUtility("think noted.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    rig.mind.workspace.write("notes/paddleboards.md", "the wide one is stabler")
    rig.mind.goals.add("pick a paddleboard", kind="task", priority=0.9)
    await rig.mind.tick()

    context = utility.calls[-1][-1]["content"]
    for block in ("THE GOAL", "ABOUT IT", "THE SITUATION RIGHT NOW", "YOUR DESK"):
        assert block in context, f"{block} missing from the private prompt"
    assert "notes/paddleboards.md" in context


async def test_the_horizon_parks_a_goal_instead_of_looping_forever(
        cfg, seeded_vault):
    cfg = cfg.model_copy(update={"mind_goal_max_steps": 2})
    utility = ScriptedUtility("think one.", "think two.", "think three.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add("think about the shed", kind="task", priority=0.9)

    for _ in range(3):
        await rig.mind.tick()
        rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 60)

    parked = rig.mind.goals.get(goal.id)
    assert parked.state == "waiting", "the step budget has to end somewhere"
    assert parked.steps == 2
    assert goal.id in rig.mind.wakeups, "…and it must be woken again, not lost"


async def test_a_goal_can_say_it_is_finished(cfg, seeded_vault):
    utility = ScriptedUtility("think it's the heat gun. goal complete.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add("work out the tiles", kind="task", priority=0.9)
    await rig.mind.tick()
    assert rig.mind.goals.get(goal.id).state == "done"


async def test_open_minded_stale_goals_still_abandon_on_a_suspend_gap(
        cfg, seeded_vault):
    """The Phase 2 lifecycle must not have cost the §22.2 behaviour that
    already worked."""
    rig = make_mind(cfg, seeded_vault)
    from yurios.mind.util import iso_of
    goal = rig.mind.goals.add(
        "mention the weather", kind="task", commitment="open-minded",
        due=iso_of(rig.clock.now() + 60))
    rig.clock.advance(10 * 3600)
    await rig.mind.tick()
    assert rig.mind.goals.get(goal.id).state == "abandoned"


async def test_reconsider_runs_on_the_day_rollover_not_only_after_a_sleep(
        cfg, seeded_vault):
    """It used to run on `suspend_gap` alone — i.e. only when the machine had
    slept two hours — so a goal that went stale while she was awake was
    defended by a strategy nobody ever asked."""
    rig = make_mind(cfg, seeded_vault)
    await rig.mind.tick()                       # today's rollover happens here
    from yurios.mind.util import iso_of
    goal = rig.mind.goals.add(
        "catch the post", kind="task", commitment="open-minded",
        due=iso_of(rig.clock.now() + 3600))

    # tick across local midnight in steps too small to be a suspend gap
    for _ in range(40):
        rig.clock.advance(1800)
        await rig.mind.tick()
        if rig.mind.goals.get(goal.id).state == "abandoned":
            break
    assert rig.mind.goals.get(goal.id).state == "abandoned"


# --- maintenance provenance (SPEC §22) -------------------------------------------

async def test_a_shelf_goal_left_standing_a_day_does_the_reading_itself(
        cfg, seeded_vault):
    """The due time is what makes it a plan rather than a record: once it is
    due it outranks the ingest impulse, and the goal that stands for the
    leftover is the thing that clears it."""
    rig = make_mind(cfg, seeded_vault)
    await rig.mind.tick()                       # today's rollover files nothing

    drop = seeded_vault / "knowledge" / "reference"
    drop.mkdir(parents=True, exist_ok=True)
    (drop / "tides.md").write_text("The tide comes in twice a day.\n")
    rig.clock.advance(25 * 3600)
    await rig.mind.tick()                       # tomorrow's rollover files it
    goal = next(g for g in rig.mind.goals.open_goals()
                if g.meta.get("auto") == "shelf")
    assert goal.due, "a leftover with no due time can never outrank the impulse"

    # …and once it comes due, the goal itself is the winning intention
    rig.clock.advance(25 * 3600)
    (drop / "moon.md").write_text("The moon pulls the water.\n")
    seen = []
    for _ in range(4):
        trace = await rig.mind.tick()
        seen.append(trace["decided"]["intention"])
        rig.clock.advance(3600)
    assert any(i.startswith("goal:read what's still sitting") for i in seen), seen
    assert not rig.mind.knowledge.pending_docs()


async def test_the_dream_backlog_goal_never_starts_a_night_off_schedule(
        cfg, seeded_vault):
    """The night IS the schedule. A goal that outranked the dream window would
    be one half of the system arguing with the other."""
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("catch up on the nights", kind="maintenance",
                              priority=0.95, provenance="maintenance:dream",
                              meta={"auto": "dream"})
    rig.mind.activity.state = "IDLE"
    acted, _interrupt, notes = await rig.mind._act_goal_work(goal)
    assert acted["what"] is None
    assert "tonight" in acted["result"]
    assert rig.mind.goals.get(goal.id).state != "done"
    assert notes


async def test_a_night_that_clears_the_backlog_closes_the_goal_that_recorded_it(
        cfg, seeded_vault):
    """The direct DREAM impulse outranks the standing goal (0.6 over 0.24, by
    design — the night IS the schedule), so the goal that stands for the
    backlog can only be closed by the act that clears it. The day rollover
    cannot: it runs at local midnight, the exact tick yesterday's journal
    becomes new backlog, so it never sees the leftover cleared. The goal used
    to sit `pending` forever in that gap — a month in one live vault while
    every night succeeded."""
    rig = make_mind(cfg, seeded_vault)
    # a Monday exchange lands in the episodic journal, as the real brain writes it
    from yurios.app.memory.store import Record
    import datetime
    await rig.mind.brain.state.store.remember(Record(
        session_id="s1", turn_index=0,
        user_msg="remember I hate mondays. see you next week",
        reply="Noted, and survived. Go.",
        ts=datetime.datetime(2026, 7, 6, 9, 0)))
    rig.say("remember I hate mondays. see you next week",
            reply="Noted, and survived. Go.")
    await rig.mind.tick()                      # Monday's exchange is journalled
    rig.mind.bus.post("user_absent", {}, source="frontend")

    traces = await run_mind(rig, hours=30)     # one rollover, one night

    # the night ran, and finished Monday…
    progress = seeded_vault / "state" / "dream_progress.json"
    assert progress.exists() and "2026-07-06" in progress.read_text()
    assert any(t["activity_state"] == "DREAM" for t in traces)
    # …so the goal the rollover filed for that backlog is closed, not still
    # pending on the goals page
    dream_goals = [g for g in rig.mind.goals.all()
                   if g.meta.get("auto") == "dream"]
    assert dream_goals, "the midnight rollover filed the standing goal"
    assert all(g.state == "done" for g in dream_goals), \
        [(g.id, g.state) for g in dream_goals]


async def test_a_standing_shelf_leftover_becomes_a_goal_and_closes_itself(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    drop = seeded_vault / "knowledge" / "reference"
    drop.mkdir(parents=True, exist_ok=True)
    (drop / "tides.md").write_text("The tide comes in twice a day.\n")

    await rig.mind.tick()
    filed = [g for g in rig.mind.goals.open_goals()
             if g.meta.get("auto") == "shelf"]
    assert len(filed) == 1
    assert filed[0].provenance == "maintenance:shelf"

    # the goal stands for the leftover, so working it does the reading — and
    # once the shelf is clear the goal closes rather than lingering as a to-do
    # nobody can finish by reading it
    for _ in range(4):
        rig.clock.advance(25 * 3600)
        await rig.mind.tick()
        if not rig.mind.knowledge.pending_docs():
            break
    rig.clock.advance(25 * 3600)
    await rig.mind.tick()
    assert not [g for g in rig.mind.goals.open_goals()
                if g.meta.get("auto") == "shelf"]


# --- `propose_edit` (SPEC §23) ---------------------------------------------------

#: A rewrite shaped the way a soul file has to be: `soul.yaml` points at both
#: headings and at the frontmatter key, and all three survive.
PERSONA_V2 = """---
soul: persona
mutable: true
personality: "quiet, careful"
---
# Persona

## Appearance

Softer now.

## Manner

Quieter than she was.
"""


async def test_propose_edit_queues_persona_and_refuses_the_constitution(
        cfg, seeded_vault, clock, controller):
    from yurios.world.tools.fakes import FakeToolRunner
    from yurios.world.tooltags import ToolCall

    rig = make_mind(cfg, seeded_vault)
    runner = FakeToolRunner()
    rig.mind.brain.set_tools(runner, [])
    rig.mind.brain.guard.allow("propose_edit", 6)

    result = await rig.mind.brain._execute(
        ToolCall("propose_edit", {"surface": "soul/PERSONA.md",
                                  "content": PERSONA_V2,
                                  "reason": "I have got quieter this year."}))
    assert "proposed" in result
    pending = rig.mind.selfedit.pending()
    assert len(pending) == 1
    assert pending[0]["surface"] == "soul/PERSONA.md"
    assert "quieter" in pending[0]["reason"]
    # queued, never applied: the file on disk is untouched until a person rules
    assert "Softer now." not in (seeded_vault / "soul" / "PERSONA.md").read_text()

    denied = await rig.mind.brain._execute(
        ToolCall("propose_edit", {"surface": "soul/CONSTITUTION.md",
                                  "content": "anything", "reason": "no"}))
    assert "error" in denied
    assert len(rig.mind.selfedit.pending()) == 1, "not even a queued proposal"


def test_the_server_contract_names_only_the_editable_surfaces():
    from yurios.world.tools.server import PROPOSABLE
    assert "CONSTITUTION.md" not in PROPOSABLE
    assert "PERSONA.md" in PROPOSABLE
    # USER.md and MEMORY.md are the runtime's to write, not hers to redraft
    assert "USER.md" not in PROPOSABLE and "MEMORY.md" not in PROPOSABLE


# --- the return path (SPEC §16) ---------------------------------------------------

async def test_a_finished_selfie_posts_task_completion_and_the_tick_journals_it(
        cfg, seeded_vault, clock):
    """`task_completion` was declared, scored, handled, folded into the world
    model — and posted by nothing."""
    from yurios.world.selfies import SelfieLab

    rig = make_mind(cfg, seeded_vault, clock=clock)
    posted: list[tuple] = []
    lab = SelfieLab.__new__(SelfieLab)
    lab.signal = lambda t, p, source="host": posted.append((t, p, source))
    SelfieLab._completed(lab, {"id": "s1"}, {"noun": "selfie"})
    assert posted and posted[0][0] == "task_completion"

    rig.mind.bus.post("task_completion", {"task": "a selfie she took",
                                          "kind": "selfie"}, source="selfies")
    trace = await rig.mind.tick()
    assert trace["acted"]["what"] == "noted"
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("finished something I'd started" in p.read_text()
               for p in day_files)


def test_a_research_run_the_mind_started_posts_nothing_into_the_chat(cfg, clock):
    """The landing rule (SPEC §18, principle 8): a tool product is not a
    delivery. `_say` is the only way a run reaches the chat, so this is the
    whole of it."""
    from yurios.world.research import Researcher

    posted: list = []
    r = Researcher.__new__(Researcher)
    r.post = lambda *a, **kw: posted.append((a, kw))
    Researcher._say(r, {"topic": "tides"}, "read up on tides: …")
    assert len(posted) == 1, "an ordinary run still answers the person who asked"

    posted.clear()
    Researcher._say(r, {"topic": "tides", "_deliver": "vault"},
                    "read up on tides: …")
    assert posted == [], "work she started for herself lands in the vault"


def test_the_contract_stamp_names_the_goal_that_wanted_it():
    from yurios.mind.hands import stamp_contract
    stamped = stamp_contract({"topic": "tides"}, goal_id="g-1")
    assert stamped["_deliver"] == "vault"      # principle 8
    assert stamped["_goal_id"] == "g-1"        # principle 7
    assert json.loads(json.dumps(stamped)) == stamped


# --- she works with her memory in the room (SPEC §22.4) -----------------------
#
# The facts block above is the *semantic* residue — what DREAM kept because it
# would still be true in a month, with the evening it came from burned off.
# Working alone she had only that, so she knew a thing about you and not the
# night you said it. These pin the episodic half.

def _remember(rig, text: str, *, when: float | None = None) -> None:
    """Put one episode in the index the conversation recalls from.

    `utc_iso_of`, not `iso_of`: the index is read back by `_recency`,
    which subtracts an aware `now` — the journal writes it aware for the
    same reason.
    """
    from yurios.mind.util import utc_iso_of
    store = rig.mind.store
    store.index.upsert(
        id=f"ep-{abs(hash(text))}", kind="turn", text=text,
        source_path="memory/episodic/test.md", source_span="",
        created_at=utc_iso_of(when if when is not None
                              else rig.clock.now()),
        embedding=store.embedder.embed([text])[0])


async def test_goal_work_recalls_what_she_remembers(cfg, seeded_vault):
    """The private step asks the same index the conversation asks."""
    utility = ScriptedUtility("think noted.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    _remember(rig, "they want a wide paddleboard, it felt stabler")
    rig.mind.goals.add("pick a paddleboard for them", kind="task",
                       priority=0.9)
    await rig.mind.tick()

    context = utility.calls[-1][-1]["content"]
    assert "THINGS THAT MAY BE RELEVANT" in context
    assert "it felt stabler" in context, \
        "she worked the goal without the evening it came from"


async def test_a_cold_index_is_not_a_reason_to_skip_the_step(cfg, seeded_vault):
    """Recall is context, not a precondition. A store that cannot answer must
    cost her the block and nothing else."""
    utility = ScriptedUtility("think noted.")
    rig = make_mind(cfg, seeded_vault, utility=utility)

    def boom(*a, **k):
        raise RuntimeError("index is cold")

    rig.mind.store.recall = boom
    goal = rig.mind.goals.add("work out the tiles", kind="task", priority=0.9)
    await rig.mind.tick()

    context = utility.calls[-1][-1]["content"]
    assert "THE GOAL" in context and "THINGS THAT MAY BE RELEVANT" not in context
    assert rig.mind.goals.get(goal.id).steps == 1, "the step still happened"


async def test_the_same_sentence_is_not_served_under_two_headings(
        cfg, seeded_vault):
    """A fact DREAM already distilled is in the facts block. Recalling it again
    reads as two pieces of evidence for one thing."""
    utility = ScriptedUtility("think noted.")
    rig = make_mind(cfg, seeded_vault, utility=utility)
    fact = "they keep the good knives in the second drawer"
    rig.mind.vault.write("memory/semantic/facts.md", f"- (2026-08-01) {fact}\n")
    _remember(rig, fact)
    # a second, distinct episode, so the block is present either way and this
    # cannot pass by recalling nothing at all
    _remember(rig, "the good knives want sharpening before the knives blunt")
    rig.mind.goals.add("sharpen the knives", kind="task", priority=0.9)
    await rig.mind.tick()

    context = utility.calls[-1][-1]["content"]
    assert "THINGS THAT MAY BE RELEVANT" in context
    assert "want sharpening" in context
    assert context.count(fact) == 1, "the same sentence under two headings"


# --- the news half survives being filed (§22.1) --------------------------------
#
# A promise splits into work and news. The news is filed as a `followup:<id>`
# goal whose text quotes its parent so it can say what it is about — and that
# quotation scored a 1.0 overlap against the parent, which was still open, so
# `add()` handed the follow-up back as a duplicate and it never existed. Four
# of the first seven promises she kept were never mentioned to anybody.

def _goals(rig):
    return rig.mind.goals


async def test_a_follow_up_is_not_absorbed_by_the_goal_it_reports_on(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    parent = _goals(rig).add("write down her research findings properly",
                             kind="task", provenance="promise:her-own-words")
    news = _goals(rig).add(
        f"tell them what came of “{parent.text}” — it's in "
        f"goals/{parent.id}.md",
        kind="reach_out", provenance=f"followup:{parent.id}")

    assert news.id != parent.id, \
        "the news was merged back into the work it was reporting on"
    assert news.kind == "reach_out"
    assert len(_goals(rig).all()) == 2


async def test_two_promises_kept_at_once_are_both_told(cfg, seeded_vault):
    """Unrelated follow-ups share only "tell", "came" and "goals" — three
    content words, 0.38 overlap, enough to merge on boilerplate alone."""
    rig = make_mind(cfg, seeded_vault)
    first = _goals(rig).add("write down her research findings properly",
                            kind="task", provenance="promise:her-own-words")
    second = _goals(rig).add("remind the user about tea after two minutes",
                             kind="task", provenance="promise:her-own-words")
    a = _goals(rig).add(f"tell them what came of “{first.text}” — it's "
                        f"in goals/{first.id}.md",
                        kind="reach_out", provenance=f"followup:{first.id}")
    b = _goals(rig).add(f"tell them what came of “{second.text}” — it's "
                        f"in goals/{second.id}.md",
                        kind="reach_out", provenance=f"followup:{second.id}")

    ids = {first.id, second.id}
    assert a.id not in ids and b.id not in ids, \
        "a follow-up was absorbed by a parent before it could collide"
    assert a.id != b.id, "the second promise kept lost its news to the first"


async def test_the_same_promise_is_not_queued_for_telling_twice(cfg, seeded_vault):
    """Filing on provenance is an exact test, so it is also idempotent."""
    rig = make_mind(cfg, seeded_vault)
    parent = _goals(rig).add("write down her research findings properly",
                             kind="task", provenance="promise:her-own-words")
    one = _goals(rig).add("tell them how it went", kind="reach_out",
                          provenance=f"followup:{parent.id}")
    two = _goals(rig).add("tell them how it turned out in the end",
                          kind="reach_out", provenance=f"followup:{parent.id}")

    assert one.id == two.id


async def test_a_goal_she_is_already_carrying_is_still_not_filed_twice(
        cfg, seeded_vault):
    """The original question `echoes` exists to ask, unchanged."""
    rig = make_mind(cfg, seeded_vault)
    first = _goals(rig).add("write down her research findings properly",
                            kind="task", provenance="promise:her-own-words")
    again = _goals(rig).add("properly write down the research findings",
                            kind="task", provenance="promise:her-own-words")

    assert again.id == first.id


async def test_a_promise_she_keeps_is_a_promise_they_hear_about(cfg, seeded_vault):
    """End to end, on the path that lost them: a long-texted promise finishes,
    and the news goal it files is still there afterwards."""
    utility = ScriptedUtility(*["think goal complete — written up properly."] * 6)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    parent = rig.mind.goals.add("write down her research findings properly",
                                kind="task", priority=0.95,
                                provenance="promise:her-own-words")

    for _ in range(3):
        await rig.mind.tick()
        rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 60)

    assert rig.mind.goals.get(parent.id).state == "done"
    news = [g for g in rig.mind.goals.all()
            if g.provenance == f"followup:{parent.id}"]
    assert news, "she kept the promise and filed nothing to tell them about it"
    assert news[0].kind == "reach_out"
