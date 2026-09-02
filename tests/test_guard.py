"""Guardrails (SPEC §7.3) — allowlist, token-bucket rates, truncation, audit."""
from __future__ import annotations

import json

from yurios.kernel import correlate
from yurios.world.tools.guard import RESULT_LIMITS, RESULT_MAX_CHARS, Guard


def test_allowlist_denies_tools_she_does_not_have(guard):
    ok, reason = guard.check("rm_rf")
    assert not ok and "not a tool" in reason


def test_rate_limit_token_bucket_refills_on_the_injected_clock(clock, cfg):
    guard = Guard(rates_per_min={"set_timer": 6}, log_dir=cfg.tool_log_dir,
                  clock=clock)
    # the bucket starts full: 6 calls pass, the 7th is denied
    for _ in range(6):
        ok, _ = guard.check("set_timer")
        assert ok
    ok, reason = guard.check("set_timer")
    assert not ok and reason == "rate limit"
    # 10 s at 6/min refills one token — exactly one more call passes
    clock.advance(10.0)
    assert guard.check("set_timer")[0]
    assert not guard.check("set_timer")[0]


def test_same_call_twice_in_one_turn_is_denied(guard):
    """The selfie bug: `status: started` carries no photo, so she re-emits the
    marker she already spent and the chat gets two of everything."""
    turn = guard.turn()
    args = {"scene": "window", "wardrobe": "cozy"}
    assert guard.check("set_timer", args, turn=turn) == (True, "")
    ok, reason = guard.check("set_timer", dict(args), turn=turn)
    assert not ok and reason == "already done this turn"


def test_dedupe_is_exact_and_scoped_to_the_one_turn(guard):
    """Different arguments are a different call; a later turn is a clean slate."""
    first = guard.turn()
    assert guard.check("set_timer", {"minutes": 10}, turn=first)[0]
    assert guard.check("set_timer", {"minutes": 5}, turn=first)[0]
    assert guard.check("set_timer", {"minutes": 10}, turn=guard.turn())[0]


def test_a_denied_duplicate_costs_no_rate_budget(clock, cfg):
    guard = Guard(rates_per_min={"set_timer": 2}, log_dir=cfg.tool_log_dir,
                  clock=clock)
    turn = guard.turn()
    assert guard.check("set_timer", {"minutes": 10}, turn=turn)[0]
    assert not guard.check("set_timer", {"minutes": 10}, turn=turn)[0]
    # the repeat spent nothing: the second *distinct* call still has a token
    assert guard.check("set_timer", {"minutes": 5}, turn=turn)[0]


def test_check_without_a_turn_keeps_the_old_two_rule_behaviour(guard):
    assert guard.check("set_timer", {"minutes": 10})[0]
    assert guard.check("set_timer", {"minutes": 10})[0]


def test_truncate_caps_result_length():
    long = "x" * (RESULT_MAX_CHARS * 2)
    out = Guard.truncate(long)
    assert len(out) == RESULT_MAX_CHARS and out.endswith("…")
    assert Guard.truncate("short") == "short"


def test_catalog_tools_keep_a_higher_truncate_bound():
    """list_notes / read_note are the result, not a fact to speak to."""
    long = "x" * 8_000
    assert len(Guard.truncate(long, tool="list_notes")) == RESULT_LIMITS["list_notes"]
    assert len(Guard.truncate(long, tool="read_note")) == RESULT_LIMITS["read_note"]
    assert len(Guard.truncate(long, tool="set_timer")) == RESULT_MAX_CHARS


