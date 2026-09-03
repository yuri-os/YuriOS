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
        {"key": "PORT", "attr": "port", "type": "number"},
        {"key": "OWNER_TOKEN", "attr": "owner_token", "type": "password",
         "help": "the secret a phone or a second machine comes in with (32+ characters). "
                 "Don't type one — generate it, then pair the device by scanning the code"},
    ]},
    {"group": "Speech-to-text", "fields": [
        {"key": "STT_BACKEND", "attr": "stt_backend", "type": "select",
         "options": ["faster_whisper", "fake"]},
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
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]}},
        {"key": "QWEN_DEVICE", "attr": "qwen_device", "type": "text",
         "suggest": ["cuda:0", "cuda:1", "cpu"],
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]}},
        {"key": "QWEN_DTYPE", "attr": "qwen_dtype", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]}},
        {"key": "QWEN_ATTN", "attr": "qwen_attn", "type": "text",
         "relevant_if": {"TTS_BACKEND": ["qwen3_tts"]}},
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
         "options": _LANGS, "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]}},
        {"key": "SOVITS_TEXT_LANG", "attr": "sovits_text_lang", "type": "select",
         "options": _LANGS, "relevant_if": {"TTS_BACKEND": ["gpt_sovits"]}},
    ]},
    {"group": "Turn-taking", "fields": [
        {"key": "VAD_BACKEND", "attr": "vad_backend", "type": "select",
         "options": ["silero", "fake"]},
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
        {"key": "MAX_REPLY_TOKENS", "attr": "max_reply_tokens", "type": "number"},
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
        {"key": "WINDOW_HEIGHT", "attr": "window_height", "type": "number"},
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
_OTHER_GROUP = "Everything else"

_example_cache: dict[str, tuple[str, str]] | None = None


def example_index(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """`.env.example` read as `KEY -> (section, one-line help)`.

    The file is written as prose with the knobs embedded in it: `# --- title ---`
    divides it into sections and most assignments carry a trailing comment that
    is already exactly the one-line help a form wants. Nothing is invented here —
    a key with no trailing comment gets no help, and a key outside every divider
    gets the catch-all group.
    """
    global _example_cache
    if path is None and _example_cache is not None:
        return _example_cache
    index: dict[str, tuple[str, str]] = {}
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
        if not assignment:
            continue
        rest = assignment.group("rest")
        comment = rest.split("#", 1)[1].strip() if "#" in rest else ""
        index[assignment.group("key")] = (section, comment)
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
    from yurios.mind.hands import describe_hands       # local: mind imports world

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
            "help": field.get("help") or
                    "the hands she may reach for unasked — tick them explicitly; "
                    "empty means none, even with MIND_TOOLS_ENABLED on"}


#: What ticking a box in each class actually commits to. One sentence at the head
#: of each half, rather than the word "cheap" repeated down fifteen rows.
CLASS_NOTES = {
    "cheap": "a step in her goal work, any time she is not talking to you",
    "expensive": "a whole tick's intention: only with the room empty, the budget "
                 "under the ceiling, and days between repeats",
}


#: key -> a function that replaces the derived field with a better one. Not a
#: second curated table: the field keeps its `.env.example` section and its
#: neighbours, and only the control changes.
ENRICHED = {"MIND_TOOL_ALLOWLIST": _hands_field}


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
                 "help": help_text, **extra}
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
    return groups


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
