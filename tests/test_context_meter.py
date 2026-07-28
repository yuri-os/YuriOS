"""The context meter (SPEC §11) — how full her window is, and who says so.

The failure this exists to make visible: a prompt that grows past the model's
context window, which LM Studio answers with "Context size has been exceeded"
and a lost turn. The gauge only helps if three things hold — the used side is
measured on every model pass, the ceiling is the window she is ACTUALLY running
in (or honestly unknown), and both reach the frontend before the turn that
breaks.
"""
from __future__ import annotations

import json
import pytest

from yurios.app.providers.usage import chunk_prompt_tokens, chunk_text
from yurios.world.context import ContextMeter, estimate_messages, short_tokens
from yurios.world.hub import EventHub

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                     # noqa: E402


def drain(q) -> list[dict]:
    return [q.get_nowait() for _ in range(q.qsize())]


# ---- measuring ---------------------------------------------------------------

def test_estimate_counts_every_message_plus_its_framing():
    msgs = [{"role": "system", "content": "x" * 4000},
            {"role": "user", "content": "y" * 400}]
    # ~4 chars/token, plus the per-message role framing every chat template adds
    assert estimate_messages(msgs) == 1000 + 4 + 100 + 4


def test_estimate_survives_a_multimodal_content_list():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "z" * 40},
                                         {"type": "image_url", "image_url": {}}]}]
    assert estimate_messages(msgs) == 10 + 4


def test_the_servers_own_count_wins_over_the_estimate():
    m = ContextMeter(limit=8192)
    m.note_prompt([{"role": "user", "content": "x" * 400}])
    assert (m.used, m.exact) == (104, False)
    m.note_usage(1234)                         # the usage the stream volunteered
    assert (m.used, m.exact) == (1234, True)
    # …and the next prompt goes back to estimating, honestly labelled
    m.note_prompt([{"role": "user", "content": "x" * 400}])
    assert (m.used, m.exact) == (104, False)


def test_a_zero_usage_is_ignored():
    """Some servers send a usage block with nothing in it — don't zero the gauge."""
    m = ContextMeter()
    m.note_prompt([{"role": "user", "content": "x" * 4000}])
    m.note_usage(0)
    assert m.used == 1004


# ---- where the exact count comes from (providers/usage.py) -------------------
#
# The regression risk in reading usage off a stream: it arrives on a chunk with
# no choices at all, so the obvious `chunk.choices[0]` loop body raises at the
# very end of an otherwise perfect reply — her voice, lost to a token count.

class Chunk:
    """A streamed chunk, shaped like LiteLLM's."""

    def __init__(self, text=None, prompt_tokens=None):
        self.choices = []
        self.usage = None
        if text is not None:
            self.choices = [type("C", (), {"delta": {"content": text}})()]
        if prompt_tokens is not None:
            self.usage = type("U", (), {"prompt_tokens": prompt_tokens,
                                        "completion_tokens": 7})()


def test_a_usage_only_chunk_yields_no_text_and_never_raises():
    final = Chunk(prompt_tokens=4821)
    assert chunk_text(final) == ""
    assert chunk_prompt_tokens(final) == 4821


def test_an_ordinary_chunk_carries_text_and_no_usage():
    assert chunk_text(Chunk("hey")) == "hey"
    assert chunk_prompt_tokens(Chunk("hey")) == 0


def test_a_server_that_never_mentions_usage_reads_as_zero():
    assert chunk_prompt_tokens(Chunk()) == 0
    assert chunk_prompt_tokens(Chunk(prompt_tokens=None)) == 0
    assert chunk_prompt_tokens(Chunk(prompt_tokens="nonsense")) == 0


# ---- the ceiling -------------------------------------------------------------

def test_unknown_window_is_reported_as_unknown_not_guessed():
    snap = ContextMeter().snapshot()
    assert snap["limit"] is None and snap["pct"] is None
    assert snap["used"] == 0


def test_a_probed_window_fills_in_a_blank_ceiling():
    m = ContextMeter()
    m.set_limit(32768, "lm studio")
    assert m.snapshot()["limit"] == 32768
    assert m.snapshot()["limit_source"] == "lm studio"


