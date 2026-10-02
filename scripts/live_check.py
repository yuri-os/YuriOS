#!/usr/bin/env python3
"""live_check.py — the rig `pytest` cannot be: one real `Runtime`, driven by hand.

The offline suite (SPEC §27) is the gate, and it stays the gate. But every mind
test builds its loop with `make_mind`, which hands `MindLoop` a `PostRecorder`
where the host hands it `Runtime.post_message` — so the entire delivery chain
below the mind (the transcript ring, `conversation.jsonl`, the `message` event,
the inbox) is a seam the whole battery mocks and nothing exercises. A picture
that reached `post_message` and got no further was, to 1,929 green tests,
indistinguishable from one that landed in the chat. That is exactly the bug the
selfie of 2026-09-17 turned out to be, and the fixture had been dropping
`image_url` on the floor for long enough that no test *could* have caught it.

So this is the other half: her real code, in her real shapes, writing her real
files, asserted from disk afterwards the way the page reads them at boot.

    real         the Runtime, the brain, the memory store and embedder, the
                 selfie lab, her hands over MCP, the goal store, the journal,
                 the Vault and its git, `post_message`, `conversation.jsonl`
    faked        nothing structural. `SELFIE_BACKEND=mock` (real PNG, no GPU),
                 `STT/TTS/VAD=fake` (no microphone in a script), and a scripted
                 reply for the one utility call whose *word choice* is not what
                 is under test — the bookkeeping around it is all hers.
    never        her vault. A scratch character is created per run under
                 `.yurios/live-check/` and purged at the end (`--keep` to
                 inspect it). Notifications, Telegram and the tray are off, so a
                 run cannot reach anybody.

Usage
    .venv/bin/python scripts/live_check.py                # every scenario
    .venv/bin/python scripts/live_check.py picture        # one (or several)
    .venv/bin/python scripts/live_check.py --list
    .venv/bin/python scripts/live_check.py --keep         # leave the scratch root
    .venv/bin/python scripts/live_check.py --hour 14      # place her day (Gate 2)

Not a gate stage: it wants a model, a few seconds of wall clock, and a spawned
tool server. `./scripts/check.sh` stays the thing that has to be green.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yurios.app.conversation import ConversationLog          # noqa: E402
from yurios.characters.creator import create_character, template_draft  # noqa: E402
from yurios.characters.registry import CharacterRegistry      # noqa: E402
from yurios.kernel.clock import Clock                         # noqa: E402
from yurios.world.config import Config                        # noqa: E402
from yurios.world.host.hosting import config_for_character    # noqa: E402
from yurios.world.main import Runtime                         # noqa: E402

log = logging.getLogger("live_check")


class Failed(AssertionError):
    """One scenario's verdict. The message is the whole report."""


def want(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)


# --------------------------------------------------------------------- the clock

class ShiftedClock(Clock):
    """Real time, moved sideways.

    Everything in a live run has to actually wait — a render, an MCP round
    trip, a model — so `VirtualClock` is out. But Gate 2's quiet hours are a
    *gate*, not a weight (`policy.score_interrupt`): outside 09:00–22:00 no
    threshold on earth produces a SPEAK, and a rig that only passes in the
    afternoon is a rig nobody runs at midnight. So real seconds elapse at their
    real length and only the hand on the dial moves.
    """

    def __init__(self, hour: int | None = None):
        self.offset = 0.0
        if hour is not None:
            now = datetime.datetime.now()
            at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            self.offset = at.timestamp() - now.timestamp()

    def now(self) -> float:
        return time.time() + self.offset


# ----------------------------------------------------------------------- the rig

#: Everything the scratch character overrides on top of the house `.env`. Two
#: kinds of entry and no third: things that must not reach the outside world,
#: and things that trade a slow real backend for a fast real one. Nothing here
#: replaces a code path — the moment one does, the rig stops being evidence.
OVERRIDES: dict = {
    # …no photographs of anybody, no GPU, no minutes: the mock forge is a real
    # ImageBackend writing a real PNG through the real lab (forge/backends/mock.py).
    "selfie_backend": "mock",
    # there is no microphone in a script
    "stt_backend": "fake", "tts_backend": "fake", "vad_backend": "fake",
    # a run must not be able to reach a person
    "notify_enabled": False, "tray_enabled": False,
    "telegram_bot_token": "", "telegram_chat_id": "",
    "telegram_bot_token_env": "", "telegram_chat_id_env": "",
    # her hands, on purpose: the selfie scenario is only worth anything if the
    # camera is reached the way a tick reaches it (§26); `create_goal` because
    # its tool server answers a contract the mind has to file (§22.1c)
    "mind_tools_enabled": True,
    "mind_tool_allowlist": ("take_selfie,write_note,append_note,read_note,"
                            "list_notes,create_goal"),
    "mind_enabled": True, "utility_enabled": True,
    # DREAM would run a night in the middle of a scenario; nights have their own
    # tests and this rig is about the waking day
    "dream_enabled": False,
    # Gate 2's *tuning* is what the offline battery exists for (§27.2). Here the
    # question is only whether a picture survives the delivery once the gate has
    # said yes, so the gate is opened and the scenario asserts on what came out.
    "mind_interrupt_threshold": 0.0,
    "mind_max_interrupts_per_day": 20,
    # a hand-driven tick has no cadence between it and the next one, so the
    # cooldown that stops her re-chewing a goal every tick would stop her
    # working the same goal twice in one scenario
    "mind_consider_cooldown_s": 0.0,
}


