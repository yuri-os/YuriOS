"""Config (SPEC §11 + §25) — Build #5's knobs on top of B4's on top of B2's."""
from __future__ import annotations

import os

from yurios.world.config import Config


def test_defaults():
    cfg = Config(_env_file=None)
    assert cfg.port == 8768                       # +1 off Build #4
    assert cfg.tools_backend == "mcp"
    assert cfg.tool_max_calls_per_turn == 2
    assert cfg.timer_max_minutes == 180
    assert cfg.rain_intensity == 0.6
    # the mind's dials (SPEC §15–§18)
    assert cfg.mind_enabled
    assert cfg.mind_act_threshold == 0.4
    assert cfg.mind_interrupt_threshold == 0.75
    assert cfg.mind_max_interrupts_per_day == 3
    assert cfg.mind_dormant_cadence_s == 900.0
    assert cfg.idle_settle_s == 20.0              # the reflex windows survive
    # the context window is unset by default: the provider's own default stands
    # until someone says otherwise (§11)
    assert cfg.context_length == 0
    # the B2 layer is still underneath (one Config object, four builds)
    assert cfg.tts_backend and cfg.vad_onset_frames


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TOOLS_BACKEND", "off")
    monkeypatch.setenv("MIND_INTERRUPT_THRESHOLD", "0.9")
    monkeypatch.setenv("MIND_ENABLED", "false")
    monkeypatch.setenv("RAIN_INTENSITY", "0.1")
    monkeypatch.setenv("CONTEXT_LENGTH", "32768")
    cfg = Config(_env_file=None)
    assert cfg.context_length == 32768
    assert cfg.tools_backend == "off"
    assert cfg.mind_interrupt_threshold == 0.9
    assert not cfg.mind_enabled
    assert cfg.rain_intensity == 0.1


def test_importing_yurios_quiets_the_libraries_that_phone_out():
    """§3: `import litellm` otherwise GETs a 1.67 MB price map from GitHub at every
    start, and Hugging Face downloads report your torch build and AI harness. The
    package sets both off switches before the libraries can read them."""
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_litellm_really_took_the_local_price_map():
    """The end of that: not that the variable is set, but that litellm obeyed it —
    the one assertion that fails if the import order ever slips."""
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

    info = get_model_cost_map_source_info()
    assert info["source"] == "local" and info["is_env_forced"]
