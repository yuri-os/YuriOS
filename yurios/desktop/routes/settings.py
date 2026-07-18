"""GET/POST /api/settings — a local settings panel over the .env knobs (§11).

The whole config is read once at boot from .env (pydantic-settings, see
desktop/config.py + app/config.py), so this endpoint edits *that file* and the
change takes effect on the next restart — it does not hot-reload a running model
into VRAM. The UI (web/settings.js) says so out loud after a save.

One SCHEMA below is the single source of truth: it drives the form the browser
renders (dropdowns where the value is an enum, text/number/password otherwise)
*and* names which .env key each field writes. Current values are read from the
live Config(), so the form always shows the effective setting (default or the
.env override). Writes are surgical — only the fields the user changed are sent,
and _update_env() upserts them line-by-line so the carefully-written comments in
.env survive.

Localhost-only by default (HOST=127.0.0.1), which is why it's fine to hand the
browser the OPENROUTER_API_KEY for editing; the panel renders it masked.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..avatar_models import MODELS

router = APIRouter()

# .env sits at the project root (this file: desktop/routes/settings.py).
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _require_local(request: Request) -> None:
    """The panel reads/writes secrets (the API key) and edits a file on disk, so
    it only answers loopback callers. That's a no-op on the default HOST=127.0.0.1,
    but keeps the endpoint from becoming network-reachable if HOST=0.0.0.0."""
    host = request.client.host if request.client else None
    try:
        is_local = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:                     # non-IP host (e.g. a unix socket)
        is_local = False
    if not is_local:
        raise HTTPException(status_code=403, detail="settings are local-only")

_LANGS = ["en", "ja", "zh", "ko", "yue", "auto"]
_WHISPER = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]

# Each field: key = the .env name (UPPERCASE); attr = the Config() attribute the
# current value is read from; type ∈ {select,text,password,number,bool}; options
# for a select; suggest = datalist hints for an open combobox; help = one line.
SCHEMA: list[dict] = [
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
        {"key": "CHAT_THINKING", "attr": "chat_thinking", "type": "bool",
         "help": "reply <think> pass — OFF for real-time voice (a reasoning model would stall)"},
        {"key": "UTILITY_THINKING", "attr": "utility_thinking", "type": "bool",
         "help": "extraction/summary <think> pass — ON (off the hot path, quality matters)"},
        {"key": "UTILITY_MAX_TOKENS", "attr": "utility_max_tokens", "type": "number",
         "help": "budget for the utility call's <think> block + JSON — too small loses the fact"},
    ]},
    {"group": "Embeddings", "fields": [
        {"key": "EMBED_BACKEND", "attr": "embed_backend", "type": "select",
         "options": ["lm_studio", "ollama", "sentence_tf"],
         "help": "lm_studio reuses the chat server; a swap at the same dim auto-reindexes"},
        {"key": "EMBED_MODEL", "attr": "embed_model", "type": "text",
         "suggest": ["text-embedding-nomic-embed-text-v1.5", "nomic-embed-text",
                     "BAAI/bge-small-en-v1.5"]},
        {"key": "EMBED_DIM", "attr": "embed_dim", "type": "number",
         "help": "must equal the index vector width (nomic=768, bge-small=384)"},
    ]},
    {"group": "Storage", "fields": [
        {"key": "VAULT_DIR", "attr": "vault_dir", "type": "text",
         "help": "her memory; point at a Build #1 Vault to continue that companion"},
        {"key": "SOUL_SRC", "attr": "soul_src", "type": "text",
         "help": "the SOUL card used to seed a fresh Vault"},
    ]},
    {"group": "Server", "fields": [
        {"key": "HOST", "attr": "host", "type": "text", "suggest": ["127.0.0.1", "0.0.0.0"],
         "help": "127.0.0.1 keeps her local-only"},
        {"key": "PORT", "attr": "port", "type": "number"},
    ]},
    {"group": "Speech-to-text", "fields": [
        {"key": "STT_BACKEND", "attr": "stt_backend", "type": "select",
         "options": ["faster_whisper", "fake"]},
        {"key": "STT_MODEL", "attr": "stt_model", "type": "select", "options": _WHISPER,
         "help": "smaller = lower latency, less accurate"},
    ]},
    {"group": "Text-to-speech", "fields": [
        {"key": "TTS_BACKEND", "attr": "tts_backend", "type": "select",
         "options": ["qwen3_tts", "kokoro", "gpt_sovits", "fake"],
         "help": "the fields below apply to whichever backend you pick"},
        {"key": "TTS_REGISTER", "attr": "tts_register", "type": "text",
         "help": "kokoro voice register only"},
        {"key": "QWEN_MODE", "attr": "qwen_mode", "type": "select",
         "options": ["clone", "design"],
         "help": "qwen3_tts: clone is stable; design re-authors the voice each turn (drifts)"},
        {"key": "QWEN_REF_AUDIO", "attr": "qwen_ref_audio", "type": "text",
         "help": "qwen3_tts clone reference wav; blank = bundled designed.wav"},
        {"key": "QWEN_REF_TEXT", "attr": "qwen_ref_text", "type": "text",
         "help": "exact transcript of the clone reference"},
        {"key": "QWEN_DEVICE", "attr": "qwen_device", "type": "text",
         "suggest": ["cuda:0", "cuda:1", "cpu"]},
        {"key": "SOVITS_BASE_URL", "attr": "sovits_base_url", "type": "text",
         "help": "gpt_sovits api_v2 server url"},
        {"key": "SOVITS_REF_AUDIO", "attr": "sovits_ref_audio", "type": "text",
         "help": "gpt_sovits reference wav (path on the server); blank = bundled designed.wav"},
        {"key": "SOVITS_PROMPT_TEXT", "attr": "sovits_prompt_text", "type": "text",
         "help": "exact transcript of the sovits reference clip"},
        {"key": "SOVITS_PROMPT_LANG", "attr": "sovits_prompt_lang", "type": "select",
         "options": _LANGS},
        {"key": "SOVITS_TEXT_LANG", "attr": "sovits_text_lang", "type": "select",
         "options": _LANGS},
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

# key → field spec, for fast validation on POST
_BY_KEY = {f["key"]: f for g in SCHEMA for f in g["fields"]}


def _display(field: dict, cfg) -> object:
    """Current effective value for a field, coerced for the form."""
    val = getattr(cfg, field["attr"], "")
    if field["type"] == "bool":
        return bool(val)
    if field["type"] == "number":
        return val
    return "" if val is None else str(val)


def _format(field: dict, raw) -> str:
    """A submitted value → the exact text to write after KEY= in .env."""
    if field["type"] == "bool":
        return "true" if (raw is True or str(raw).lower() in ("true", "1", "yes")) else "false"
    s = str(raw)
    # quote if a bare value could be mis-parsed (spaces, inline #, =, quotes)
    if s and (s != s.strip() or any(c in s for c in ' #="\'')):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _update_env(path: Path, updates: dict[str, str]) -> list[str]:
    """Upsert KEY=value lines, uncommenting a matching `# KEY=` and preserving the
    rest of the file (comments, order, blank lines). Returns the keys written."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)

    def _matches(line: str, key: str) -> bool:
        stripped = line.lstrip()
        body = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        return body.split("=", 1)[0].strip() == key if "=" in body else False

    for i, line in enumerate(lines):
        for key in list(remaining):
            if _matches(line, key):
                lines[i] = f"{key}={remaining.pop(key)}"
                break
    # any keys with no line at all → append under a header
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# --- set from the settings panel ---")
        for key, val in remaining.items():
            lines.append(f"{key}={val}")

    path.write_text("\n".join(lines) + "\n")
    return list(updates)