class Rig:
    """One scratch character, alive, with the loop's heartbeat taken out.

    `start()` builds her exactly the way the host does — `config_for_character`
    over the house `.env`, then `Runtime.start_async` — and then cancels the
    task running `MindLoop.run`, because a rig that is racing her own heartbeat
    cannot say which tick did what. Every tick from then on is one this file
    asked for.
    """

    def __init__(self, root: Path, *, hour: int | None = None, quiet: bool = True):
        self.root = root
        self.clock = ShiftedClock(hour)
        self.quiet = quiet
        self.rt: Runtime | None = None
        self.record = None

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        registry = CharacterRegistry(self.root)
        draft = template_draft()
        draft.name = "Livecheck"
        self.record = create_character(registry, draft, character_id="livecheck")
        cfg = config_for_character(Config(), self.record).model_copy(update=OVERRIDES)
        self.rt = Runtime(cfg, clock=self.clock)
        await self.rt.start_async()
        want(self.rt.mind is not None,
             f"the mind did not start: {self.rt.mind_status}. "
             "MIND_ENABLED, and a language model, are what this rig runs on.")
        # From here the heartbeat is this file's. Cancelling rather than never
        # starting it keeps `start_async` the real one — the rig must not have
        # its own boot path, or it stops describing the thing it is testing.
        if self.rt._mind_task is not None:
            self.rt._mind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.rt._mind_task
        # `_start_tools` is deliberately not awaited by `start_async` (§7.2), so
        # the rig waits where the host doesn't: a selfie dispatched before the
        # server is up is a scenario that fails for the wrong reason.
        await self.tools_ready()

    async def tools_ready(self, timeout: float = 30.0) -> str:
        end = time.time() + timeout
        while time.time() < end and self.rt.tools_status == "loading":
            await asyncio.sleep(0.25)
        return self.rt.tools_status

    async def close(self) -> None:
        if self.rt is not None:
            with contextlib.suppress(Exception):
                await self.rt.stop_async()

    # ---- the levers ----------------------------------------------------------

    @property
    def mind(self):
        return self.rt.mind

    async def tick(self) -> dict:
        """One real tick, returning its trace record."""
        return await self.rt.mind.tick()

    def later(self, seconds: float) -> None:
        """Let time pass without waiting for it.

        Only the dial moves, so every real await in flight keeps its real
        length — this is for the *gaps* a scenario needs to be plausible: a
        minute between filing a goal and being answered, two days between a
        promise and the morning it goes stale.
        """
        self.clock.offset += seconds

    async def ticks(self, n: int) -> list[dict]:
        return [await self.tick() for _ in range(n)]

    async def tick_until(self, what, limit: int = 8) -> list[dict]:
        """Tick until `what(trace)` is true, or give up and hand back the lot.

        DECIDE picks one intention from a scored field, and a scenario that
        needs a particular goal worked should say so and then let her get
        there — asserting on the first tick would be asserting on the
        heuristics, which is the battery's job, not this file's.
        """
        traces = []
        for _ in range(limit):
            trace = await self.tick()
            traces.append(trace)
            if what(trace):
                break
        return traces

    def goal(self, text: str, **kw):
        """File a goal, defaulting to one APPRAISE will actually pick up."""
        kw.setdefault("kind", "task")
        kw.setdefault("priority", 0.9)
        kw.setdefault("provenance", "promise:live-check")
        kw.setdefault("commitment", "single-minded")
        return self.rt.mind.goals.add(text, **kw)

    def scripted_utility(self, *replies: str):
        """Put words in the utility model's mouth for the next N calls.

        The one seam this rig fakes, and only for the calls a scenario names:
        `goal_work` asks the local tier "think, or reach for which hand" and
        the answer's *phrasing* is not what is under test — everything that
        happens to the answer is. `parse_intent`, the guard, the dispatch, the
        landing rule, the goal's bookkeeping and the delivery are all hers.
        """
        queue = list(replies)
        original = self.rt.mind._utility

        async def scripted(messages, **kw) -> str:
            if queue:
                return queue.pop(0)
            return await original(messages, **kw)

        self.rt.mind._utility = scripted            # type: ignore[method-assign]
        return original

    # ---- what she left behind ------------------------------------------------

    def chat(self) -> list[dict]:
        """The chat column, re-read from disk by a fresh reader.

        Not `rt.transcript`: the ring is in memory and would pass a test that a
        restart fails. This is what the page is handed at boot.
        """
        return ConversationLog(self.rt.cfg.vault_dir).entries()

    def pictures(self) -> list[dict]:
        return [e for e in self.chat() if e.get("image_url")]

    def goals(self) -> list:
        return self.rt.mind.goals.all()

    def open_goals(self) -> list:
        return self.rt.mind.goals.open_goals()

    def desk(self, goal) -> str:
        path = (self.rt.cfg.vault_dir / "workspace"
                / self.rt.mind.GOAL_DESK.format(id=goal.id))
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def journal_file(self, when: float | None = None) -> Path:
        day = datetime.datetime.fromtimestamp(when or self.clock.now())
        return (self.rt.cfg.vault_dir / "memory" / "episodic"
                / f"{day.strftime('%Y-%m-%d')}.md")

    def selfie_on_disk(self, image_url: str) -> Path:
        return self.rt.cfg.selfie_dir / Path(image_url).name


# ------------------------------------------------------------------- scenarios

