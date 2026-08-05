"""Changing the model she thinks with, without a restart (SPEC §31.4).

The providers are two small objects; the swap is rebuilding them and pointing
the brain's AppState at the new pair. These tests pin the three things that make
that safe: the live Config is *mutated* (every holder reads the new value), the
memory store's own utility reference follows, and a runtime whose models were
injected by a caller keeps them.
"""
from __future__ import annotations

import pytest

from yurios.world import rewire


class FakeStore:
    def __init__(self, utility):
        self.utility = utility


class FakeState:
    """The three attributes of Build #1's AppState the swap touches."""

    def __init__(self):
        self.chat = "the model she started on"
        self.utility = "the utility model she started on"
        self.store = FakeStore(self.utility)


METER = object()          # stands in for the runtime's ContextMeter


def move(cfg, **wanted):
    state = FakeState()
    applied = rewire.apply(state, cfg, rewire.differences(cfg, wanted), meter=METER)
    return state, applied


def test_a_model_swap_rebuilds_both_providers_and_the_memory_store(cfg):
    state, applied = move(cfg, chat_model="ollama/llama3", utility_model="ollama/llama3")

    assert applied == ["chat_model", "utility_model"]
    assert cfg.chat_model == "ollama/llama3"          # the live Config, in place
    assert state.chat.model == "ollama/llama3"
    assert state.chat.api_base == cfg.ollama_base_url  # a local id needs its server
    # her memory summarises through its own reference — miss it and USER.md would
    # keep being written by the model she just left
    assert state.store.utility is state.utility
    assert state.utility.model == "ollama/llama3"
    # the context gauge reads the prompt inside the provider, so a rebuilt one
    # that lost the meter would silently freeze the masthead readout
    assert state.chat.meter is METER


def test_a_hosted_route_travels_on_the_key_not_a_base_url(cfg):
    cfg.openrouter_api_key = "sk-test"
    state, _ = move(cfg, chat_model="openrouter/anthropic/claude-3.5-haiku")

    assert state.chat.api_base is None
    assert state.chat.api_key == "sk-test"


def test_the_reasoning_switch_reaches_the_new_provider(cfg):
    state, applied = move(cfg, chat_thinking=True)

    assert applied == ["chat_thinking"] and state.chat.thinking is True


def test_settings_that_already_match_change_nothing(cfg):
    state, applied = move(cfg, **rewire.snapshot(cfg))

    assert applied == []
    assert state.chat == "the model she started on"    # not even rebuilt


def test_the_per_call_knobs_need_no_new_provider(cfg):
    """`temperature` and the reply cap are read off the Config at call time, so
    they are live the moment they land — no rebuild, and none needed."""
    state, applied = move(cfg, temperature=0.25, max_reply_tokens=800)

    assert applied == ["max_reply_tokens", "temperature"]
    assert cfg.temperature == 0.25 and cfg.max_reply_tokens == 800
    assert state.chat == "the model she started on"


def test_a_value_out_of_the_registrys_json_is_coerced_or_refused(cfg):
    assert rewire.coerce(cfg, "temperature", "0.4") == 0.4
    assert rewire.coerce(cfg, "chat_thinking", "false") is False
    assert rewire.coerce(cfg, "context_length", "32768") == 32768
    with pytest.raises(ValueError):
        rewire.coerce(cfg, "temperature", "warm")
    with pytest.raises(ValueError):
        rewire.coerce(cfg, "not_a_knob", "x")


async def test_an_injected_model_is_the_callers_and_is_left_alone(cfg):
    """The route suites hand the runtime a scripted chat model. A retune moves
    her knobs; it must not replace somebody else's object with a real provider."""
    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app

    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False,
                                            # an ollama id keeps the repin path
                                            # (which would reach for LM Studio)
                                            # out of an offline test
                                            "chat_model": "ollama/first"}),
                     brain=FakeBrain())
    rt = app.state.rt
    result = await rt.retune({"chat_model": "ollama/second", "temperature": 0.3})

    assert result["applied"] == ["chat_model", "temperature"]
    assert rt.cfg.chat_model == "ollama/second"
    assert isinstance(rt.brain, FakeBrain)


async def test_a_live_model_choice_retires_the_first_run_chooser(cfg, monkeypatch):
    """A model set *live* from the switchboard lands through retune, and the
    onboarding panel reads `model_configured` — a snapshot taken at boot. If the
    retune does not move it, the sanctuary keeps asking her to choose a model
    she already has (and a cleared override must bring the chooser back)."""
    from types import SimpleNamespace

    from yurios.world.main import Runtime, ToolBrain

    monkeypatch.setattr(ToolBrain, "build",
                        staticmethod(lambda cfg, **kw: SimpleNamespace(state=None)))
    rt = Runtime(cfg, embedder=object())
    assert not rt.model_configured                       # the fresh-install boot

    chosen = await rt.retune({"chat_model": "ollama/qwen3"})
    assert chosen["applied"] == ["chat_model"] and rt.model_configured

    cleared = await rt.retune({"chat_model": "NONE"})
    assert cleared["applied"] == ["chat_model"] and not rt.model_configured
