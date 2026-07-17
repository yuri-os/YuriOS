"""Guardrails (SPEC §7.3) — allowlist, token-bucket rates, truncation, audit."""
from __future__ import annotations

import json

from yurios.world.tools.guard import RESULT_MAX_CHARS, Guard


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


def test_truncate_caps_result_length():
    long = "x" * (RESULT_MAX_CHARS * 2)
    out = Guard.truncate(long)
    assert len(out) == RESULT_MAX_CHARS and out.endswith("…")
    assert Guard.truncate("short") == "short"


def test_audit_writes_one_jsonl_line_per_call_allowed_or_denied(guard, cfg):
    guard.audit("set_timer", {"minutes": 10}, "ok", 12.3, '{"seconds": 600}')
    guard.audit("rm_rf", {}, "denied: not a tool she has", 0.0, "")
    lines = [json.loads(l) for l in
             (cfg.tool_log_dir / "calls.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "set_timer" and lines[0]["verdict"] == "ok"
    assert lines[0]["duration_ms"] == 12.3
    assert lines[1]["verdict"].startswith("denied")