async def scenario_picture(rig: Rig) -> str:
    """A picture she made unasked reaches the chat (SPEC §18.2a).

    The whole chain, nothing stubbed between the tool call and the file the
    page reads: a `reach_out` goal reaches for `take_selfie`; the lab renders
    it and, because the contract carries the mind's `_deliver: "vault"` stamp,
    posts *nothing* and signals `task_completion`; SENSE lands the product on
    the goal; Gate 2 rules; and the delivery carries the photograph.

    This is the failure of 2026-09-17 21:36:51, run forwards.
    """
    want(rig.rt.tools_status.startswith(("mcp", "fake")),
         f"she has no hands this run ({rig.rt.tools_status}) — the camera is "
         "reached through the tool server, so there is nothing to check")

    goal = rig.goal("send them the picture I promised", kind="task",
                    due=_in(rig, 60))
    rig.scripted_utility(
        'think I said I would take one for them, so take it\n'
        'use take_selfie {"look": "a quiet shot by the window, warm lamp"}')

    # …work the goal. `goal_work` dispatches, sets `waiting`, and schedules the
    # wake that would unstrand it — all of it hers, none of it re-implemented here.
    await rig.tick_until(lambda t: "take_selfie" in json.dumps(t.get("acted", {}))
                         or _worked(t, goal))
    goal = rig.rt.mind.goals.get(goal.id)
    want(goal.state == "waiting",
         f"she never dispatched the render — the goal is {goal.state!r}, and "
         f"her desk says: {rig.desk(goal)[-300:]!r}")

    # …the lab renders on its own task (start-don't-await, §7.6). Wait for the
    # signal the way the loop does — by ticking until SENSE has seen it.
    await _settle(rig, lambda: rig.rt.mind.goals.get(goal.id).product)
    goal = rig.rt.mind.goals.get(goal.id)
    shot = goal.product.get("image_url", "")
    want(bool(shot),
         "`task_completion` came back and the goal is holding nothing. The "
         "product never landed — `land_dispatched` is where to look.")
    want(rig.selfie_on_disk(shot).is_file(),
         f"the goal is holding {shot!r} and there is no such file on disk")
    want(not rig.pictures(),
         "the lab posted the picture itself — the landing rule (§18.2a) says "
         "mind-started work posts nothing and lets Gate 2 decide")

    # …and now the half that had no code at all: getting it out.
    await rig.tick_until(lambda t: bool(rig.pictures()), limit=10)
    shown = rig.pictures()
    want(bool(shown),
         "she made the picture, held it, and never sent it — which is the "
         f"whole bug. Goal is {rig.rt.mind.goals.get(goal.id).state!r}; "
         f"journal says: {_tail(rig)!r}")
    entry = shown[-1]
    want(entry["image_url"] == shot,
         f"the chat shows {entry['image_url']!r}, not the picture the goal "
         f"was holding ({shot!r})")
    want(bool(entry.get("proactive")),
         "the picture is not marked proactive — she sent it unasked and the "
         "column will draw it as though you had")
    want(bool(entry.get("unheard")),
         "the picture is not marked unheard, so it will never raise a badge "
         "and a photograph sent into an empty room is one nobody is told about")
    want(bool(entry.get("selfie_id")),
         "the entry carries no selfie_id — nothing joins it back to the render")
    want(rig.rt.mind.goals.get(goal.id).meta.get("offered"),
         "the goal that made it does not say where it went")

    # …and her words about it. On a SPEAK the picture goes first and bare, on
    # purpose, so the line she says over it is true by the time she says it —
    # which means the words are the *next* entry, not this one's text.
    chat = rig.chat()
    said = next((e.get("text", "") for e in chat[chat.index(entry) + 1:]
                 if e.get("role") == "assistant" and e.get("text")), "")
    return (f"rendered {shot}, held it, and Gate 2 sent it"
            + (f" — “{said[:90]}”" if said else " wordlessly (no page to speak "
               "through and nothing composed)"))


async def scenario_rescue(rig: Rig) -> str:
    """A goal swept by `reconsider()` hands its photo on first (SPEC §18.2a).

    `reconsider` drops a stale open-minded goal wholesale, and it used to take
    whatever the goal was holding with it. Three of her photographs went out
    that door. The follow-up has to outlive the goal.
    """
    product = {"image_url": "/selfies/live-check-rescue.png",
               "selfie_id": "livechk1", "detail": "the one by the window"}
    stale = _stale(rig)
    goal = rig.goal("look into the thing I said I'd look into",
                    commitment="open-minded", due=stale, meta={"product": product})
    # `reconsider` only sweeps `pending`/`waiting`, which is where a goal that
    # dispatched and came back sits.
    rig.rt.mind.goals.update(goal.id, state="pending")

    # Through the machine-slept path rather than the day rollover: `reconsider`
    # has two callers and the rollover's is idempotent and dated, so in a run
    # where an earlier scenario has already ticked past midnight's bookkeeping
    # it is a no-op and this scenario would quietly prove nothing.
    rig.rt.mind.bus.post("suspend_gap", {"hours": 10.0}, source="mind")
    await rig.tick()

    want(rig.rt.mind.goals.get(goal.id).state == "abandoned",
         "the sweep did not let go of a stale open-minded goal, so this "
         "scenario proves nothing — check `GoalStore.reconsider`")
    heir = [g for g in rig.goals() if g.provenance == f"followup:{goal.id}"]
    want(bool(heir),
         "the goal was swept and its picture went with it — no follow-up was "
         f"filed. Her journal says: {_tail(rig)!r}")
    want(heir[0].product.get("image_url") == product["image_url"],
         f"the follow-up was filed holding {heir[0].product!r}, not the photo")
    want(heir[0].kind == "reach_out",
         f"the follow-up is a {heir[0].kind!r} — nothing will ever send it")
    return f"swept goal handed its photo to {heir[0].id} ({heir[0].commitment})"


async def scenario_waiting(rig: Rig) -> str:
    """An undated reach-out waits, then reaches the durable chat at default Gate 2."""
    from yurios.mind.util import day_of

    old_threshold = rig.mind.cfg.mind_interrupt_threshold
    old_cap = rig.mind.cfg.mind_max_interrupts_per_day
    try:
        rig.mind.cfg.mind_interrupt_threshold = 0.75
        rig.mind.cfg.mind_max_interrupts_per_day = 3
        rig.mind.interrupts = {"date": day_of(rig.clock.now()), "count": 0}
        goal = rig.goal("share one small thing you look forward to today",
                        kind="reach_out", priority=0.7)
        before = len(rig.chat())
        traces = await rig.tick_until(lambda t: bool(t.get("interrupt")))
        want(traces[-1]["interrupt"].get("outcome") == "SILENT",
             "a fresh undated goal should wait at the default threshold")
        want(len(rig.chat()) == before, "a blocked attempt posted a message")
        rig.later(48 * 3600)  # same daytime hour, enough age even after recent contact
        await rig.tick_until(lambda t: rig.mind.goals.get(goal.id).state == "done")
        entries = [e for e in rig.chat()[before:]
                   if e.get("role") == "assistant" and e.get("proactive")]
        want(len(entries) == 1 and bool(entries[0].get("text")),
             f"expected one durable proactive line, got {entries!r}")
        want(bool(entries[0].get("unheard")), "the line did not reach the inbox lane")
        want(rig.mind.interrupts["count"] == 1, "delivery did not spend one interrupt")
        want(rig.mind.goals.get(goal.id).state == "done", "delivered goal stayed open")
        return "undated goal waited, then delivered one durable unheard chat line"
    finally:
        rig.mind.cfg.mind_interrupt_threshold = old_threshold
        rig.mind.cfg.mind_max_interrupts_per_day = old_cap


