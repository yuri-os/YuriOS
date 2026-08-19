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
    # Set by the host, not by you (host.config_for_character): which registry id
    # this runtime *is*. A single-companion install has no registry and leaves it
    # empty. It exists because a notification has to be clickable — the shell
    # needs `/characters/<id>/sanctuary/` to open the room the reach-out came
    # from, and "the character named Mia" is not a URL.
    character_id: str = ""

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
    tool_rate_desk: int = 20                    # her workspace + skills (§34.2):
                                                #   local writes, no outside party
                                                #   — a loop-catcher, not a ration
    timer_max_minutes: int = 180                # set_timer upper bound (§7.1)

    # --- the web: search, read, research (SPEC §7.7) ---
    # searxng = your own metasearch instance — keyless AND third-party-less,
    # which is the local-first argument applied to the one capability that
    # usually hands your curiosity to somebody else. fake = deterministic
    # offline rows (tests, demos). off = no web hands at all: the three tools
    # aren't advertised, the SELFIE_BACKEND=off rule.
    #
    # DEFAULT IS OFF. A SearXNG instance is something you have to stand up
    # first, and a hand that always errors is worse than a hand she doesn't
    # have. Set this to `searxng` once the instance below answers.
    #
    # NOTE the instance needs `json` in its settings.yml `search.formats` — it
    # is disabled by default and the symptom is a 403 on every query.
    search_backend: str = "off"                 # searxng | fake | off
    searxng_url: str = "http://localhost:8080"
    search_results: int = 5                     # rows per web_search
    search_language: str = "en"
    search_safesearch: int = 1                  # 0 none | 1 moderate | 2 strict
    fetch_timeout_s: float = 8.0                # one page, inside TOOL_TIMEOUT_S
    fetch_max_bytes: int = 2_000_000            # read_page stops here
    research_max_pages: int = 5                 # ceiling on `research(depth=…)`
    tool_rate_search: int = 6                   # calls/minute
    tool_rate_read: int = 6
    tool_rate_research: int = 2                 # a run is many requests — cheap
                                                #   to ask for, expensive to serve

    # --- other people's hands: third-party MCP servers (SPEC §7.2) ---
    # A JSON file in the familiar `{"mcpServers": {...}}` shape, so a server
    # config you already have somewhere else pastes straight in. Empty (the
    # default) means the in-repo server alone — exactly today's behaviour.
    # Mounting a server gives her its tools, rate-limited but not reviewed.
    mcp_servers: str = ""                       # path to mcp-servers.json
    tool_rate_external: int = 4                 # default bucket for a discovered
                                                #   tool with no rate of its own

    # --- pictures *in*: showing her something (SPEC §35) ---
    # The mirror of the camera below — that one is what she sends you, this is
    # what you can send her, and it exists only when the model on the other end
    # of the chat seam takes image parts as well as text. `auto` asks the
    # provider at boot (app/providers/vision.py) and believes the answer; `on`
    # and `off` are the override for the case it gets wrong, because a probe
    # does not get the last word on something the user can see with their own
    # eyes. Off (or a text-only model) means the composer has no paperclip and
    # POST /api/uploads is refused — an affordance that errors is worse than
    # one that isn't there.
    chat_image_input: str = "auto"              # auto | on | off
    upload_dir: Path = Path("./uploads")        # what you sent her, served at
                                                #   /api/uploads/<name>
    # Sent pictures are re-encoded before she sees them (world/uploads.py): the
    # long side is capped, EXIF orientation is applied and then dropped with the
    # rest of the metadata. The cap is a context-window knob as much as a
    # bandwidth one — an image is worth a four-figure number of tokens, and 1024
    # is about the smallest that still reads handwriting.
    chat_image_max_px: int = 1024
    upload_max_bytes: int = 12_000_000          # what the route accepts, before
                                                #   the re-encode shrinks it
    upload_keep: int = 200                      # newest N kept on disk; older
                                                #   files are pruned on save

    # --- her camera: selfies via the forge (SPEC §7.6) ---
    # openrouter = hosted generation (needs OPENROUTER_API_KEY, keeps the GPU
    # free). mock = deterministic placeholder cards, no key, no network (tests,
    # demos). off = no camera — the tool isn't advertised. A missing key
    # degrades openrouter → mock with one loud WARNING (the voice-fakes
    # philosophy). Default model: seedream — cheap enough for casual selfies;
    # sourceful/riverflow-v2.5-pro is the brand-art register (pricier, one knob).
    selfie_backend: str = "off"                 # openrouter | diffusers | mock | off
    selfie_model: str = "bytedance-seed/seedream-4.5"
    selfie_dir: Path = Path("./selfies")        # saved shots, served at /selfies/
    # Optional overlay yaml merged over the shipped template library
    # (forge/templates.py — sections merge key-by-key). The shipped library
    # stays everyday; personal registers are user-supplied files outside the
    # repo, exactly like the checkpoint below. Set but missing → one loud
    # WARNING and the shipped library alone.
    selfie_templates_extra: str = ""
    # Her *own* template library, which REPLACES the shipped one rather than
    # merging over it (characters/selfiebook.py). Empty, or a path with no file
    # there, means the shipped book — which is what every character has until
    # somebody edits her library in the studio. A character runtime points this
    # at `data/characters/<id>/selfie.yaml` (host.py, `config_for_character`);
    # the overlay above still layers on top of whichever base wins, so a
    # house-wide register keeps working for characters with no book of their own.
    selfie_templates: str = ""
    # Whose likeness the camera renders — an appearance yaml (forge/character.py).
    # Empty = the shipped Yuri, which is the right answer for a single-character
    # house; a character runtime points this at her own `appearance.yaml`
    # (host.py, `config_for_character`), because rendering one character with
    # another's face is the one failure a camera must never have.
    selfie_character: str = ""
    tool_rate_selfie: int = 2                   # calls/minute — images are expensive
    # `show_picture` — the same camera pointed at anything that isn't her. Its
    # own bucket rather than a share of the selfie one: they cost the same GPU
    # but they are different urges, and spending her selfie budget on a photo of
    # the rain shouldn't stop her sending you her face a minute later.
    tool_rate_picture: int = 2

    # --- the local camera: SELFIE_BACKEND=diffusers (your GPU, no third party) ---
    # An SDXL .safetensors checkpoint loaded in-process (Illustrious-lineage
    # recommended; e.g. a Pie Model from Civitai). The file is user-supplied —
    # never shipped. Defaults are the Pie author's own: DPM++ 2M / Karras /
    # 30 steps / CFG 5 / hires fix on. ~7 GB fp16 resident; cpu_offload trades
    # speed for headroom. Missing deps or checkpoint degrade to mock, loudly.
    selfie_local_model: str = ""                # path to the .safetensors checkpoint
    selfie_local_device: str = "cuda"           # cuda | cpu (cpu is for emergencies)
    selfie_local_steps: int = 30
    selfie_local_cfg: float = 5.0
    selfie_local_hires: bool = True             # the A1111 "Hires fix" second pass
    selfie_local_hires_scale: float = 1.5
    selfie_local_hires_denoise: float = 0.35
    selfie_local_cpu_offload: bool = False
    # Krea 2 checkpoints (a diffusion transformer, not a UNet) are detected from
    # the file and loaded by the krea2 backend instead — they share the knobs
    # above but need their own sampling numbers, because SDXL's 30/5.0 burns a
    # distilled Krea 2. 0 / -1 mean "read it off the checkpoint": 8 steps at
    # guidance 0.0 for a turbo/TDM export, 28 at 4.5 for a base one.
    selfie_krea2_steps: int = 0
    selfie_krea2_cfg: float = -1.0
    # When a local render is requested and free VRAM won't hold the resident
    # pipeline, park her LLM for the render's duration: LM Studio models are
    # evicted and re-pinned via the boot path's ensure_resident; direct gguf/
    # contexts (llama.cpp, in-process) are closed and reloaded. Seconds of
    # render instead of a minute of offload; her brain is always restored, even
    # on a failed render.
    selfie_llm_park: bool = True
    # Room her brain needs on the card after a render, in GiB. The camera keeps
    # its pipeline warm between renders (worth ~25 s a selfie) only while this
    # much VRAM is still free with the pipeline loaded — otherwise the brain
    # reloads beside it, the card fills, and the *next* render OOMs. Raise it
    # if your chat model is bigger than ~6 GiB on the card.
    selfie_warm_headroom_gib: float = 6.0

    # --- her voice, on demand (SPEC §9.9 — world/voicestack.py) ---
    # Kokoro + faster-whisper + silero are the heaviest thing a runtime holds,
    # and only /ws/voice ever wants them: the text room, /api/chat, the channels
    # and the mind all run on the brain alone. So the stack loads when a client
    # opens the audio socket and is freed when the last one closes it — a node
    # hosting six characters keeps one voice resident, not six.
    voice_preload: bool = False                 # 1 = warm at boot instead (single-companion)
    # How long an empty room keeps her voice before it's freed. A page reload is
    # a disconnect too, and paying 20 s of model loading for an F5 is worse than
    # holding the memory a minute. 0 = drop it the moment the last client goes;
    # negative = never unload once loaded.
    voice_unload_after_s: float = 60.0

    # --- the mind: the always-on tick loop (SPEC §15–§18) ---
    mind_enabled: bool = True                   # off = Build #4 behaviour minus ambient life
    mind_seed: int = 0                          # 0 = unseeded; tests pin a seed
    mind_act_threshold: float = 0.4             # gate 1: salience-to-act (§18.1)
    mind_interrupt_threshold: float = 0.75      # gate 2: salience-to-interrupt (§18.2)
    mind_max_interrupts_per_day: int = 3        # the hard daily cap (§18.2)
    mind_consider_cooldown_s: float = 3600.0    # min gap between re-chewing one goal
    mind_daily_tokens: int = 200_000            # the budget governor's cap (§17.3)
    mind_dream_tick_tokens: int = 40000         # per-DREAM-tick consolidation budget (§21)
    mind_trace_max_bytes: int = 2_000_000       # ticks.jsonl rotates to .1 past this size
    # The rest of the observability sinks, all rotating the same way (to `.1`,
    # one generation, then gone). An always-on mind writes to these forever, so
    # a cap is not tidiness — it is the difference between a log and a leak.
    tool_log_max_bytes: int = 2_000_000         # tool-logs/calls.jsonl
    mind_signal_max_bytes: int = 2_000_000      # traces/signals.jsonl
    mind_activity_log_max_bytes: int = 512_000  # traces/activity.jsonl (tiny records)
    mind_prompt_log_max_bytes: int = 32_000_000  # traces/prompts.jsonl (whole prompts)
    mind_prompt_capture: bool = True            # off = no assembled prompts on disk
    mind_prompt_max_chars: int = 200_000        # per-message cap inside a prompt record

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
    # Telegram-originated turns always receive their reply there. This opt-in
    # controls whether replies from web/voice/CLI/API chats are copied there too.
    telegram_send_non_telegram: bool = False
    # An outside account belongs to exactly one character, so with more than one
    # of them in the house each gets her own bot: TELEGRAM_BOT_TOKEN_<ID> and
    # TELEGRAM_CHAT_ID_<ID> are hers alone (host.telegram_for_character). The
    # unsuffixed pair above is the single-companion install's; this names who
    # keeps it once there are others. Unset, it is offered to every character
    # and the first runtime to start holds it (channels/manager.py).
    telegram_character: str = ""                # registry id, or "" = whoever starts first
    # Set by the host, not by you: which two variables this character's bot is
    # written in. Pairing mode names hers, and the settings panel edits hers.
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    # The desktop notification channel (channels/notify.py) — the transport of
    # last resort for a reach-out. Unlike the others this one has no credentials
    # to be switched on by, so it needs a flag, and the flag is **off**: nothing
    # should start drawing on your desktop because you installed it. On, it
    # carries only `unheard` lines — what Gate 2 already decided to say into an
    # empty room — never greetings or replies.
    notify_enabled: bool = False
    # auto = the desktop shell when one is attached, else notify-send.
    # shell | libnotify pin one renderer; off is the same as notify_enabled=False
    # and exists so a character can opt out without editing the house default.
    notify_backend: str = "auto"                # auto | shell | libnotify | off

    # Her tray icon (§18.4.7). ON by default, unlike the doorbell: a tray icon
    # is a thing you can look at, not a thing that interrupts you, and it costs
    # nothing where there is no tray — no session bus, no watcher, or a headless
    # box and it quietly never appears. It reads the host in-process and posts
    # no presence, so it can sit there for a week without her reading it as
    # company (§18.4.5).
    tray_enabled: bool = True

    # --- the room (SPEC §6) ---
    rain_intensity: float = 0.6                 # 0..1, pushed to the scene at connect

    # --- the desktop window (SPEC §6.5–§6.6) ---
    # Which body `python -m yurios.world --window` floats: the VRM stage (/?desktop=1)
    # or the Build #2 Live2D client (/live2d/?desktop=1). The window
    # frame itself (WINDOW_* knobs) is inherited from the B2 config;
    # the Live2D rig inside it is the inherited AVATAR_MODEL knob.
    desktop_body: str = "vrm"                   # vrm | live2d
