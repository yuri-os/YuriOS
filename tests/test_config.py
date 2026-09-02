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
    assert not cfg.telegram_send_non_telegram
    # the B2 layer is still underneath (one Config object, four builds)
    assert cfg.tts_backend and cfg.vad_onset_frames
    assert cfg.voice_ws_max_connections == 8
    assert cfg.voice_ws_max_frame_bytes == 2048
    assert cfg.voice_ws_max_utterance_s == 60.0
    assert cfg.voice_ws_max_message_bytes == 64 * 1024
    # a hosted brain never trips the warm-headroom test, so a local camera
    # must not sit in VRAM until restart (§7.6a)
    assert cfg.selfie_unload_after_s == 3600.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TOOLS_BACKEND", "off")
    monkeypatch.setenv("MIND_INTERRUPT_THRESHOLD", "0.9")
    monkeypatch.setenv("MIND_ENABLED", "false")
    monkeypatch.setenv("RAIN_INTENSITY", "0.1")
    monkeypatch.setenv("CONTEXT_LENGTH", "32768")
    monkeypatch.setenv("TELEGRAM_SEND_NON_TELEGRAM", "true")
    monkeypatch.setenv("VOICE_WS_MAX_CONNECTIONS", "3")
    monkeypatch.setenv("VOICE_WS_IDLE_TIMEOUT_S", "12.5")
    monkeypatch.setenv("SELFIE_UNLOAD_AFTER_S", "900")
    cfg = Config(_env_file=None)
    assert cfg.context_length == 32768
    assert cfg.tools_backend == "off"
    assert cfg.mind_interrupt_threshold == 0.9
    assert not cfg.mind_enabled
    assert cfg.rain_intensity == 0.1
    assert cfg.telegram_send_non_telegram
    assert cfg.voice_ws_max_connections == 3
    assert cfg.voice_ws_idle_timeout_s == 12.5
    assert cfg.selfie_unload_after_s == 900.0


def test_example_enables_flash_attention_for_direct_gguf():
    cfg = Config(_env_file=".env.example")

    assert cfg.context_length == 32768
    assert cfg.gguf_context_length == 0
    assert cfg.gguf_flash_attn
    assert cfg.selfie_backend == "off"


def test_the_example_never_hands_a_comment_over_as_a_value():
    """`KEY=              # what it means` is a trap for any knob whose default
    is empty: python-dotenv reads the whole remainder as the value, so copying
    `.env.example` to `.env` sets it to the prose. It shipped that way for
    `SELFIE_LOCAL_MODEL` — a checkpoint path of English, failing at load with a
    message about a file nobody named — and `MIND_TOOL_ALLOWLIST` would be
    worse: the allowlist is the whole of what her hands may touch (§26.1), and
    a garbage one is a configuration nobody can read the meaning of.

    So the rule is a rule: an empty-valued key carries its comment on the lines
    ABOVE it, never after the `=`."""
    import dotenv

    swallowed = {key: value for key, value in dotenv.dotenv_values(".env.example").items()
                 if value and value.lstrip().startswith("#")}
    assert not swallowed, (
        "these keys took their trailing comment as their value — move the "
        f"comment above the assignment: {sorted(swallowed)}")


def test_the_example_ships_her_hands_off_and_empty():
    """The default-off proof, read off the file a new install actually copies
    (§26.1). Two separate decisions, and `.env.example` makes neither of them."""
    cfg = Config(_env_file=".env.example")

    assert not cfg.mind_tools_enabled
    assert cfg.mind_tool_allowlist == ""
    # …and even were both flipped, the caps are the shipped ones
    assert cfg.mind_tool_calls_per_day == 8
    assert cfg.mind_tool_pressure_ceiling == 0.5
    # a cooldown shorter than the goal's own re-consider gap is not a cooldown
    assert cfg.mind_tool_cooldown_cheap_s >= cfg.mind_consider_cooldown_s


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
