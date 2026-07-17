"""Typed configuration (SPEC §11 + §25) — extends Build #2's, which extends Build #1's.

The `desktop.config.Config` already holds every brain + voice knob
(models, Vault, STT/TTS/VAD, the loop). This subclass adds the Build #4 knobs
this build inherits — the hands, the room — and Build #5's own: the mind
(§15–§18), the budget governor (§17.3), DREAM (§21).
"""
from __future__ import annotations

from pathlib import Path

from yurios.desktop.config import Config as VoiceConfig


class Config(VoiceConfig):
    port: int = 8768                            # +1 off Build #4's 8767
    companion_name: str = "yuri"                # the `hello` event + the chat header

    # --- the hands: tools over MCP (SPEC §7) ---
    # mcp = the real in-repo MCP server over stdio (§7.2). fake = deterministic
    # offline results (tests, and a no-deps demo). off = no hands — she talks
    # about doing things instead of doing them (Build #2 behaviour).
    tools_backend: str = "mcp"                  # mcp | fake | off
    tool_max_calls_per_turn: int = 2            # per-turn cap (§7.3)
    tool_timeout_s: float = 10.0                # per-call timeout (§7.3)
    tool_log_dir: Path = Path("./tool-logs")    # JSONL audit, one line per call (§7.3)
    tool_rate_timer: int = 6                    # calls/minute, token bucket (§7.3)
    tool_rate_music: int = 6
    tool_rate_weather: int = 4
    timer_max_minutes: int = 180                # set_timer upper bound (§7.1)
    weather_backend: str = "open_meteo"         # open_meteo | fake (§7.5)
    weather_city: str = "Tokyo"                 # default when she isn't told one

    # --- her camera: selfies via the forge (SPEC §7.6) ---
    # openrouter = hosted generation (needs OPENROUTER_API_KEY, keeps the GPU
    # free). mock = deterministic placeholder cards, no key, no network (tests,
    # demos). off = no camera — the tool isn't advertised. A missing key
    # degrades openrouter → mock with one loud WARNING (the voice-fakes
    # philosophy). Default model: seedream — cheap enough for casual selfies;
    # sourceful/riverflow-v2.5-pro is the brand-art register (pricier, one knob).
    selfie_backend: str = "openrouter"          # openrouter | mock | off
    selfie_model: str = "bytedance-seed/seedream-4.5"
    selfie_dir: Path = Path("./selfies")        # saved shots, served at /selfies/
    tool_rate_selfie: int = 2                   # calls/minute — images are expensive

    # --- the mind: the always-on tick loop (SPEC §15–§18) ---
    mind_enabled: bool = True                   # off = Build #4 behaviour minus ambient life
    mind_seed: int = 0                          # 0 = unseeded; tests pin a seed
    mind_act_threshold: float = 0.4             # gate 1: salience-to-act (§18.1)
    mind_interrupt_threshold: float = 0.75      # gate 2: salience-to-interrupt (§18.2)
    mind_max_interrupts_per_day: int = 3        # the hard daily cap (§18.2)
    mind_consider_cooldown_s: float = 3600.0    # min gap between re-chewing one goal
    mind_daily_tokens: int = 200_000            # the budget governor's cap (§17.3)
    mind_dream_tick_tokens: int = 4000          # per-DREAM-tick consolidation budget (§21)

    # activity-state cadences + drift timeouts (§17.1)
    mind_engaged_cadence_s: float = 2.0
    mind_idle_cadence_s: float = 60.0
    mind_dormant_cadence_s: float = 900.0
    mind_dream_cadence_s: float = 5.0           # DREAM works in capped chunks, tick by tick
    mind_engaged_timeout_s: float = 180.0       # quiet this long → drop to IDLE
    mind_idle_timeout_s: float = 3600.0         # away this long → DORMANT
    mind_dream_start_hour: int = 2              # local window DORMANT may enter DREAM
    mind_dream_end_hour: int = 6

    # body reflexes + the murmur (§15.5 — the idle machine's windows, kept)
    idle_settle_s: float = 20.0                 # quiet after a turn before ambient life
    idle_act_min_s: float = 8.0                 # reflex window (gaze drift, pulse…)
    idle_act_max_s: float = 25.0
    idle_talk_min_s: float = 120.0              # the self-talk impulse window
    idle_talk_max_s: float = 300.0

    # --- channels: the mediums beyond this origin (SPEC §10.5) ---
    # A channel is on when its credentials are set; no separate enable flag.
    # The web page and the CLI need nothing here (they ride /api/events +
    # /api/chat on this origin); these knobs are for the outside mediums.
    telegram_bot_token: str = ""                # @BotFather token; empty = channel off
    telegram_chat_id: str = ""                  # the ONE chat she talks in; unset =
                                                #   pairing mode (the bot tells you the id)

    # --- the room (SPEC §6) ---
    rain_intensity: float = 0.6                 # 0..1, pushed to the scene at connect

    # --- the desktop window (SPEC §6.5–§6.6) ---
    # Which body `python -m yurios.world --window` floats: the VRM stage (/?desktop=1)
    # or the Build #2 Live2D client (/live2d/?desktop=1). The window
    # frame itself (WINDOW_* knobs) is inherited from the B2 config;
    # the Live2D rig inside it is the inherited AVATAR_MODEL knob.
    desktop_body: str = "vrm"                   # vrm | live2d
