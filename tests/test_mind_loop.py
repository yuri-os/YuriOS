"""The tick loop's mechanics (SPEC §15) — one intention per tick, the trace,
the ENGAGED preempt, the goal store, and the strings it inherited from the
idle machine it replaced.
"""
from __future__ import annotations

import subprocess

from yurios.mind.goals import extract_promises

from .conftest import make_mind, run_mind


async def test_one_intention_per_tick_and_the_trace_shape(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    trace = await rig.mind.tick()
    for key in ("tick_id", "activity_state", "sensed", "appraised",
                "decided", "acted", "interrupt"):
        assert key in trace
    assert trace["decided"]["intention"] == "REST"         # a quiet room rests
    # the trace file is the same record
    assert rig.mind.trace.tail(1)[0]["tick_id"] == trace["tick_id"]


async def test_user_message_preempts_to_engaged(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.mind.activity.state = "DORMANT"
    rig.say("hey")
    trace = await rig.mind.tick()
    assert rig.mind.activity.state == "ENGAGED"
    assert any(s["type"] == "user_message" for s in trace["sensed"])


async def test_proactive_turn_does_not_preempt_to_engaged(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.mind.activity.state = "IDLE"
    rig.mind.turn_started(proactive=True)
    assert rig.mind.activity.state == "IDLE"        # her own murmur doesn't force ENGAGED
    assert rig.mind._turns_in_flight == 1            # bookkeeping still happens


async def test_real_turn_still_preempts_to_engaged(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.mind.activity.state = "IDLE"
    rig.mind.turn_started()
    assert rig.mind.activity.state == "ENGAGED"


async def test_new_document_gets_read_and_journaled(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    ref = seeded_vault / "knowledge" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "manual.md").write_text("The kettle whistles at 93 degrees.\n")
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "ingest"
    assert rig.mind.knowledge.search("kettle degrees", k=1)
    # journaled — the act is visible in her day file
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("read and shelved manual.md" in p.read_text() for p in day_files)


async def test_every_dirty_tick_is_one_commit(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)

    def commits():
        out = subprocess.run(["git", "-C", str(seeded_vault), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        return out.strip().splitlines()

    before = len(commits())
    await rig.mind.tick()                                  # a REST tick: no commit
    assert len(commits()) == before
    rig.say("hi")                                          # observe() dirties state
    await rig.mind.tick()
    after = commits()
    assert len(after) == before + 1
    assert after[0].split(" ", 1)[1].startswith("tick ")


async def test_self_talk_needs_company_and_a_long_quiet(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.speak.connected = True
    rig.mind.bus.post("user_present", {}, source="frontend")
    await rig.mind.tick()
    rig.mind.activity.state = "IDLE"
    rig.mind._last_turn_end = rig.clock.now() - 600        # long past settle
    rig.clock.advance(400)                                 # past the talk window
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "self_talk"
    assert any("yourself" in c["cue"] for c in rig.speak.calls)

    # …and never into an empty room: with the user absent it doesn't even appraise
    rig.mind.bus.post("user_absent", {}, source="frontend")
    rig.mind._next_self_talk = rig.clock.now()
    rig.clock.advance(1)
    trace2 = await rig.mind.tick()
    assert trace2["decided"]["intention"] != "self_talk"


async def test_goal_work_is_silent_and_journaled(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.mind.goals.add("sort my notes on the rain sounds", kind="maintenance",
                       priority=0.8, provenance="maintenance:dream")
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"].startswith("goal:")
    assert trace["acted"]["what"] == "goal_work"
    assert rig.post.proactive() == []                      # never a message
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("worked on: sort my notes" in p.read_text() for p in day_files)


async def test_budget_is_debited_by_her_own_words(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    before = rig.mind.budget.snapshot()["spent_tokens"]
    await rig.mind._compose("((say one line))")
    assert rig.mind.budget.snapshot()["spent_tokens"] > before


async def test_rest_majority_over_a_quiet_day(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    traces = await run_mind(rig, hours=12)
    rests = [t for t in traces if t["decided"]["intention"] == "REST"]
    assert len(rests) > len(traces) * 0.8


# --- the goal store ----------------------------------------------------------

async def test_goals_roundtrip_and_dedupe(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    g = rig.mind.goals.add("water the plant", kind="maintenance", priority=0.7,
                           commitment="open-minded", provenance="user")
    same = rig.mind.goals.add("water the plant")
    assert same.id == g.id                                 # near-duplicates merge
    text = (seeded_vault / "goals.md").read_text()
    assert "water the plant" in text and "open-minded" in text
    parsed = rig.mind.goals.get(g.id)
    assert parsed.priority == 0.7 and parsed.commitment == "open-minded"


async def test_reconsider_drops_stale_open_minded_only(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    from yurios.mind.util import iso_of
    past = iso_of(rig.clock.now() - 3600)
    rig.mind.goals.add("share that article", commitment="open-minded", due=past)
    rig.mind.goals.add("birthday reminder", commitment="blind", due=past)
    rig.mind.goals.reconsider()
    states = {g.text: g.state for g in rig.mind.goals.all()}
    assert states["share that article"] == "abandoned"     # impulse, dropped
    assert states["birthday reminder"] == "pending"        # promise, defended


def test_promise_scan_shapes():
    out = extract_promises("I'll look into VRM springbones tonight.",
                           "also remind me to buy rice")
    texts = [t for t, _ in out]
    assert any("look into VRM springbones" in t for t in texts)
    assert any("buy rice" in t for t in texts)
    provs = [p for _, p in out]
    assert "promise:her-own-words" in provs and "user:remind-me" in provs
    # negations are not promises
    assert extract_promises("I'll never leave.", "") == []


async def test_what_she_read_reaches_the_next_prompt(cfg, seeded_vault):
    """§20.2 end to end, on the real MindLoop and the real brain: a doc lands on
    the shelf, the tick ingests it, and the *next turn she assembles* carries it
    with its citation. Everything the web hands shelve arrives the same way —
    `read_page`, `research` and a book you dropped are one shelf and one slot."""
    rig = make_mind(cfg, seeded_vault)
    ref = seeded_vault / "knowledge" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "web-sencha.md").write_text(
        "# Sencha\n\nSource: https://example.invalid/sencha\n\n"
        "Sencha is steamed rather than pan-fired, which keeps it green.\n")
    await rig.mind.tick()                               # SENSE → ingest

    _soul, prompt = rig.mind.brain._assemble(
        "s1", "how is sencha made?", window=[], lore=[])
    assert "WHAT YOU'VE READ" in prompt.system
    assert "steamed rather than pan-fired" in prompt.system
    assert "web-sencha.md (chars" in prompt.system      # the citation, grounded


async def test_a_turn_survives_a_broken_shelf(cfg, seeded_vault):
    """Retrieval is an enhancement, never a dependency: an index that can't be
    searched costs the block, not the reply."""
    rig = make_mind(cfg, seeded_vault)

    class Broken:
        def search(self, *a, **kw):
            raise RuntimeError("index is half-written")

    rig.mind.brain.set_knowledge(Broken())
    _soul, prompt = rig.mind.brain._assemble("s1", "hello", window=[], lore=[])
    assert "WHAT YOU'VE READ" not in prompt.system
    assert "PERSONA BACKBONE" in prompt.system          # the turn still happened


# ---- the camera's door, from this side (§7.6) -------------------------------
# Her turns wait for a parked render to give the GPU back. So must the loop's
# own model calls: the DREAM selfie job starts a render and then asks the model
# about the next day, and that request used to JIT-load the whole chat model
# back onto the card the render was still filling. Two of four dreamt selfies
# died of OOM on a night whose logs said the parking worked — because it had.

async def test_an_off_turn_model_call_waits_for_a_parked_render(cfg, seeded_vault):
    import asyncio

    rig = make_mind(cfg, seeded_vault)
    gate = rig.mind.park_gate
    gate.bind(asyncio.get_running_loop())
    gate.close()                                    # a selfie has the card
    call = asyncio.create_task(rig.mind._utility(
        [{"role": "user", "content": "which moment wants a picture?"}]))
    await asyncio.sleep(0)
    assert not call.done()                          # queued at the door
    gate.open()
    assert await asyncio.wait_for(call, 1)          # and answered after it


async def test_a_park_starting_now_waits_for_the_call_already_running(
        cfg, seeded_vault):
    """The mirror case: don't pull the weights out from under a live job."""
    import asyncio

    rig = make_mind(cfg, seeded_vault)
    gate = rig.mind.park_gate
    gate.bind(asyncio.get_running_loop())
    started, release = asyncio.Event(), asyncio.Event()

    class SlowUtility:
        async def complete(self, messages, **kw):
            started.set()
            await release.wait()
            return "a picture of the rain"

    rig.mind.brain.state.utility = SlowUtility()
    call = asyncio.create_task(rig.mind._utility([{"role": "user", "content": "?"}]))
    await asyncio.wait_for(started.wait(), 1)
    quiet = asyncio.create_task(gate.wait_idle(timeout_s=5))
    await asyncio.sleep(0)
    assert not quiet.done()                         # the park holds off
    release.set()
    await asyncio.wait_for(call, 1)
    assert await asyncio.wait_for(quiet, 1) is True