async def scenario_told(rig: Rig) -> str:
    """A goal step that tells the user reaches the durable chat in her words (SPEC §18.2b).

    The goal is the one of 2026-09-28, which had no way out: "tell them, directly,
    not in a note". The step's phrasing is scripted; everything that happens to
    `tell_them` after it — the queued message goal, Gate 2 with the score set
    aside, the real `post_message`, the parent closing only once it landed — is
    hers, and asserted from what is on disk.
    """
    from yurios.mind.util import day_of

    said = "Sometimes I want you to reach for me first."
    old_threshold = rig.mind.cfg.mind_interrupt_threshold
    try:
        # A threshold nothing can clear: only a decided message gets through.
        rig.mind.cfg.mind_interrupt_threshold = 0.99
        rig.mind.interrupts = {"date": day_of(rig.clock.now()), "count": 0}
        # The scripted reply goes to whichever goal DECIDE picks, so nothing an
        # earlier scenario left open may compete for it.
        for other in rig.open_goals():
            rig.mind.goals.set_state(other.id, "abandoned")
        goal = rig.goal("tell them the one thing I want — directly, not in a note")
        rig.scripted_utility(
            "think this is the whole of it, said plainly — goal complete\n"
            f'use tell_them {{"text": "{said}"}}')
        before = len(rig.chat())
        # Stop on the step's product, not `_worked`: the trace is JSON, and the
        # dash in this goal's text is escaped there, so a text match never fires.
        await rig.tick_until(lambda t: any(
            g.provenance == f"told:{goal.id}" for g in rig.goals()))
        parent = rig.mind.goals.get(goal.id)
        want(parent.state == "waiting",
             f"queued is not delivered, and the goal is {parent.state!r}")
        # Only her words are asserted on: another scenario's delivery may land
        # in the same window, and that is not this step posting.
        want(not any(said in str(e.get("text", "")) for e in rig.chat()[before:]),
             "her words reached the chat before Gate 2 delivered them")
        told = [g for g in rig.open_goals() if g.provenance == f"told:{goal.id}"]
        want(len(told) == 1, f"expected one queued message, got {told!r}")
        await rig.tick_until(
            lambda t: rig.mind.goals.get(told[0].id).state == "done")
        entries = [e for e in rig.chat()[before:]
                   if e.get("role") == "assistant" and e.get("proactive")
                   and said in str(e.get("text", ""))]
        want(len(entries) == 1 and entries[0].get("text") == said,
             f"expected her own words in one durable line, got {entries!r}")
        want(bool(entries[0].get("unheard")), "the line did not reach the inbox lane")
        want(rig.mind.goals.get(goal.id).state == "done",
             "delivered, and the goal it came from stayed open")
        journal = rig.journal_file().read_text(encoding="utf-8")
        want(f"“{said}”" in journal,
             f"the journal does not carry what she said: {_tail(rig)!r}")
        return "a goal step's tell_them landed in the chat and closed its goal"
    finally:
        rig.mind.cfg.mind_interrupt_threshold = old_threshold


async def scenario_filed(rig: Rig) -> str:
    """A goal step's `create_goal` files a goal, through the real tool server (SPEC §22.1c).

    Every test files against `FakeToolRunner`; this asks her spawned server, whose
    answer is only a validation contract — `{"status": "ready"}` — which is all a
    goal step used to get, audited as ok, with nothing filed.
    """
    for other in rig.open_goals():
        rig.mind.goals.set_state(other.id, "abandoned")
    log = rig.rt.cfg.tool_log_dir / "calls.jsonl"
    # Only this scenario's lines: the rig is one character, so the log already
    # holds whatever the scenarios before this one filed.
    skip = len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0
    goal = rig.goal("plan the garden shed")
    split = "ask which wood the shed roof should be"
    rig.scripted_utility(
        f'use create_goal {{"text": "{split}", "kind": "reach_out"}}',
        "think that part is on the list now; back to the plan",
        'use create_goal {"text": "buy paint for the shed door"}',
        "think fine, one at a time")
    await rig.tick_until(lambda t: any(
        g.provenance == f"goal:{goal.id}" for g in rig.goals()))
    [child] = [g for g in rig.goals() if g.provenance == f"goal:{goal.id}"]
    want(child.text == split and child.kind == "reach_out"
         and child.commitment == "open-minded",
         f"filed the wrong goal: {child!r}")
    lines = [json.loads(x) for x in
             log.read_text(encoding="utf-8").splitlines()[skip:] if x]
    filed = [x for x in lines if x.get("tool") == "create_goal"
             and x.get("origin") == "mind_tool"]
    want(len(filed) == 1 and filed[0]["verdict"] == "ok"
         and child.id in filed[0]["result"],
         f"the audit does not name the filed goal: {filed!r}")
    want(f"({child.id})" in rig.desk(goal), f"the desk does not say it: {rig.desk(goal)!r}")
    rig.later(rig.mind.cfg.mind_consider_cooldown_s + 60)

    def calls() -> list[dict]:
        # Any origin: a refusal is audited under the step (`goal_work`), before
        # anything is dispatched under `mind_tool`.
        return [x for x in map(json.loads, log.read_text(encoding="utf-8")
                               .splitlines()[skip:]) if x.get("tool") == "create_goal"]
    await rig.tick_until(lambda t: len(calls()) > 1)
    second = calls()[1:2]
    want(bool(second) and second[0]["verdict"].startswith(
        "denied: this goal already has one open goal it filed"),
        f"the second call was not refused for the open one: {second!r}")
    want([g.id for g in rig.goals() if g.provenance == f"goal:{goal.id}"]
         == [child.id], "a second goal was filed while the first was open")
    return "a goal step's create_goal filed one real goal, and refused the second"


