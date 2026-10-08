"""The `.env` knobs, as one table both the panel and the terminal read (SPEC §11).

`.env` is the house configuration: read once at boot into the typed `Config`
(`yurios/world/config.py`, extending the voice config, extending the brain's), so
everything written here takes effect on the next restart rather than hot-applying
into a running model. Two surfaces edit it — the settings panel
(`desktop/routes/settings.py`, the gear in every room and the switchboard's own
button) and `yurios settings` on the command line — and they agree because
neither owns the list: this module does.

The list is built from two halves.

**The curated table** below is hand-written, and is what the panel shows first:
the couple of dozen knobs somebody actually opens the panel to change, with the
control each one deserves — a provider+model combobox, an enum as a dropdown, a
secret as write-only, a per-character channel credential resolving to the
variable *this* runtime reads it from (§10.5).

**The derived table** is every remaining field of the running `Config`, typed
from its annotation, grouped and described by walking `.env.example` — which is
already an organised, commented catalogue of all of them. That is what makes this
a *general* settings surface instead of a shortlist: a knob added to the config
and documented in the example file appears in the panel and in the CLI with no
edit here. A field the running build has no knob for is dropped rather than shown
dead, so the same table serves the world server and the smaller desktop app.

A derived field can be **`ENRICHED`** in place without leaving its section: an
annotation says `str` where the value is really a list of tool names, and a text
box over a closed vocabulary is the one field nobody can fill in — the names
exist only in the source. Those get the whole vocabulary instead.

Writes are surgical. `update_env` upserts one line per key, uncommenting a
matching `# KEY=` in place, so the prose in `.env` survives being edited by a
form. And `check` refuses the two combinations that would leave an installation
unable to boot at all — a short owner token, or a non-loopback bind with no token
— because the process that reads them raises at startup (`yurios/security.py`)
and the panel that wrote them would be behind a server that no longer starts.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from yurios import daemon
from yurios.security import MIN_TOKEN_LENGTH, is_loopback

from yurios.desktop.avatar_models import MODELS

# The `.env` of the installation this process belongs to — the same answer
# `yurios` commands get (daemon.install_root: the package's own project
# directory, or `YURIOS_ROOT`), so the panel and the terminal edit one file. The
# example beside it is the catalogue the derived half of the table reads.
ROOT = daemon.install_root()
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = Path(__file__).resolve().parents[1] / ".env.example"

_LANGS = ["en", "ja", "zh", "ko", "yue", "auto"]
_WHISPER = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]

# Each field: key = the .env name (UPPERCASE); attr = the Config() attribute the
# current value is read from; type ∈ {select,text,password,number,bool}; options
# for a select; suggest = datalist hints for an open combobox; help = one line.
CURATED: list[dict] = [
    {"group": "You", "fields": [
        {"key": "USER_NAME", "attr": "user_name", "type": "text",
         "help": "your name in her prompts — not 'you'. The default 'you' collides "
                 "with You meaning her, so she reads 'you is here' as herself"},
    ]},
    {"group": "Brain", "fields": [
        # the key comes first: set it once and it's ready before you pick an
        # openrouter/… route below (and it's what the model browse authenticates with).
        {"key": "OPENROUTER_API_KEY", "attr": "openrouter_api_key", "type": "password",
         "help": "needed for openrouter/… models — set this first (openrouter.ai/keys)"},
        {"key": "CHAT_MODEL", "attr": "chat_model", "type": "model",
         "help": "her reply voice — pick a provider, then type a model or browse what's loaded"},
        {"key": "UTILITY_MODEL", "attr": "utility_model", "type": "model",
         "help": "model for summaries/extraction (runs off the hot path)"},
        {"key": "LMSTUDIO_BASE_URL", "attr": "lmstudio_base_url", "type": "text",
         "suggest": ["http://localhost:1234/v1"],
         "help": "OpenAI-compatible endpoint for lm_studio/… ids (chat + embeddings)"},
        {"key": "OLLAMA_BASE_URL", "attr": "ollama_base_url", "type": "text",
         "suggest": ["http://localhost:11434"],
         "help": "local Ollama server — routes ollama/… ids and lists your pulled models"},
        {"key": "CONTEXT_LENGTH", "attr": "context_length", "type": "number",
         "min": "0",
         "help": "her context window in tokens — 0 = the provider's default. "
                 "Loads the LM Studio model at this size and sets the ceiling the "
                 "masthead gauge measures against; raise it if turns start failing"},
        {"key": "CHAT_THINKING", "attr": "chat_thinking", "type": "bool",
          "help": "reply <think> pass — OFF for real-time voice (a reasoning model would stall)"},
        {"key": "CHAT_REASONING_EFFORT", "attr": "chat_reasoning_effort", "type": "select",
         "options": ["", "low", "medium", "high"],
         "help": "reply reasoning effort when thinking is on — blank uses the provider default"},
        {"key": "UTILITY_THINKING", "attr": "utility_thinking", "type": "bool",
         "help": "extraction/summary <think> pass — ON (off the hot path, quality matters)"},
        {"key": "UTILITY_MAX_TOKENS", "attr": "utility_max_tokens", "type": "number",
         "help": "budget for the utility call's <think> block + JSON — too small loses the fact"},
    ]},
    {"group": "Embeddings", "fields": [
        {"key": "EMBED_BACKEND", "attr": "embed_backend", "type": "select",
         "options": ["sentence_tf", "lm_studio", "ollama"],
         "help": "sentence_tf runs in-process — no LM Studio/Ollama needed. "
                 "lm_studio/ollama reuse a local server. A swap auto-reindexes."},
        {"key": "EMBED_MODEL", "attr": "embed_model", "type": "embed_model",
         "backend_key": "EMBED_BACKEND",
         "help": "MUST match the backend: sentence_tf → a HF repo (BAAI/bge-small-en-v1.5); "
                 "lm_studio/ollama → browse that server's models (text-embedding-nomic-…)"},
        {"key": "EMBED_DIM", "attr": "embed_dim", "type": "number",
         "help": "must equal the model's vector width (bge-small=384, nomic=768)"},
    ]},
    {"group": "Storage", "fields": [
        {"key": "DATA_DIR", "attr": "data_dir", "type": "text",
         "help": "active house data: character records and each character's Vault"},
        {"key": "VAULT_DIR", "attr": "vault_dir", "type": "text",
         "help": "legacy import source only; changing it does not move an existing character's Vault"},
        {"key": "SOUL_SRC", "attr": "soul_src", "type": "text",
         "help": "legacy SOUL source used when seeding or importing a character"},
    ]},
    {"group": "Server", "fields": [
        {"key": "HOST", "attr": "host", "type": "text", "suggest": ["127.0.0.1", "0.0.0.0"],
         "help": "127.0.0.1 keeps her local-only; any other bind requires OWNER_TOKEN"},
        {"key": "PORT", "attr": "port", "type": "number",
         "help": "the port the switchboard and every room are served on"},
        {"key": "OWNER_TOKEN", "attr": "owner_token", "type": "password",
         "help": "the secret a phone or a second machine comes in with (32+ characters). "
                 "Don't type one — generate it, then pair the device by scanning the code"},
    ]},
    {"group": "Speech-to-text", "fields": [
        {"key": "STT_BACKEND", "attr": "stt_backend", "type": "select",
         "options": ["faster_whisper", "fake"],
         "help": "faster_whisper transcribes on this machine; fake hears nothing "
                 "(a silent stand-in where the voice stack isn't installed)"},
        {"key": "STT_MODEL", "attr": "stt_model", "type": "select", "options": _WHISPER,
         "relevant_if": {"STT_BACKEND": ["faster_whisper"]},
         "help": "smaller = lower latency, less accurate"},
        {"key": "STT_COMPUTE", "attr": "stt_compute", "type": "text",
         "relevant_if": {"STT_BACKEND": ["faster_whisper"]},
         "help": "faster-whisper compute type, normally int8 on CPU"},
    ]},
    {"group": "Text-to-speech", "fields": [
        {"key": "TTS_BACKEND", "attr": "tts_backend", "type": "select",
         "options": ["qwen3_tts", "kokoro", "gpt_sovits", "fake"],
         "help": "the fields below apply to whichever backend you pick"},
        {"key": "TTS_REGISTER", "attr": "tts_register", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["kokoro"]},
         "help": "kokoro voice register only"},
        {"key": "TTS_SAMPLE_RATE", "attr": "tts_sample_rate", "type": "number",
         "relevant_if": {"TTS_BACKEND": ["kokoro", "qwen3_tts", "gpt_sovits"]},
         "help": "output sample rate; the bundled voices use 24000 Hz"},
        {"key": "QWEN_MODEL", "attr": "qwen_model", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "Qwen Base model for clone mode, or VoiceDesign model for design mode"},
        {"key": "QWEN_MODE", "attr": "qwen_mode", "type": "select",
          "options": ["clone", "design"],
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "qwen3_tts: clone is stable; design re-authors the voice each turn (drifts)"},
        {"key": "QWEN_REF_AUDIO", "attr": "qwen_ref_audio", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"], "QWEN_MODE": ["clone"]},
         "help": "qwen3_tts clone reference wav; blank = bundled designed.wav"},
        {"key": "QWEN_REF_TEXT", "attr": "qwen_ref_text", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"], "QWEN_MODE": ["clone"]},
         "help": "exact transcript of the clone reference"},
        {"key": "QWEN_INSTRUCT", "attr": "qwen_instruct", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"], "QWEN_MODE": ["design"]},
         "help": "voice description used only in Qwen design mode"},
        {"key": "QWEN_LANGUAGE", "attr": "qwen_language", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "the language she speaks in, by name (English, Japanese, Chinese…)"},
        {"key": "QWEN_DEVICE", "attr": "qwen_device", "type": "text",
         "suggest": ["cuda:0", "cuda:1", "cpu"],
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "which GPU runs Qwen (cuda:0 is the first); it needs a CUDA GPU"},
        {"key": "QWEN_DTYPE", "attr": "qwen_dtype", "type": "text",
         "suggest": ["bfloat16", "float16"],
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "weight precision — bfloat16 on recent NVIDIA cards, float16 on older ones"},
        {"key": "QWEN_ATTN", "attr": "qwen_attn", "type": "text",
         "suggest": ["sdpa", "flash_attention_2"],
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]},
         "help": "attention kernel — sdpa works everywhere; flash_attention_2 uses "
                 "less VRAM when it is installed"},
        {"key": "SOVITS_BASE_URL", "attr": "sovits_base_url", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]},
         "help": "gpt_sovits api_v2 server url"},
        {"key": "SOVITS_REF_AUDIO", "attr": "sovits_ref_audio", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]},
         "help": "gpt_sovits reference wav (path on the server); blank = bundled designed.wav"},
        {"key": "SOVITS_PROMPT_TEXT", "attr": "sovits_prompt_text", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]},
         "help": "exact transcript of the sovits reference clip"},
        {"key": "SOVITS_PROMPT_LANG", "attr": "sovits_prompt_lang", "type": "select",
         "options": _LANGS, "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]},
         "help": "the language of the reference clip's transcript"},
        {"key": "SOVITS_TEXT_LANG", "attr": "sovits_text_lang", "type": "select",
         "options": _LANGS, "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]},
         "help": "the language she speaks her replies in"},
    ]},
    {"group": "Turn-taking", "fields": [
        {"key": "VAD_BACKEND", "attr": "vad_backend", "type": "select",
         "options": ["silero", "fake"],
         "help": "silero listens for speech on this machine; fake never hears a turn begin"},
        {"key": "VAD_THRESHOLD", "attr": "vad_threshold", "type": "number",
         "step": "0.05", "min": "0", "max": "1", "help": "speech-probability gate (0–1)"},
        {"key": "VAD_ONSET_FRAMES", "attr": "vad_onset_frames", "type": "number",
         "min": "1", "help": "consecutive speech frames to start a turn (debounce)"},
        {"key": "VAD_BARGEIN_FRAMES", "attr": "vad_bargein_frames", "type": "number",
         "min": "1", "help": "consecutive frames to interrupt her — higher rejects key-clatter"},
        {"key": "VAD_CONFIRM", "attr": "vad_confirm", "type": "bool",
         "help": "drop an endpointed utterance the VAD heard no real speech in"},
    ]},
    {"group": "The loop", "fields": [
        {"key": "MASK_LATENCY", "attr": "mask_latency", "type": "bool",
         "help": "play a filler line while the LLM spins up"},
        {"key": "MAX_REPLY_TOKENS", "attr": "max_reply_tokens", "type": "number",
         "help": "a ceiling a reply may think and speak up to, not a target — a "
                 "reasoning model spends its thinking inside it"},
        {"key": "AVATAR_MODEL", "attr": "avatar_model", "type": "select",
         "options": list(MODELS.keys()),
         "help": "miara/kei/ren are the modern female rigs; unknown → hiyori"},
    ]},
    {"group": "Channels", "fields": [
        # One bot, one character (SPEC §10.5): `key_env` names the Config
        # attribute holding the variable this character's bot is actually
        # written in, so the panel in Mia's room edits TELEGRAM_BOT_TOKEN_MIA
        # and the one in Yuri's edits hers — pasting a token here can never
        # take over another companion's chat.
        {"key": "TELEGRAM_BOT_TOKEN", "attr": "telegram_bot_token",
         "key_env": "telegram_bot_token_env", "type": "password",
         "help": "her own @BotFather bot — one bot per companion, never shared "
                 "(Telegram hands a token's updates to a single poller)"},
        {"key": "TELEGRAM_CHAT_ID", "attr": "telegram_chat_id",
          "key_env": "telegram_chat_id_env", "type": "text",
          "help": "the one chat she answers in. Leave empty, save, restart, and "
                  "message the bot once — it replies with the id to paste here"},
        {"key": "TELEGRAM_SEND_NON_TELEGRAM", "attr": "telegram_send_non_telegram",
         "type": "bool",
         "help": "also copy replies from web, voice, CLI and API chats to Telegram"},
    ]},
    {"group": "Desktop window", "fields": [
        {"key": "WINDOW_WIDTH", "attr": "window_width", "type": "number",
         "help": "size of the `--window` desktop-pet window (px)"},
        {"key": "WINDOW_HEIGHT", "attr": "window_height", "type": "number",
         "help": "height of the desktop-pet window (px)"},
        {"key": "WINDOW_ON_TOP", "attr": "window_on_top", "type": "bool",
         "help": "keep the floating avatar above other windows"},
        {"key": "WINDOW_GUI", "attr": "window_gui", "type": "select",
         "options": ["", "qt", "gtk"],
         "help": "engine for --window: auto = qt/Chromium when installed (crisper); gtk = WebKitGTK"},
    ]},
]


# --- the derived half: every other knob the running Config declares ----------
#
# Hidden, because they are not yours to type. `character_id` and the two
# `*_env` names are written by the host as it builds a character's runtime
# (world/host.py), and `connection_api_key` is chosen per request beside a
# connection profile's endpoint — a value in `.env` for any of the four would be
# ignored at best and confusing at worst.
HIDDEN = {"character_id", "telegram_bot_token_env", "telegram_chat_id_env",
          "connection_api_key"}

# Anything whose name ends one of these is rendered write-only: reported as
# configured-or-not, never sent back to the browser, blank means keep.
_SECRET_SUFFIXES = ("_api_key", "_token", "_secret", "_password")

_DIVIDER = re.compile(r"^#\s*-{2,}\s*(?P<title>.*?)\s*-*\s*$")
_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<rest>.*)$")
# `# KEY=value   # help` — a knob documented at its default and left commented,
# so a copied `.env` does not pin a default the config may later move.
_COMMENTED = re.compile(r"^#\s?(?P<key>[A-Z][A-Z0-9_]*)=(?P<rest>.*)$")
_OTHER_GROUP = "Everything else"

_example_cache: dict[str, tuple[str, str]] | None = None


def example_index(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """`.env.example` read as `KEY -> (section, one-line help)`.

    The file is written as prose with the knobs embedded in it: `# --- title ---`
    divides it into sections and most assignments carry a trailing comment that
    is already exactly the one-line help a form wants. Nothing is invented here —
    a key with no trailing comment gets no help, and a key outside every divider
    gets the catch-all group.

    A commented-out `# KEY=value  # help` counts too, below a live line for the
    same key: the rarely-touched knobs are documented that way, at their
    defaults, and prose that merely mentions `KEY=` mid-sentence never outranks
    the assignment it is talking about.
    """
    global _example_cache
    if path is None and _example_cache is not None:
        return _example_cache
    index: dict[str, tuple[str, str]] = {}
    live: set[str] = set()
    try:
        text = (path or EXAMPLE_PATH).read_text(encoding="utf-8")
    except OSError:
        text = ""
    section = _OTHER_GROUP
    for line in text.splitlines():
        divider = _DIVIDER.match(line)
        if divider and divider.group("title"):
            section = divider.group("title").strip()
            continue
        assignment = _ASSIGNMENT.match(line)
        commented = None if assignment else _COMMENTED.match(line)
        match = assignment or commented
        if not match:
            continue
        key = match.group("key")
        if commented and key in live:
            continue
        rest = match.group("rest")
        comment = rest.split("#", 1)[1].strip() if "#" in rest else ""
        if commented and key in index and not comment:
            continue                     # a later mention, not a better line
        index[key] = (section, comment)
        if assignment:
            live.add(key)
    if path is None:
        _example_cache = index
    return index


def _hands_field(field: dict, cfg: Any) -> dict:
    """`MIND_TOOL_ALLOWLIST` as the hands this build actually has (SPEC §26.1).

    Every name, what it does, and — because a hand whose backend is off is
    dropped at load and would otherwise just never fire — whether this
    installation can offer it at all. `.env.example` gives this key no trailing
    comment to derive help from, deliberately, so the help is here too.
    """
    from yurios.mind.hands import EVERY_HAND, describe_hands  # local: mind imports world

    catalogue = describe_hands(cfg)
    return {**field, "type": "multi",
            "options": [hand["name"] for hand in catalogue],
            "option_detail": {hand["name"]: {
                "group": hand["klass"],
                "help": hand["does"],
                **({} if hand["available"]
                   else {"note": f"needs {hand['needs']}, which is off"}),
            } for hand in catalogue},
            "option_groups": dict(CLASS_NOTES),
            # ticking every box saves this, so a hand a later build adds is
            # admitted too; unticking every box saves "", which is none
            "all": EVERY_HAND,
            "help": field.get("help") or
                    "the hands she may use — in a reply, and on her own; "
                    "all ticked means every hand, including new ones; "
                    "none ticked means none, even with MIND_TOOLS_ENABLED on"}


#: What ticking a box in each class actually commits to. One sentence at the head
#: of each half, rather than the word "cheap" repeated down fifteen rows.
CLASS_NOTES = {
    "cheap": "any time — in a reply, and in her own work, including while she "
             "is talking unless her utility model is on this machine",
    "expensive": "in a reply whenever you ask; on her own, only with the room "
                 "empty, the budget under the ceiling, and days between repeats",
}


def _choice(*options: str):
    """A derived `str` that is really one of a few words: a dropdown, not a box.

    `MIND_TOOLS_DURING_CHAT` is the case the rule was written for; the backend
    switches are the same thing — the annotation says `str`, the config's own
    comment says `mcp | fake | off`, and a typo in a text box is a backend that
    silently never loads.
    """
    def enrich(field: dict, _cfg: Any) -> dict:
        return {**field, "type": "select", "options": list(options)}
    return enrich


#: key -> a function that replaces the derived field with a better one. Not a
#: second curated table: the field keeps its `.env.example` section and its
#: neighbours, and only the control changes.
ENRICHED = {"MIND_TOOL_ALLOWLIST": _hands_field,
            "MIND_TOOLS_DURING_CHAT": _choice("auto", "on", "off"),
            "TOOLS_BACKEND": _choice("mcp", "fake", "off"),
            "SEARCH_BACKEND": _choice("off", "searxng", "fake"),
            "SEARCH_SAFESEARCH": _choice("0", "1", "2"),
            "CHAT_IMAGE_INPUT": _choice("auto", "on", "off"),
            "SELFIE_BACKEND": _choice("off", "openrouter", "diffusers", "krea2", "mock"),
            "MIND_SOUL_IN_PROMPTS": _choice("full", "brief", "off"),
            "NOTIFY_BACKEND": _choice("auto", "shell", "libnotify", "off"),
            "DESKTOP_BODY": _choice("vrm", "live2d"),
            "EXPRESSION_DEFAULT": _choice("neutral", "happy", "sad", "surprised",
                                          "shy", "thinking", "playful", "tender")}

#: A derived knob that only means something under one setting of another — the
#: same `relevant_if` the curated voice fields carry, so the panel hides the
#: SearXNG address while search is off rather than asking you to fill it in.
_LOCAL_CAMERA = {"SELFIE_BACKEND": ["diffusers", "krea2"]}
RELEVANT: dict[str, dict[str, list[str]]] = {
    "SEARXNG_URL": {"SEARCH_BACKEND": ["searxng"]},
    "SEARCH_LANGUAGE": {"SEARCH_BACKEND": ["searxng"]},
    "SEARCH_SAFESEARCH": {"SEARCH_BACKEND": ["searxng"]},
    "SELFIE_MODEL": {"SELFIE_BACKEND": ["openrouter"]},
    **{key: _LOCAL_CAMERA for key in (
        "SELFIE_LOCAL_MODEL", "SELFIE_LOCAL_DEVICE", "SELFIE_LOCAL_CPU_OFFLOAD",
        "SELFIE_KREA2_STEPS", "SELFIE_KREA2_CFG", "SELFIE_LLM_PARK",
        "SELFIE_WARM_HEADROOM_GIB", "SELFIE_UNLOAD_AFTER_S")},
    **{key: {"SELFIE_BACKEND": ["diffusers"]} for key in (
        "SELFIE_LOCAL_STEPS", "SELFIE_LOCAL_CFG", "SELFIE_LOCAL_HIRES",
        "SELFIE_LOCAL_HIRES_SCALE", "SELFIE_LOCAL_HIRES_DENOISE")},
    "NOTIFY_BACKEND": {"NOTIFY_ENABLED": ["true"]},
}

#: The derived knobs `.env.example` cannot describe on their own line: each is
#: shipped EMPTY, and an empty value followed by `# help` is read by dotenv as
#: the value itself (see the note above MIND_TOOL_ALLOWLIST in the example).
#: Every other knob is documented there — tests/test_envfile.py holds that — and
#: this only fills a blank, so a line that gains a comment there wins.
HELP: dict[str, str] = {
    "GGUF_REPO": "Hugging Face repo to download a gguf/… model from when its id "
                 "can't name one — blank derives it from the id",
    "SELFIE_LOCAL_MODEL": "path to the .safetensors checkpoint — SDXL (Illustrious "
                          "family) or a Krea 2 export",
    "MIND_TOOL_COOLDOWN_S": "per-hand overrides of the repeat cooldown, "
                            "e.g. web_search=3600,research=604800",
}


def _derived_type(annotation: Any, name: str) -> tuple[str, dict[str, str]]:
    if name.endswith(_SECRET_SUFFIXES):
        return "password", {}
    if annotation is bool:
        return "bool", {}
    if annotation is int:
        return "number", {}
    if annotation is float:
        return "number", {"step": "any"}
    return "text", {}


def _derived_fields(cfg: Any, covered: set[str]) -> list[dict]:
    fields = getattr(type(cfg), "model_fields", None)
    if not fields:                    # a duck-typed stand-in, not a real Config
        return []
    index = example_index()
    out: list[dict] = []
    for name, model_field in fields.items():
        key = name.upper()
        if name in HIDDEN or name in covered:
            continue
        kind, extra = _derived_type(model_field.annotation, name)
        section, help_text = index.get(key, (_OTHER_GROUP, ""))
        entry = {"group": section, "key": key, "attr": name, "type": kind,
                 "help": help_text or HELP.get(key, ""), **extra}
        if key in RELEVANT:
            entry["relevant_if"] = RELEVANT[key]
        enrich = ENRICHED.get(key)
        out.append(enrich(entry, cfg) if enrich else entry)
    return out


def groups_for(cfg: Any, *, key_cfg: Any | None = None) -> list[dict]:
    """The whole table as one running build sees it — the single source of truth
    for the form, for the POST that validates against it, and for the CLI.

    Two rewrites happen here. A curated field with `key_env` writes whichever
    variable this runtime actually reads it from (`world/host.py` resolves those
    per character, SPEC §10.5), so the panel in Mia's room edits her bot and the
    one in Yuri's edits hers. And a field this build has no knob for is dropped
    rather than shown dead: the channels live in the world server's config, and
    the Build #2 desktop app does not have them.
    """
    groups: list[dict] = []
    key_cfg = key_cfg or cfg
    covered: set[str] = set()
    for group in CURATED:
        fields = []
        for field in group["fields"]:
            if not hasattr(cfg, field["attr"]):
                continue
            covered.add(field["attr"])
            key = getattr(key_cfg, field["key_env"], "") if field.get("key_env") else ""
            fields.append({**field, "key": key} if key else field)
        if fields:
            groups.append({"group": group["group"], "fields": fields, "advanced": False})

    derived: dict[str, list[dict]] = {}
    for field in _derived_fields(cfg, covered):
        derived.setdefault(field.pop("group"), []).append(field)
    # `.env.example`'s own order, with the catch-all last however it fell out
    for name, fields in derived.items():
        if name != _OTHER_GROUP:
            groups.append({"group": name, "fields": fields, "advanced": True})
    if _OTHER_GROUP in derived:
        groups.append({"group": _OTHER_GROUP, "fields": derived[_OTHER_GROUP],
                       "advanced": True})
    # the page's name for each knob (PAGES, below), on every surface that shows one
    return [{**group, "fields": [{**field, "label": LABELS.get(field["attr"].upper(), "")}
                                 for field in group["fields"]]}
            for group in groups]


def fields_by_key(cfg: Any, *, key_cfg: Any | None = None) -> dict[str, dict]:
    return {field["key"]: field
            for group in groups_for(cfg, key_cfg=key_cfg) for field in group["fields"]}


def display(field: Mapping[str, Any], cfg: Any) -> object:
    """Current effective value for a field, coerced for the form."""
    val = getattr(cfg, field["attr"], "")
    if field["type"] == "bool":
        return bool(val)
    if field["type"] == "number":
        return val
    return "" if val is None else str(val)


def format_value(field: Mapping[str, Any], raw: object) -> str:
    """A submitted value -> the exact text to write after KEY= in `.env`.

    A `multi` is where the validation is, rather than at the far end: an
    unrecognised name in `MIND_TOOL_ALLOWLIST` is dropped at load with a line in
    a log nobody is reading, so a typo saved through either surface would look
    exactly like a hand that quietly never fires. Refused here, it is a sentence
    on the form.
    """
    if field["type"] == "bool":
        return "true" if (raw is True or str(raw).lower() in ("true", "1", "yes")) else "false"
    if field["type"] == "multi":
        given = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        names = list(dict.fromkeys(str(n).strip() for n in given if str(n).strip()))
        options = list(field.get("options") or [])
        unknown = [n for n in names if n not in options]
        if unknown:
            raise ValueError(
                f"{field['key']}: {', '.join(unknown)} — no such "
                f"{'name' if len(unknown) == 1 else 'names'}. "
                f"Choose from: {', '.join(options)}")
        return ",".join(names)
    text = str(raw)
    # quote if a bare value could be mis-parsed (spaces, inline #, =, quotes)
    if text and (text != text.strip() or any(c in text for c in ' #="\'')):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def check(cfg: Any, updates: Mapping[str, str]) -> None:
    """Refuse a save that would leave the installation unable to start.

    `install_owner_security` raises on both of these at boot, and the surface
    that would report the error is the very one being configured — a panel
    reachable over the network, or a daemon the CLI just asked to restart. So
    they are caught here, before the file is written, against the *result* of
    applying this diff rather than against either half of it.
    """
    def resulting(key: str, attr: str) -> str:
        if key in updates:
            return str(updates[key]).strip('"')
        return str(getattr(cfg, attr, "") or "")

    token = resulting("OWNER_TOKEN", "owner_token")
    host = resulting("HOST", "host")
    if token and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"OWNER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters "
            f"(this one is {len(token)}); generate one instead of typing it")
    if not is_loopback(host) and not token:
        raise ValueError(
            f"HOST={host or 'unset'} is not loopback, so OWNER_TOKEN must be set "
            "— generate one first, or set HOST back to 127.0.0.1")


def update_env(path: Path, updates: Mapping[str, str]) -> list[str]:
    """Upsert KEY=value lines, uncommenting a matching `# KEY=` and preserving the
    rest of the file (comments, order, blank lines). Returns the keys written."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    def matches(line: str, key: str) -> bool:
        stripped = line.lstrip()
        body = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        return body.split("=", 1)[0].strip() == key if "=" in body else False

    for i, line in enumerate(lines):
        for key in list(remaining):
            if matches(line, key):
                lines[i] = f"{key}={remaining.pop(key)}"
                break
    # any keys with no line at all -> append under a header
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# --- set from the settings panel ---")
        for key, val in remaining.items():
            lines.append(f"{key}={val}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(updates)


def apply(cfg: Any, payload: Mapping[str, object], *,
          path: Path | None = None,
          key_cfg: Any | None = None) -> tuple[list[str], list[str]]:
    """Validate a `{KEY: value}` diff against this build's table and write it.

    Returns (written, ignored). A password field is write-only in both
    directions: `None` removes it, a blank string preserves what is there, and
    anything else replaces it. Raises ValueError for a value the table refuses.
    """
    table = fields_by_key(cfg, key_cfg=key_cfg)
    updates: dict[str, str] = {}
    ignored: list[str] = []
    for key, raw in payload.items():
        field = table.get(key)
        if field is None:
            ignored.append(key)
            continue
        if field["type"] == "password":
            if raw is None:                       # explicit remove
                updates[key] = ""
                continue
            if not isinstance(raw, str):
                raise ValueError(f"{key} must be a string or null")
            if not raw.strip():                   # blank means preserve
                continue
        updates[key] = format_value(field, raw)
    check(cfg, updates)
    written = update_env(path or ENV_PATH, updates) if updates else []
    return written, ignored


# --- the settings page's rail (SPEC §11.2) -------------------------------------
#
# The table above answers *what* a knob is. This answers *where you would look
# for it*: the board's House settings page puts a rail of pages down the left,
# and each page is a few titled sections of fields in the order a person reads
# them — the switch that turns a thing on, then what it needs, then how hard it
# runs. `.env.example`'s own sections were written to be read top to bottom by
# someone editing the file, which is why "the hands" holds the web, the camera
# and your uploads; on a page you choose from, those are three different places.
#
# Every entry is (KEY, label). The label is what the page leads with — the key
# stays beside it in mono, because it is still the name in the file and the name
# `yurios settings` takes. An `advanced` section starts folded: it is the knobs
# for the case something is already wrong.
#
# A knob placed nowhere is not lost — `pages_for` files it under "Other", in its
# `.env.example` section, and tests/test_envfile.py fails until it is placed. A
# key this build has no knob for is skipped, the same rule as `groups_for`.
PAGES: list[dict] = [
    {"id": "models", "title": "Models", "icon": "chip",
     "note": "Which language model she thinks with, where it runs, and how hard it thinks.",
     "sections": [
         {"title": "Her models", "fields": [
             ("OPENROUTER_API_KEY", "OpenRouter API key"),
             ("CHAT_MODEL", "Chat model"),
             ("UTILITY_MODEL", "Utility model"),
             ("UTILITY_ENABLED", "Background utility work"),
         ]},
         {"title": "Thinking and length", "fields": [
             ("CHAT_THINKING", "Think before replying"),
             ("CHAT_REASONING_EFFORT", "Reply reasoning effort"),
             ("UTILITY_THINKING", "Think during utility work"),
             ("UTILITY_MAX_TOKENS", "Utility token budget"),
             ("MAX_REPLY_TOKENS", "Reply token ceiling"),
             ("TEMPERATURE", "Temperature"),
             ("CONTEXT_LENGTH", "Context window (tokens)"),
         ]},
         {"title": "Local model servers", "fields": [
             ("LMSTUDIO_BASE_URL", "LM Studio address"),
             ("LMSTUDIO_PRELOAD", "Keep LM Studio models loaded"),
             ("LMSTUDIO_LOAD_TIMEOUT_S", "LM Studio load timeout (s)"),
             ("OLLAMA_BASE_URL", "Ollama address"),
         ]},
         {"title": "Built-in GGUF runner — gguf/… models", "advanced": True, "fields": [
             ("GGUF_FALLBACK", "Fall back to GGUF when LM Studio is away"),
             ("GGUF_REPO", "Hugging Face repo"),
             ("GGUF_QUANT", "Quantisation"),
             ("GGUF_CACHE_DIR", "Download folder"),
             ("GGUF_CONTEXT_LENGTH", "Context window override"),
             ("GGUF_N_GPU_LAYERS", "GPU layers"),
             ("GGUF_N_THREADS", "CPU threads"),
             ("GGUF_FLASH_ATTN", "Flash attention"),
         ]},
     ]},
    {"id": "memory", "title": "Memory", "icon": "memory",
     "note": "How she remembers: the embedder that indexes her Vault, what is recalled "
             "into a turn, and how much room each part of the prompt gets.",
     "sections": [
         {"title": "Embeddings", "fields": [
             ("EMBED_BACKEND", "Embedding backend"),
             ("EMBED_MODEL", "Embedding model"),
             ("EMBED_DIM", "Vector width"),
         ]},
         {"title": "Recall", "fields": [
             ("RETRIEVAL_K", "Memories recalled per turn"),
             ("RETRIEVAL_MIN_SIM", "Minimum similarity"),
             ("HALF_LIFE_DAYS", "Memory half-life (days)"),
             ("RAW_WINDOW_TURNS", "Recent messages kept verbatim"),
             ("SUMMARY_EVERY_N", "Summarise every N turns"),
         ]},
         {"title": "Reference shelf", "fields": [
             ("KNOWLEDGE_K", "Reference chunks per turn"),
             ("KNOWLEDGE_MIN_SCORE", "Minimum relevance"),
             ("KNOWLEDGE_BUDGET_TOKENS", "Shelf budget (tokens)"),
         ]},
         {"title": "Her desk and skills", "fields": [
             ("WORKSPACE_ENABLED", "Her desk"),
             ("WORKSPACE_DIGEST_FILES", "Desk files named in the prompt"),
             ("SKILLS_ENABLED", "Skills"),
         ]},
         {"title": "Prompt budgets", "advanced": True, "fields": [
             ("SYSTEM_BUDGET_TOKENS", "System block ceiling"),
             ("SUMMARY_BUDGET_TOKENS", "Summary budget"),
             ("LOREBOOK_BUDGET_TOKENS", "Lorebook budget"),
             ("GOALS_IN_PROMPT", "Open goals in the prompt"),
         ]},
     ]},
    {"id": "mind", "title": "Mind", "icon": "mind",
     "note": "Her life between conversations: whether she runs, how often she wakes, "
             "when she reaches out to you, and what a day and a night may cost.",
     "sections": [
         {"title": "On and off", "fields": [
             ("MIND_ENABLED", "Mind loop"),
             ("DREAM_ENABLED", "Night consolidation (DREAM)"),
         ]},
         {"title": "Reaching out to you", "fields": [
             ("MIND_INTERRUPT_THRESHOLD", "Interrupt threshold"),
             ("MIND_MAX_INTERRUPTS_PER_DAY", "Most interruptions a day"),
             ("MIND_ACT_THRESHOLD", "Act threshold"),
         ]},
         {"title": "Goals and free time", "fields": [
             ("MIND_GOAL_FILING_ENABLED", "She may file goals of her own"),
             ("MIND_SELF_GOALS_MAX", "Most goals of her own at once"),
             ("MIND_MUSE_COOLDOWN_S", "Free-time interval (s)"),
             ("MIND_INBOX_WAKE", "A document on her desk wakes her"),
             ("MIND_GOAL_MAX_STEPS", "Working ticks per goal"),
             ("MIND_GOAL_MAX_HANDS", "Hands per goal step"),
             ("MIND_CONSIDER_COOLDOWN_S", "Goal reconsider cooldown (s)"),
             ("MIND_DISPATCH_TIMEOUT_S", "Dispatched-work timeout (s)"),
         ]},
         {"title": "Budget", "fields": [
             ("MIND_DAILY_TOKENS", "Daily token budget"),
             ("MIND_DREAM_TICK_TOKENS", "Night tick budget"),
             ("MIND_DREAM_RESEARCH_TOKENS", "Night research budget"),
         ]},
         {"title": "The night", "fields": [
             ("MIND_DREAM_START_HOUR", "Night starts (hour)"),
             ("MIND_DREAM_END_HOUR", "Night ends (hour)"),
             ("MIND_DREAM_RESEARCH_SEARCHES", "Searches per research night"),
             ("MIND_DREAM_RESEARCH_PAGES", "Pages per research night"),
             ("MIND_DREAM_RESEARCH_STEPS", "Steps per research night"),
         ]},
         {"title": "Rhythm", "advanced": True, "fields": [
             ("MIND_ENGAGED_CADENCE_S", "ENGAGED tick interval (s)"),
             ("MIND_IDLE_CADENCE_S", "IDLE tick interval (s)"),
             ("MIND_DORMANT_CADENCE_S", "DORMANT tick interval (s)"),
             ("MIND_DREAM_CADENCE_S", "DREAM tick interval (s)"),
             ("MIND_ENGAGED_TIMEOUT_S", "ENGAGED → IDLE after (s)"),
             ("MIND_IDLE_TIMEOUT_S", "IDLE → DORMANT after (s)"),
         ]},
         {"title": "Her character in the mind's calls", "advanced": True, "fields": [
             ("MIND_SOUL_IN_PROMPTS", "Character card in mind prompts"),
             ("MIND_SOUL_CACHE_S", "Soul cache (s)"),
         ]},
     ]},
    {"id": "hands", "title": "Hands", "icon": "tool",
     "note": "Her tools — the same ones in a reply and in her own work — and the limits "
             "that keep a loop from running away.",
     "sections": [
         {"title": "Which hands", "fields": [
             ("MIND_TOOLS_ENABLED", "Hands"),
             ("TOOLS_BACKEND", "Tool server"),
             ("MIND_TOOL_ALLOWLIST", "Allowed hands"),
             ("MIND_TOOLS_DURING_CHAT", "Keep working while you talk"),
             ("MCP_SERVERS", "Third-party MCP servers"),
         ]},
         {"title": "Limits", "fields": [
             ("TOOL_MAX_CALLS_PER_TURN", "Calls per reply"),
             ("MIND_TOOL_CALLS_PER_DAY", "Calls a day on her own"),
             ("MIND_TOOL_PRESSURE_CEILING", "Budget pressure ceiling"),
             ("MIND_TOOL_COOLDOWN_CHEAP_S", "Repeat cooldown, cheap hands (s)"),
             ("MIND_TOOL_COOLDOWN_EXPENSIVE_S", "Repeat cooldown, expensive hands (s)"),
             ("MIND_TOOL_COOLDOWN_S", "Per-hand cooldown overrides"),
             ("TOOL_TIMEOUT_S", "Call timeout (s)"),
             ("TIMER_MAX_MINUTES", "Longest timer (minutes)"),
         ]},
         {"title": "Rate limits in conversation — calls per minute", "advanced": True,
          "fields": [
             ("TOOL_RATE_DESK", "Desk and skills"),
             ("TOOL_RATE_SEARCH", "Web search"),
             ("TOOL_RATE_READ", "Read a page"),
             ("TOOL_RATE_RESEARCH", "Research"),
             ("TOOL_RATE_SELFIE", "Selfie"),
             ("TOOL_RATE_PICTURE", "Show a picture"),
             ("TOOL_RATE_TIMER", "Timer"),
             ("TOOL_RATE_MUSIC", "Music"),
             ("TOOL_RATE_GOAL", "Goal creation"),
             ("TOOL_RATE_SELFEDIT", "Self-edit proposals"),
             ("TOOL_RATE_EXTERNAL", "Third-party tools"),
         ]},
         {"title": "Rate limits on her own — calls per minute", "advanced": True,
          "fields": [
             ("TOOL_RATE_MIND_DESK", "Desk and skills"),
             ("TOOL_RATE_MIND_WEB", "Web"),
             ("TOOL_RATE_MIND_CAMERA", "Camera"),
             ("TOOL_RATE_MIND_OTHER", "Everything else"),
         ]},
     ]},
    {"id": "web", "title": "Web", "icon": "globe",
     "note": "Searching and reading the web. Off by default: search needs a SearXNG "
             "instance, which `./install.sh --web-search` sets up.",
     "sections": [
         {"title": "Search", "fields": [
             ("SEARCH_BACKEND", "Search backend"),
             ("SEARXNG_URL", "SearXNG address"),
             ("SEARCH_RESULTS", "Results per search"),
             ("SEARCH_LANGUAGE", "Search language"),
             ("SEARCH_SAFESEARCH", "Safe search"),
         ]},
         {"title": "Reading and research", "fields": [
             ("RESEARCH_MAX_PAGES", "Pages per research run"),
             ("RESEARCH_MAX_CALLS", "Model calls per research run"),
             ("FETCH_TIMEOUT_S", "Page timeout (s)"),
             ("FETCH_MAX_BYTES", "Largest page (bytes)"),
         ]},
     ]},
    {"id": "pictures", "title": "Pictures", "icon": "camera",
     "note": "Her camera — the pictures she takes — and the pictures you send her.",
     "sections": [
         {"title": "Her camera", "fields": [
             ("SELFIE_BACKEND", "Camera backend"),
             ("SELFIE_MODEL", "Hosted image model"),
             ("SELFIE_DIR", "Gallery folder"),
         ]},
         {"title": "Local renderer", "fields": [
             ("SELFIE_LOCAL_MODEL", "Checkpoint file"),
             ("SELFIE_LOCAL_DEVICE", "Device"),
             ("SELFIE_LOCAL_STEPS", "Steps"),
             ("SELFIE_LOCAL_CFG", "Guidance"),
             ("SELFIE_LOCAL_HIRES", "Hires pass"),
             ("SELFIE_LOCAL_HIRES_SCALE", "Hires scale"),
             ("SELFIE_LOCAL_HIRES_DENOISE", "Hires denoise"),
             ("SELFIE_KREA2_STEPS", "Krea 2 steps"),
             ("SELFIE_KREA2_CFG", "Krea 2 guidance"),
         ]},
         {"title": "Sharing the GPU with her brain", "fields": [
             ("SELFIE_LLM_PARK", "Park her model during a render"),
             ("SELFIE_WARM_HEADROOM_GIB", "VRAM to leave free (GiB)"),
             ("SELFIE_UNLOAD_AFTER_S", "Unload the pipeline after (s)"),
             ("SELFIE_LOCAL_CPU_OFFLOAD", "CPU offload"),
         ]},
         {"title": "Pictures you send her", "fields": [
             ("CHAT_IMAGE_INPUT", "Accept pictures"),
             ("CHAT_IMAGE_MAX_PX", "Resize to (px, long side)"),
             ("UPLOAD_MAX_BYTES", "Largest upload (bytes)"),
             ("UPLOAD_KEEP", "Uploads kept"),
             ("UPLOAD_DIR", "Upload folder"),
         ]},
         {"title": "Scene library", "advanced": True, "fields": [
             ("SELFIE_TEMPLATES_EXTRA", "Extra scenes (YAML)"),
             ("SELFIE_TEMPLATES", "Replacement library (YAML)"),
             ("SELFIE_CHARACTER", "House appearance file"),
         ]},
     ]},
    {"id": "voice", "title": "Voice", "icon": "wave",
     "note": "Hearing you, speaking back, and knowing when it is her turn.",
     "sections": [
         {"title": "Hearing you", "fields": [
             ("STT_BACKEND", "Speech-to-text"),
             ("STT_MODEL", "Whisper model"),
             ("STT_COMPUTE", "Compute type"),
         ]},
         {"title": "Her voice", "fields": [
             ("TTS_BACKEND", "Text-to-speech"),
             ("TTS_REGISTER", "Kokoro voice"),
             ("TTS_SAMPLE_RATE", "Sample rate (Hz)"),
             ("QWEN_MODE", "Qwen mode"),
             ("QWEN_MODEL", "Qwen model"),
             ("QWEN_REF_AUDIO", "Clone reference audio"),
             ("QWEN_REF_TEXT", "Clone reference transcript"),
             ("QWEN_INSTRUCT", "Voice description"),
             ("QWEN_LANGUAGE", "Language"),
             ("QWEN_DEVICE", "Device"),
             ("QWEN_DTYPE", "Precision"),
             ("QWEN_ATTN", "Attention kernel"),
             ("SOVITS_BASE_URL", "GPT-SoVITS address"),
             ("SOVITS_REF_AUDIO", "Reference audio"),
             ("SOVITS_PROMPT_TEXT", "Reference transcript"),
             ("SOVITS_PROMPT_LANG", "Reference language"),
             ("SOVITS_TEXT_LANG", "Reply language"),
         ]},
         {"title": "Turn-taking", "fields": [
             ("VAD_BACKEND", "Voice activity detector"),
             ("VAD_THRESHOLD", "Speech threshold"),
             ("VAD_MIN_SILENCE_MS", "End-of-turn pause (ms)"),
             ("VAD_ONSET_FRAMES", "Frames to start a turn"),
             ("VAD_BARGEIN_FRAMES", "Frames to interrupt her"),
             ("VAD_CONFIRM", "Drop turns with no real speech"),
             ("MASK_LATENCY", "Filler line while she thinks"),
         ]},
         {"title": "Loading", "fields": [
             ("VOICE_PRELOAD", "Load voice at start"),
             ("VOICE_UNLOAD_AFTER_S", "Unload after the room empties (s)"),
         ]},
         {"title": "Voice connection limits", "advanced": True, "fields": [
             ("VOICE_WS_MAX_CONNECTIONS", "Connections at once"),
             ("VOICE_WS_INITIAL_TIMEOUT_S", "Hello timeout (s)"),
             ("VOICE_WS_IDLE_TIMEOUT_S", "Idle timeout (s)"),
             ("VOICE_WS_HEARTBEAT_S", "Heartbeat (s)"),
             ("VOICE_WS_MAX_UTTERANCE_S", "Longest utterance (s)"),
             ("VOICE_WS_MAX_FRAME_BYTES", "Largest frame (bytes)"),
             ("VOICE_WS_MAX_MESSAGE_BYTES", "Largest message (bytes)"),
             ("VOICE_WS_MAX_QUEUE", "Queue length"),
             ("FRAME_MS", "Audio frame (ms)"),
         ]},
     ]},
    {"id": "room", "title": "Room & body", "icon": "room",
     "note": "The room she lives in, the small things her body does on its own, and "
             "the floating desktop window.",
     "sections": [
         {"title": "The room", "fields": [
             ("RAIN_INTENSITY", "Rain"),
             ("EXPRESSION_DEFAULT", "Resting face"),
         ]},
         {"title": "Idle life", "fields": [
             ("IDLE_SETTLE_S", "Quiet after a turn (s)"),
             ("IDLE_ACT_MIN_S", "Reflex, soonest (s)"),
             ("IDLE_ACT_MAX_S", "Reflex, latest (s)"),
             ("IDLE_TALK_MIN_S", "Murmur, soonest (s)"),
             ("IDLE_TALK_MAX_S", "Murmur, latest (s)"),
         ]},
         {"title": "Desktop window", "fields": [
             ("DESKTOP_BODY", "Body in the window"),
             ("AVATAR_MODEL", "Live2D rig"),
             ("WINDOW_WIDTH", "Width (px)"),
             ("WINDOW_HEIGHT", "Height (px)"),
             ("WINDOW_ON_TOP", "Always on top"),
             ("WINDOW_GUI", "Window engine"),
             ("TRAY_ENABLED", "Tray icon"),
         ]},
     ]},
    {"id": "channels", "title": "Channels", "icon": "message",
     "note": "Ways to reach her from outside the browser, and how she reaches you.",
     "sections": [
         {"title": "Telegram", "fields": [
             ("TELEGRAM_BOT_TOKEN", "Bot token"),
             ("TELEGRAM_CHAT_ID", "Chat id"),
             ("TELEGRAM_SEND_NON_TELEGRAM", "Copy other chats to Telegram"),
             ("TELEGRAM_CHARACTER", "Who keeps this bot"),
         ]},
         {"title": "Desktop notifications", "fields": [
             ("NOTIFY_ENABLED", "Notify on the desktop"),
             ("NOTIFY_BACKEND", "Notifier"),
         ]},
     ]},
    {"id": "access", "title": "Access", "icon": "lock",
     "note": "Who can reach her. Local-only is the default; opening her to your "
             "network needs an owner token, and a phone pairs by scanning a code.",
     "sections": [
         {"title": "Network", "fields": [
             ("HOST", "Listen address"),
             ("PORT", "Port"),
         ]},
         {"title": "Owner token and pairing", "fields": [
             ("OWNER_TOKEN", "Owner token"),
         ]},
     ]},
    {"id": "system", "title": "System", "icon": "folder",
     "note": "Where the house keeps its files, how big its logs may grow, and the "
             "knobs for debugging.",
     "sections": [
         {"title": "Where things live", "fields": [
             ("DATA_DIR", "Data folder"),
             ("VAULT_DIR", "Legacy Vault (import only)"),
             ("SOUL_SRC", "Legacy SOUL source"),
             ("CORPUS_DIR", "Corpus folder"),
             ("TRACE_DIR", "Traces folder"),
             ("TOOL_LOG_DIR", "Tool log folder"),
         ]},
         {"title": "Prompt capture", "fields": [
             ("MIND_PROMPT_CAPTURE", "Keep assembled prompts"),
             ("MIND_PROMPT_MAX_CHARS", "Longest message kept"),
         ]},
         {"title": "Log sizes", "advanced": True, "fields": [
             ("MIND_TRACE_MAX_BYTES", "Tick traces (bytes)"),
             ("MIND_ACTIVITY_LOG_MAX_BYTES", "Activity log (bytes)"),
             ("MIND_SIGNAL_MAX_BYTES", "Signal log (bytes)"),
             ("MIND_PROMPT_LOG_MAX_BYTES", "Prompt log (bytes)"),
             ("TOOL_LOG_MAX_BYTES", "Tool log (bytes)"),
         ]},
         {"title": "Developer", "advanced": True, "fields": [
             ("MIND_SEED", "Mind random seed"),
             ("COMPANION_NAME", "Single-companion name"),
         ]},
     ]},
]

#: The one field that sits above the rail rather than on a page: who she is
#: talking to. The overview leads with it, as the board does when it is unset.
OVERVIEW_FIELDS = [("USER_NAME", "Your name")]

LABELS: dict[str, str] = {key: label
                          for page in PAGES for section in page["sections"]
                          for key, label in section["fields"]}
LABELS.update(OVERVIEW_FIELDS)


def default_of(field: Mapping[str, Any], cfg: Any) -> object:
    """The value a knob takes with no line in `.env` — `None` when there is no
    single answer: a secret, or a key resolved per character (§10.5)."""
    if field["type"] == "password" or field.get("key_env"):
        return None
    model_field = getattr(type(cfg), "model_fields", {}).get(field["attr"])
    if model_field is None or model_field.default is None:
        return None
    return display(field, SimpleNamespace(**{field["attr"]: model_field.default}))


def pages_for(cfg: Any, *, key_cfg: Any | None = None) -> dict:
    """The same table as `groups_for`, laid out as the settings page's rail.

    Regrouping only: every field comes out of `groups_for`, so the page cannot
    offer a knob the panel and the CLI do not, or type one differently.
    """
    by_name: dict[str, tuple[dict, str]] = {}
    for group in groups_for(cfg, key_cfg=key_cfg):
        for field in group["fields"]:
            by_name[field["attr"].upper()] = (field, group["group"])
    placed: set[str] = set()

    def take(entries) -> list[dict]:
        out = []
        for name, _label in entries:
            if name in by_name and name not in placed:
                placed.add(name)
                out.append(by_name[name][0])
        return out

    overview = take(OVERVIEW_FIELDS)
    pages = []
    for page in PAGES:
        sections = [{"title": section["title"], "advanced": bool(section.get("advanced")),
                     "fields": fields}
                    for section in page["sections"]
                    if (fields := take(section["fields"]))]
        if sections:
            pages.append({"id": page["id"], "title": page["title"], "icon": page["icon"],
                          "note": page["note"], "sections": sections})
    leftover: dict[str, list[dict]] = {}
    for name, (field, section_title) in by_name.items():
        if name not in placed:
            leftover.setdefault(section_title, []).append(field)
    if leftover:
        pages.append({"id": "other", "title": "Other", "icon": "dots",
                      "note": "Knobs this page has no place for yet, under the "
                              "section .env.example files them in.",
                      "sections": [{"title": title, "advanced": False, "fields": fields}
                                   for title, fields in leftover.items()]})
    return {"overview": overview, "pages": pages}