async def _fetch_json(url: str, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(url, headers=headers or {})
        r.raise_for_status()
        return r.json()


@router.get("/api/models")
async def list_models(request: Request, provider: str = ""):
    """The models a provider can actually serve right now, for the settings panel's
    model picker. lm_studio + ollama hit the local server; openrouter hits its
    public catalogue (the key, if set, is sent so private/BYOK models show too).
    Any failure — server down, no such provider — comes back as an empty list plus
    an `error` string the panel renders inline, never a 500 that breaks the dialog."""
    _require_local(request)
    cfg = request.app.state.rt.cfg
    provider = (provider or "").lower()
    try:
        if provider in ("lmstudio", "lm_studio"):
            base = cfg.lmstudio_base_url.rstrip("/")
            data = await _fetch_json(f"{base}/models")
            ids = [m.get("id", "") for m in data.get("data", [])]
        elif provider == "ollama":
            base = cfg.ollama_base_url.rstrip("/")
            data = await _fetch_json(f"{base}/api/tags")
            ids = [m.get("name", "") for m in data.get("models", [])]
        elif provider == "openrouter":
            headers = ({"Authorization": f"Bearer {cfg.openrouter_api_key}"}
                       if cfg.openrouter_api_key else None)
            data = await _fetch_json("https://openrouter.ai/api/v1/models", headers)
            ids = [m.get("id", "") for m in data.get("data", [])]
        else:
            return {"models": [], "error": f"no live listing for '{provider}' — type the id"}
    except Exception as e:                     # unreachable server, bad json, timeout…
        return {"models": [], "error": f"couldn't reach {provider}: {str(e)[:120]}"}
    return {"models": sorted({i for i in ids if i})}


@router.get("/api/settings")
async def get_settings(request: Request):
    _require_local(request)
    cfg = request.app.state.rt.cfg
    return {
        "env_path": str(ENV_PATH),
        "groups": [
            {"group": g["group"],
             "fields": [{**{k: v for k, v in f.items() if k != "attr"},
                         "value": _display(f, cfg)} for f in g["fields"]]}
            for g in SCHEMA
        ],
    }


@router.post("/api/settings")
async def post_settings(request: Request):
    _require_local(request)
    payload = await request.json()
    updates: dict[str, str] = {}
    unknown: list[str] = []
    for key, raw in (payload or {}).items():
        field = _BY_KEY.get(key)
        if field is None:
            unknown.append(key)
            continue
        updates[key] = _format(field, raw)
    written = _update_env(ENV_PATH, updates) if updates else []
    return {"ok": True, "written": written, "ignored": unknown,
            "restart_required": bool(written), "env_path": str(ENV_PATH)}