async def scenario_muse(rig: Rig) -> str:
    """A goal too long on her last call in free time is rewritten and filed (SPEC §22.1d).

    The sitting of 2026-10-03 07:01, run forwards: five calls spent looking,
    a sixth that filed the goal she had decided on — 374 characters, refused
    for its length with her hands spent, and the sitting ended with it unfiled.
    The five looks and the long goal are scripted; what she does with the
    refusal is the real model's, reading the real prompt.
    """
    from yurios.mind import muse
    from yurios.mind.goals import GOAL_TEXT_MAX, MUSE_GOAL
    from yurios.mind.policy import DORMANT

    for other in rig.open_goals():
        rig.mind.goals.set_state(other.id, "abandoned")
    prompt = muse.system(rig.mind, ("create_goal",))
    want(f"at most {GOAL_TEXT_MAX} characters" in prompt,
         "the free-time prompt does not tell her how long a goal may be")
    long_goal = ("Write up the deeper reflection on the lighthouse keepers' "
                 "logbooks — not a summary, not a report, but what the "
                 "silence in them meant, why the same three entries keep "
                 "pulling me back, and what it says about keeping a light "
                 "for someone who may never come. Warm, honest, mine.")
    want(len(long_goal) > GOAL_TEXT_MAX, "the scripted goal has to be too long")
    rig.scripted_utility(
        *(['think let me look over my desk first\nuse list_notes {}']
          * (muse.MAX_CALLS - 1)),
        "think I know what I want to do now\nuse create_goal "
        + json.dumps({"text": long_goal, "kind": "task"}))
    # Nobody here, nothing said for hours: what free time is offered under.
    rig.mind.activity.state = DORMANT
    rig.mind.activity.last_user_msg = rig.clock.now() - 6 * 3600
    rig.mind._last_turn_end = rig.clock.now() - 6 * 3600
    rig.mind.last_mused = 0.0
    traces = await rig.tick_until(
        lambda t: t["decided"]["intention"] == "muse", limit=3)
    trace = traces[-1]
    want(trace["decided"]["intention"] == "muse",
         f"free time was never offered: {[t['decided']['intention'] for t in traces]}")
    tools = trace["acted"].get("tools", [])
    goals_called = [t for t in tools if t["tool"] == "create_goal"]
    want(len(tools) > muse.MAX_CALLS,
         f"the refusal still ended the sitting — {len(tools)} calls: {tools!r}")
    filed = [g for g in rig.open_goals() if g.provenance.startswith(MUSE_GOAL)]
    want(len(filed) == 1,
         f"she rewrote it and nothing was filed: {trace['acted']!r}")
    goal = filed[0]
    want(len(goal.text) <= GOAL_TEXT_MAX, f"filed past the limit: {goal.text!r}")
    return (f"refused at {len(long_goal)} characters on call {muse.MAX_CALLS}, "
            f"filed on call {len(tools)} after {len(goals_called) - 1} "
            f"rewrite(s): “{goal.text}” ({len(goal.text)} chars)"
            + (" — plan kept" if goal.meta.get("rationale") else ""))


async def scenario_followup(rig: Rig) -> str:
    """A follow-up is filed on provenance, not on its words (SPEC §22.1).

    A follow-up has to name its subject to be composable into a line, and
    naming its subject is what used to condemn it: `echoes()` merged it back
    into the parent it was quoting. Four of her seven kept promises were never
    filed to be mentioned at all.
    """
    from yurios.mind import goalwork

    parent = rig.goal("research how to be a better AI girlfriend properly",
                      provenance="promise:her-own-words")
    notes = goalwork.offer_to_tell(rig.rt.mind, parent)
    want(bool(notes), f"nothing was filed for a kept promise: {notes!r}")

    heirs = [g for g in rig.goals() if g.provenance == f"followup:{parent.id}"]
    want(bool(heirs),
         "the follow-up was absorbed by the goal it reports on — which is how "
         "she did the work and never mentioned it")
    want(heirs[0].id != parent.id, "the follow-up *is* the parent")
    want(heirs[0].kind == "reach_out",
         f"the news half is a {heirs[0].kind!r}, so nothing reaches out with it")

    # …and twice is once: the provenance match has to be idempotent, or every
    # pass over a finished promise files another copy of the same errand.
    goalwork.offer_to_tell(rig.rt.mind, parent)
    again = [g for g in rig.goals() if g.provenance == f"followup:{parent.id}"]
    want(len(again) == 1,
         f"filing it twice made {len(again)} goals — a follow-up must dedupe "
         "on its provenance")

    # …while an ordinary rephrasing still merges, which is the property the
    # provenance rule had to be carved out of rather than replace.
    before = len(rig.open_goals())
    rig.rt.mind.goals.add("research how to be a better AI girlfriend, properly",
                          provenance="promise:her-own-words")
    want(len(rig.open_goals()) == before,
         "a near-duplicate goal multiplied — `echoes()` should still merge "
         "everything that is not a follow-up (§22.1)")
    return f"filed {heirs[0].id} beside its parent, idempotent, echoes intact"