def test_audit_writes_one_jsonl_line_per_call_allowed_or_denied(guard, cfg):
    guard.audit("set_timer", {"minutes": 10}, "ok", 12.3, '{"seconds": 600}')
    guard.audit("rm_rf", {}, "denied: not a tool she has", 0.0, "")
    lines = [json.loads(l) for l in
             (cfg.tool_log_dir / "calls.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "set_timer" and lines[0]["verdict"] == "ok"
    assert lines[0]["duration_ms"] == 12.3
    assert lines[1]["verdict"].startswith("denied")


# --- who asked (world/correlate.py) -------------------------------------------

def audit_lines(cfg):
    return [json.loads(l) for l in
            (cfg.tool_log_dir / "calls.jsonl").read_text().splitlines()]


def test_a_call_made_inside_a_turn_carries_the_turn(guard, cfg):
    with correlate.scope(kind=correlate.CHAT_TURN, session_id="s-1",
                         turn_index=7) as origin:
        guard.audit("set_timer", {"minutes": 10}, "ok", 1.0, "{}")
    line = audit_lines(cfg)[0]
    assert line["corr_id"] == origin.corr_id
    assert (line["origin"], line["session_id"], line["turn_index"]) \
        == ("chat_turn", "s-1", 7)


def test_a_call_the_mind_made_for_itself_still_writes_a_whole_line(guard, cfg):
    """No turn is in scope for most of what she does. That is the ordinary
    case, so it must produce the same shape — nulls, not missing keys."""
    guard.audit("list_notes", {}, "ok", 1.0, "{}")
    line = audit_lines(cfg)[0]
    assert line["origin"] == "host"
    assert all(line[k] is None for k in
               ("corr_id", "session_id", "turn_index", "tick_id"))


def test_a_tick_stamps_every_call_it_causes(guard, cfg):
    with correlate.scope(kind=correlate.TICK, tick_id="t-abc") as tick:
        with correlate.scope(kind=correlate.DREAM):     # nested: refines, not restarts
            guard.audit("list_notes", {}, "ok", 1.0, "{}")
    line = audit_lines(cfg)[0]
    assert line["tick_id"] == "t-abc"
    assert line["origin"] == "dream"
    assert line["corr_id"] == tick.corr_id, "one unit of work, one join key"


def test_every_call_gets_its_own_id(guard, cfg):
    guard.audit("set_timer", {}, "ok", 1.0, "{}")
    guard.audit("set_timer", {}, "ok", 1.0, "{}")
    ids = [l["call_id"] for l in audit_lines(cfg)]
    assert len(set(ids)) == 2 and all(i.startswith("call-") for i in ids)


def test_the_scope_does_not_leak_out_of_the_turn(guard, cfg):
    with correlate.scope(kind=correlate.CHAT_TURN):
        guard.audit("set_timer", {}, "ok", 1.0, "{}")
    guard.audit("set_timer", {}, "ok", 1.0, "{}")
    inside, outside = audit_lines(cfg)
    assert inside["corr_id"] is not None
    assert outside["corr_id"] is None


def test_the_audit_rotates_so_it_cannot_grow_forever(clock, cfg):
    """calls.jsonl had no rotation at all — an always-on mind writes to it for
    as long as she is alive."""
    guard = Guard(rates_per_min={"set_timer": 6}, log_dir=cfg.tool_log_dir,
                  clock=clock, max_bytes=2_000)
    for _ in range(60):
        guard.audit("set_timer", {"pad": "x" * 100}, "ok", 1.0, "{}")
    live = cfg.tool_log_dir / "calls.jsonl"
    rolled = cfg.tool_log_dir / "calls.jsonl.1"
    assert rolled.is_file()
    # `live` may not exist at all: a roll renames it away and the next append
    # recreates it. What must hold is that the pair stays bounded — one
    # generation each side of the cap, never sixty calls' worth of history.
    footprint = sum(p.stat().st_size for p in (live, rolled) if p.exists())
    assert footprint < 2 * 2_000


def test_an_unwritable_log_never_breaks_the_turn_it_observes(guard, cfg, monkeypatch):
    monkeypatch.setattr("yurios.world.tools.guard.jsonl_append",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    guard.audit("set_timer", {}, "ok", 1.0, "{}")      # must not raise


def test_allow_admits_a_discovered_tool_at_its_own_rate(guard, clock):
    """A third-party MCP server's tools can't be hardcoded — nobody here knows
    what it offers until list_tools answers (§7.2)."""
    assert guard.check("scrape")[0] is False
    assert guard.allow("scrape", 2) is True
    assert guard.check("scrape")[0] is True
    assert guard.check("scrape")[0] is True
    ok, why = guard.check("scrape")                   # the bucket holds two
    assert ok is False and why == "rate limit"


def test_allow_never_widens_the_bucket_on_a_hand_she_already_has(guard):
    """A server that happens to advertise `play_music` must not raise the rate
    chosen for hers — or lower it."""
    assert guard.allow("play_music", 999) is False
    assert guard._rates["play_music"] == 6


def test_a_tool_that_was_never_discovered_is_still_denied(guard):
    guard.allow("scrape", 4)
    assert guard.check("rm_rf")[0] is False