def test_the_observed_window_beats_the_one_we_asked_for():
    """CONTEXT_LENGTH is a request; the loaded instance is the fact. They come
    apart when the window won't fit in RAM — and measuring against a ceiling she
    does not have is the exact failure this gauge exists to prevent."""
    m = ContextMeter(limit=32768)
    m.set_limit(4096, "lm studio")
    assert (m.snapshot()["limit"], m.snapshot()["limit_source"]) == (4096, "lm studio")


def test_percentage_is_of_the_window():
    m = ContextMeter(limit=10000)
    m.note_usage(2500)
    assert m.snapshot()["pct"] == 25.0


def test_measurements_append_character_local_history(tmp_path):
    m = ContextMeter(limit=1000, trace_dir=tmp_path)
    m.note_prompt([{"role": "user", "content": "x" * 400}])
    m.note_usage(125)
    rows = [json.loads(line) for line in
            (tmp_path / "context.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["source"] for row in rows] == ["estimate", "usage"]
    assert rows[-1]["used"] == 125 and rows[-1]["pct"] == 12.5


def test_the_reply_is_part_of_what_must_fit():
    """A gauge that ignores MAX_REPLY_TOKENS reads green right up to the failure,
    so `reserve` rides the wire and the UI thresholds count it."""
    m = ContextMeter(limit=8192, reserve=2048)
    m.note_usage(7000)
    snap = m.snapshot()
    assert snap["reserve"] == 2048
    assert snap["used"] + snap["reserve"] > snap["limit"]


# ---- reaching the frontend ---------------------------------------------------

async def test_every_measurement_publishes_a_sticky_event():
    hub = EventHub()
    q = hub.subscribe()
    m = ContextMeter(hub, limit=8192, reserve=1600)
    m.note_prompt([{"role": "user", "content": "x" * 4000}])
    events = drain(q)
    assert events[-1]["type"] == "context"
    assert events[-1]["used"] == 1004 and events[-1]["limit"] == 8192

    # a page opened mid-conversation gets the last reading, not a blank gauge
    assert drain(hub.subscribe())[-1]["used"] == 1004


async def test_a_known_window_shows_before_the_first_turn():
    hub = EventHub()
    ContextMeter(hub, limit=8192)
    assert drain(hub.subscribe()) == [
        {"type": "context", "used": 0, "limit": 8192, "limit_source": "env",
         "reserve": 0, "exact": False, "pct": 0.0}]


def test_short_tokens_reads_like_people_talk():
    assert (short_tokens(8192), short_tokens(32768), short_tokens(900)) == \
        ("8.2k", "32.8k", "900")


# ---- the route ---------------------------------------------------------------

def test_api_context_serves_the_snapshot(cfg):
    app = create_app(cfg.model_copy(update={"context_length": 16384}),
                     brain=FakeBrain())
    with TestClient(app) as c:
        body = c.get("/api/context").json()
        assert body["limit"] == 16384 and body["limit_source"] == "env"
        assert body["reserve"] == cfg.max_reply_tokens
        # and it's on the health page too, where "why did that turn fail?" is asked
        assert c.get("/api/health").json()["context"]["limit"] == 16384


def test_the_meter_is_wired_to_the_chat_provider(cfg):
    """One attachment, at the seam every spoken path funnels through — reply,
    greeting, ambient, and each pass of the tool loop all end at `state.chat`."""
    class MeteredChat:
        meter = None                       # what a real LiteLLMChatModel exposes

    brain = type("B", (), {"state": type("S", (), {"chat": MeteredChat()})()})()
    rt = create_app(cfg, brain=brain).state.rt
    assert brain.state.chat.meter is rt.context


def test_a_brain_with_no_such_seam_is_left_alone(cfg):
    """An injected test brain (no AppState) must not be a boot failure — the
    gauge just stays at zero."""
    assert create_app(cfg, brain=FakeBrain()).state.rt.context.used == 0


def test_an_unset_window_is_null_all_the_way_out(cfg):
    # unset explicitly: the machine's own .env may well name a CONTEXT_LENGTH,
    # and a gauge that reads null only on machines without one proves nothing
    app = create_app(cfg.model_copy(update={"context_length": 0}),
                     brain=FakeBrain())
    with TestClient(app) as c:
        assert c.get("/api/context").json()["limit"] is None