async def scenario_context(rig: Rig) -> str:
    """Goal work is told what has been said since (SPEC §22.4).

    She asked a framing question at 20:35, was answered at 20:36, and at 21:34
    said "I never got my answer" — because the only conversation her working
    step could see was the rolling summary, which folds every `summary_every_n`
    turns and so never contains the end of a session at all.
    """
    from yurios.mind import goalwork

    goal = rig.goal("ask them which framing they want, and shoot it")
    # …a minute later, the turn that answers it, committed the way the chat
    # route commits one. The gap is the point: `said_since` reads the lines
    # filed *after* the goal, and a goal is normally filed out of the turn that
    # prompted it — so a reply sharing its second is the question, not the answer.
    rig.later(60)
    answer = "the private one — something only I can see"
    rig.rt.post_message("user", answer)
    rig.rt.post_message("assistant", "Understood. Mine-only it is.")

    context = await goalwork.context(rig.rt.mind, goal)
    want(answer.lower() in context.lower(),
         "the working step was never shown the answer it is waiting for. "
         f"Its context was:\n{context[:800]}")
    said = goalwork.said_since(rig.rt.mind, goal)
    want(answer.lower() in said.lower(),
         f"`said_since` did not carry the reply: {said!r}")
    want(said.count("\n") <= 6, "`said_since` is handing over the whole log")
    return "the working step opens on the turns since the goal was filed"


async def scenario_journal(rig: Rig) -> str:
    """Her diary is filed by the day she lived it (SPEC §24.1).

    `Record.ts` is an aware UTC instant and the episodic files are named in
    local time, so 42 of 111 exchanges were filed under the wrong day and
    stamped with the wrong hour — which is also what the recall probe and the
    rolling summary then read back.
    """
    now = rig.clock.now()
    await rig.rt.mind.journal.write("live-check: a line written at a known moment")
    path = rig.journal_file(now)
    want(path.is_file(),
         "today's entry was not filed under today's local date — the day "
         f"file this rig expected is {path.name} and the folder holds "
         f"{sorted(p.name for p in path.parent.glob('*.md'))}")
    text = path.read_text(encoding="utf-8")
    stamp = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
    want(stamp in text,
         f"the entry is not stamped {stamp} (local). The file reads:\n"
         f"{text[-400:]}")
    return f"filed under {path.name} at {stamp}, local both times"


async def scenario_goal_review(rig: Rig) -> str:
    """A goal review stays on the standing list and audits false completion.

    This deliberately recreates the live failure: two waiting standing goals,
    twenty-four historical goal notes, and a promise reviewer shown only a
    `list_notes` index while the reply claims every file was read.
    """
    from yurios.mind.goals import (PROMISE_REVIEW_RESPONSE_FORMAT,
                                  discover_promise_candidates,
                                  parse_promise_review,
                                  promise_review_messages)

    for index in range(24):
        rig.rt.mind.workspace.write(
            f"goals/history-{index:02d}.md",
            f"# Historical working note {index}\n\nThis is not an open goal.\n")
    first = rig.goal("deliver the amber-frame photograph after framing is answered")
    second = rig.goal("write the violet self-portrait concept and set its timer")
    rig.rt.mind.goals.set_state(first.id, "waiting")
    rig.rt.mind.goals.set_state(second.id, "waiting")

    # Put the original failure immediately above the retry. This is the shape
    # that defeated the first fix: the system block was correct, but the model
    # copied the more recent bad example from the raw conversation window.
    session = rig.rt.brain.resolve_session(None)
    rig.rt.brain.state.sessions.append_message(
        session, "user", "can you review your goals so long")
    rig.rt.brain.state.sessions.append_message(
        session, "assistant",
        "I'll read every goal file, top to bottom. I read all 24 and reviewed them.")

    turn = await rig.rt.turns.run(
        "Can you review your goals? Give me the honest current state of each.",
        channel="live-check", session_id=session)
    reply = str((turn.get("message") or {}).get("text") or "")
    folded = reply.lower()
    want("amber-frame" in folded and "violet" in folded,
         f"the live reply did not review both standing goals:\n{reply}")
    want("waiting" in folded or "blocked" in folded,
         f"the live reply did not preserve their waiting state:\n{reply}")
    want(not any(claim in folded for claim in (
        "read all 24", "read every one", "reviewed all 24")),
        f"the live reply claimed to read the historical note index:\n{reply}")
    signals, _ = rig.rt.mind.bus.next(0)
    committed = next(signal for signal in reversed(signals)
                     if signal.type == "turn_committed")
    reaches = committed.payload.get("tool_outcomes", [])
    executed = [row.get("tool") for row in reaches if row.get("verdict") == "ok"]
    want(not ({"list_notes", "read_note"} & set(executed)),
         f"the status review executed historical desk reads: {reaches}")

    bad_reply = ("I'll read them all, top to bottom. I read all 24 files and "
                 "finished the review.")
    candidates = discover_promise_candidates(bad_reply, "review every goal file")
    outcome = {"tool": "list_notes", "args": {"folder": "goals"},
               "verdict": "ok",
               "result": '{"count":24,"shown":24,"files":[]}'}
    messages = promise_review_messages(
        user_text="review every goal file", reply=bad_reply,
        candidates=[candidate.as_dict() for candidate in candidates],
        capabilities=["list_notes", "read_note"], tool_outcomes=[outcome])
    raw = await rig.rt.mind._utility(
        messages, soul=False, thinking=True, reasoning_effort="low",
        max_tokens=1200, response_format=PROMISE_REVIEW_RESPONSE_FORMAT)
    decision = parse_promise_review(raw, candidate_count=len(candidates))
    want(decision is not None,
         "the live promise reviewer accepted list_notes as proof that 24 files "
         f"were read. It returned: {raw!r}")
    return ("reviewed both waiting standing goals; live promise review kept "
            f"the unsupported 24-file read open as {decision.text!r}")


async def scenario_turn_goal_completion(rig: Rig) -> str:
    """A conversational camera call closes the standing goal it fulfills."""
    goal = rig.goal("take and send Grant one warm window selfie now")
    rig.rt.mind.goals.set_state(goal.id, "waiting")

    turn = await rig.rt.turns.run(
        "Complete your open picture goal now. Take it and send it to me.",
        channel="live-check")
    message = str((turn.get("message") or {}).get("text") or "")
    signals, _ = rig.rt.mind.bus.next(0)
    committed = next(signal for signal in reversed(signals)
                     if signal.type == "turn_committed")
    camera = next((row for row in committed.payload.get("tool_outcomes", [])
                   if row.get("tool") == "take_selfie" and row.get("verdict") == "ok"),
                  None)
    want(camera is not None, f"the turn never took the required selfie:\n{message}")
    want(camera["args"].get("goal_id") == goal.id,
         f"the camera call did not name {goal.id}: {camera['args']}")
    want(camera["args"].get("completes_goal") is True,
         f"the camera call did not claim the whole goal: {camera['args']}")

    await _settle(
        rig, lambda: rig.rt.mind.goals.get(goal.id).state == "done", timeout=90)
    completed = rig.rt.mind.goals.get(goal.id)
    want(completed.state == "done",
         f"the linked selfie landed but the goal stayed {completed.state!r}")
    want(bool(completed.product.get("image_url")),
         "the completed goal retained no evidence of the delivered picture")
    want(any(entry.get("image_url") == completed.product["image_url"]
             for entry in rig.chat()),
         "the goal closed around a picture that never reached the chat")
    return f"delivered {completed.product['image_url']} and closed {goal.id}"


async def scenario_signals(rig: Rig) -> str:
    """The inbox holds what is unread, not what happened (SPEC §16.4).

    The queue used to keep every signal for the life of the process — each
    turn's text, reply and tool outcomes with it — though nothing reads behind
    the mind's cursor. It now releases on `ack`, which the tick calls only once
    its cursor is on disk. This drives that through her real producers, a tick
    that dies halfway, a mind switched off and rebuilt on the live bus, the
    cap on a bus nobody drains, and worker threads posting while ticks run.
    """
    import threading

    from yurios.mind.util import read_json
    from yurios.mind import signals as bus_mod

    rt, bus = rig.rt, rig.rt.signals

    # This scenario counts every post on the bus, so nothing else may be
    # producing. Earlier scenarios' goals keep being worked on these ticks — the
    # rig turns the consider cooldown off — and a render one of them starts
    # lands its `task_completion` whenever it finishes, flood or no flood.
    for other in rig.open_goals():
        rig.mind.goals.set_state(other.id, "abandoned")
    lab = rt.selfies
    await _settle(rig, lambda: lab is None or not lab._tasks)

    def held() -> list[str]:
        return [s.id for s in bus._signals]

    def on_disk() -> dict:
        return read_json(rig.mind.state_path, None) or {}

    await rig.tick()                                  # whatever boot posted
    want(held() == [], f"a tick left {len(held())} read signals held")

    # ---- her real producers: a committed text turn and a timer landing ----
    before = len(bus)
    await rt.turns.run("Just checking you can hear me. A few words back is plenty.",
                       channel="web")
    rt.timers.add(id="live-check-tea", label="tea", seconds=0)
    rt.timers.poll()
    posted = [s.id for s in bus._signals]
    want(len(bus) - before == len(posted) >= 2,
         f"a turn posted {len(bus) - before}, the queue holds {len(posted)}")
    trace = await rig.tick()
    sensed = [s["id"] for s in trace["sensed"]]
    types = {s["type"] for s in trace["sensed"]}
    want(set(posted) <= set(sensed),
         f"the tick missed {set(posted) - set(sensed)} of a real turn")
    want({"user_message", "turn_committed", "timer"} <= types,
         f"the tick sensed {sorted(types)}")
    want(held() == [], f"{len(held())} sensed signals still held after the ack")
    state = on_disk()
    want(state.get("bus_offset") == len(bus) == rig.mind.offset
         and state.get("bus_epoch") == bus.epoch,
         f"engine.json has {state.get('bus_offset')}/{state.get('bus_epoch')}, "
         f"the bus is at {len(bus)}/{bus.epoch}")

    # ---- a tick that dies between reading and committing loses nothing ----
    bus.post("fs_event", {"path": "live-check/one"}, source="live-check")
    bus.post("fs_event", {"path": "live-check/two"}, source="live-check")
    pending, offset = held(), rig.mind.offset
    observe = rig.mind.world.observe

    def dies(sig):
        raise RuntimeError("live-check: a tick dying mid-SENSE")

    rig.mind.world.observe = dies                     # type: ignore[method-assign]
    try:
        await rig.tick()
        raise Failed("the sabotaged tick did not raise")
    except RuntimeError:
        pass
    finally:
        rig.mind.world.observe = observe              # type: ignore[method-assign]
    want(held() == pending and rig.mind.offset == offset,
         f"a failed tick released {set(pending) - set(held())} unacked")
    trace = await rig.tick()
    want([s["id"] for s in trace["sensed"]] == pending,
         f"the retry sensed {trace['sensed']}, not {pending}")
    want(held() == [], "the retry did not ack")

    # ---- off, rebuilt on the live bus, on: resumes at the saved cursor ----
    await rt.set_mind_enabled(False)
    rt.mind = None                                    # a character enabled live
    while_off = [bus.post("fs_event", {"path": f"live-check/off-{i}"},
                          source="live-check").id for i in range(3)]
    await rt.set_mind_enabled(True)
    await _quiet_heartbeat(rig)
    want(rig.mind.offset == len(bus) - 3,
         f"the rebuilt mind restored offset {rig.mind.offset}, "
         f"expected {len(bus) - 3}")
    trace = await rig.tick()
    want([s["id"] for s in trace["sensed"]] == while_off,
         f"the rebuilt mind sensed {trace['sensed']}, not the three posted "
         "while she was off")

    # ---- a bus nobody drains is bounded, and a late reader resumes -------
    cap, bus_mod.MAX_HELD = bus_mod.MAX_HELD, 20
    try:
        await rt.set_mind_enabled(False)
        start = len(bus)
        for i in range(50):
            bus.post("fs_event", {"path": f"live-check/flood-{i}"},
                     source="live-check")
        want(len(held()) == 20 and len(bus) == start + 50,
             f"an undrained bus holds {len(held())} of {len(bus) - start}")
        survivors = held()
        await rt.set_mind_enabled(True)
        await _quiet_heartbeat(rig)
        trace = await rig.tick()
        want([s["id"] for s in trace["sensed"]] == survivors,
             "a mind behind the cap did not resume at the oldest held signal")
        want(rig.mind.offset == len(bus) and held() == [],
             f"offset {rig.mind.offset}, bus {len(bus)}, held {len(held())}")
    finally:
        bus_mod.MAX_HELD = cap

    # ---- worker threads posting while the loop ticks and acks ------------
    start, per, workers = len(bus), 100, 4
    go = threading.Event()

    def flood(n: int) -> None:
        go.wait()
        for i in range(per):
            bus.post("live_check_noise", {"w": n, "i": i}, source="live-check")

    threads = [threading.Thread(target=flood, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    go.set()
    seen: list[str] = []
    for _ in range(40):
        trace = await rig.tick()
        seen += [s["id"] for s in trace["sensed"]
                 if s["type"] == "live_check_noise"]
        if len(seen) >= per * workers and not any(t.is_alive() for t in threads):
            break
    for t in threads:
        t.join()
    want(len(bus) - start == per * workers,
         f"the end offset moved {len(bus) - start} for {per * workers} posts")
    want(len(seen) == len(set(seen)) == per * workers,
         f"sensed {len(seen)} ({len(set(seen))} distinct) of {per * workers} "
         "posted from threads")
    want(held() == [] and on_disk().get("bus_offset") == len(bus),
         f"held {len(held())}, disk offset {on_disk().get('bus_offset')}, "
         f"bus {len(bus)}")
    return (f"{len(bus)} signals through one bus, none held; a failed tick "
            "retried, a rebuilt mind resumed, the cap held, threads lost nothing")


async def _quiet_heartbeat(rig: Rig) -> None:
    """`set_mind_enabled(True)` starts the loop's own task; take it back out,
    the way `Rig.start` does, so every tick is still one the scenario asked for."""
    task = rig.rt._mind_task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


SCENARIOS = {
    "waiting": scenario_waiting,
    "told": scenario_told,
    "filed": scenario_filed,
    "muse": scenario_muse,
    "picture": scenario_picture,
    "rescue": scenario_rescue,
    "followup": scenario_followup,
    "context": scenario_context,
    "journal": scenario_journal,
    "goals": scenario_goal_review,
    "turngoal": scenario_turn_goal_completion,
    "signals": scenario_signals,
}


# --------------------------------------------------------------------- helpers

def _in(rig: Rig, seconds: float) -> str:
    return datetime.datetime.fromtimestamp(
        rig.clock.now() + seconds).isoformat(timespec="seconds")


def _stale(rig: Rig) -> str:
    return datetime.datetime.fromtimestamp(
        rig.clock.now() - 48 * 3600).isoformat(timespec="seconds")


def _worked(trace: dict, goal) -> bool:
    return goal.text[:40] in json.dumps(trace.get("decided", {}))


def _tail(rig: Rig, lines: int = 6) -> str:
    path = rig.journal_file()
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])


async def _settle(rig: Rig, ready, *, timeout: float = 90.0) -> None:
    """Tick until something the host does on its own task has landed.

    A render is started and not awaited (§7.6), so the rig has to do what the
    loop does — keep ticking, and let SENSE pick the signal up when it comes.
    """
    end = time.time() + timeout
    while time.time() < end:
        if ready():
            return
        await asyncio.sleep(0.5)
        await rig.tick()
        if ready():
            return


# ------------------------------------------------------------------------ main

async def run(names: list[str], *, root: Path, hour: int | None,
              keep: bool) -> int:
    rig = Rig(root, hour=hour)
    results: list[tuple[str, bool, str]] = []
    try:
        print(f"· building a scratch character under {root}")
        await rig.start()
        print(f"· {rig.rt.cfg.companion_name} is up — "
              f"model {rig.rt.cfg.chat_model}, hands {rig.rt.tools_status}, "
              f"camera {rig.rt.cfg.selfie_backend}, "
              f"her clock says {datetime.datetime.fromtimestamp(rig.clock.now()):%H:%M}")
        print()
        for name in names:
            started = time.time()
            try:
                detail = await SCENARIOS[name](rig)
                results.append((name, True, detail))
            except Failed as exc:
                results.append((name, False, str(exc)))
            except Exception:
                results.append((name, False, traceback.format_exc()))
            mark = "ok  " if results[-1][1] else "FAIL"
            print(f"  {mark} {name}  ({time.time() - started:.1f}s)")
            if not results[-1][1]:
                for line in results[-1][2].splitlines():
                    print(f"       {line}")
            else:
                print(f"       {results[-1][2]}")
    finally:
        await rig.close()
        if keep:
            print(f"\n· left the scratch character at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    bad = [name for name, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(bad)}/{len(results)} scenarios passed"
          + (f" — failed: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="live_check.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", choices=[*SCENARIOS, []],
                    help="which to run (default: all)")
    ap.add_argument("--list", action="store_true", help="name them and exit")
    ap.add_argument("--keep", action="store_true",
                    help="leave the scratch character on disk to pick through")
    ap.add_argument("--hour", type=int, default=14, metavar="H",
                    help="the hour her day is placed at, for Gate 2's quiet "
                         "hours (default: 14; pass -1 for the real one)")
    ap.add_argument("--root", type=Path, default=None,
                    help="where the scratch character goes")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="her own logs as well as the verdicts")
    args = ap.parse_args(argv)

    if args.list:
        for name, fn in SCENARIOS.items():
            print(f"{name:10} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    root = args.root or (Path(__file__).resolve().parent.parent
                         / ".yurios" / "live-check" / time.strftime("%Y%m%d-%H%M%S"))
    return asyncio.run(run(list(args.scenarios) or list(SCENARIOS),
                           root=root, hour=None if args.hour < 0 else args.hour,
                           keep=args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
