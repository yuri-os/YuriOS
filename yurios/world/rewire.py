"""Swapping the model she thinks with, mid-sentence (SPEC §31.2, §31.4).

A character's brain settings — which model answers, which server it is reached
on, whether it thinks before it speaks — are her registry record's, falling back
to the host `.env` field by field. Changing one used to mean rebuilding her whole
runtime: a restart tears down the mind loop, the tool server, the memory index
and the transcript, which is a heavy price for "try the bigger model on this
sentence".

Nothing about a model swap needs any of that. The provider objects
(`LiteLLMChatModel` / `LiteLLMUtilityModel`) are thin: an id, a route, a key, a
reasoning switch. Rebuilding the two of them and pointing the brain's `AppState`
at the new pair is the whole operation — the Vault, the session window, the mind
and her voice never notice, and the next token she speaks comes from the new
model.

Two rules make that safe:

  - **The live `Config` is mutated in place, not replaced.** One object is shared
    by the runtime, the brain, the memory store, the mind and the VRAM parker; a
    `model_copy` would leave every one of them reading the old values.
  - **Only the fields listed here move.** The embedder is not among them — a
    swapped embedding model re-indexes the Vault (§4.3), which is a restart's
    work, not a turn's.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger("world.rewire")

# The knobs a character may take for herself and change without a restart, in the
# order the settings screen shows them. Every one is a Config field name.
BRAIN_FIELDS: tuple[str, ...] = (
    "chat_model", "utility_model",
    "lmstudio_base_url", "ollama_base_url", "openrouter_api_key", "connection_api_key",
    "chat_thinking", "utility_thinking", "utility_max_tokens",
    "temperature", "max_reply_tokens", "context_length",
)

# …of which these are baked into the provider objects at construction, so a change
# to one means building a new pair. The rest are read per call (`temperature`,
# `max_reply_tokens`) or by the loader (`context_length`), and are live the moment
# they land on the Config.
_PROVIDER_FIELDS = frozenset({
    "chat_model", "utility_model", "lmstudio_base_url", "ollama_base_url",
    "openrouter_api_key", "connection_api_key", "chat_thinking", "utility_thinking",
    "utility_max_tokens",
})


# The same knobs as a form: what a character's own brain panel shows, in order.
# `store` says where an override lives in her registry record — the two model ids
# and the connection have named homes (§31.1), the rest ride `models.options`.
# A blank value is not a value: it means *inherit the host's `.env`*, which is
# what keeps one file configuring a house (§11).
OVERRIDE_SCHEMA: tuple[dict[str, Any], ...] = (
    {"key": "chat_model", "store": "chat", "type": "model",
     "help": "her reply voice — hers alone, whatever the rest of the house runs"},
    {"key": "utility_model", "store": "utility", "type": "model",
     "help": "her summaries and fact extraction (off the hot path)"},
    {"key": "chat_thinking", "store": "options", "type": "bool",
     "help": "her reply <think> pass — off keeps a reasoning model real-time"},
    {"key": "utility_thinking", "store": "options", "type": "bool",
     "help": "the utility model's <think> pass"},
    {"key": "temperature", "store": "options", "type": "number", "step": "0.05",
     "help": "how far she wanders from the likeliest word"},
    {"key": "max_reply_tokens", "store": "options", "type": "number",
     "help": "the ceiling on one reply"},
    {"key": "context_length", "store": "options", "type": "number",
     "help": "the window her model is loaded with — 0 = the provider's default"},
)


def snapshot(cfg) -> dict[str, Any]:
    """Every brain field as this Config currently holds it."""
    return {field: getattr(cfg, field) for field in BRAIN_FIELDS}


def coerce(cfg, field: str, value: Any) -> Any:
    """A submitted value as the Config field's own type.

    Registry options and JSON bodies arrive as strings; `model_copy(update=…)`
    does not validate, so a `"0.8"` temperature would reach LiteLLM as text and
    fail the call. Unknown fields and unparseable values raise `ValueError`, which
    the API turns into a 400 rather than a broken brain."""
    info = type(cfg).model_fields.get(field)
    if info is None:
        raise ValueError(f"unknown setting: {field}")
    annotation = info.annotation
    if annotation is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{field} must be true or false")
    if annotation in (int, float):
        try:
            return annotation(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
    return "" if value is None else str(value)


def differences(cfg, wanted: Mapping[str, Any]) -> dict[str, Any]:
    """The brain fields in *wanted* that the live Config does not already hold."""
    out: dict[str, Any] = {}
    for field in BRAIN_FIELDS:
        if field not in wanted:
            continue
        value = coerce(cfg, field, wanted[field])
        if getattr(cfg, field) != value:
            out[field] = value
    return out


def apply(state, cfg, changes: Mapping[str, Any], *, meter=None) -> list[str]:
    """Land *changes* on the live Config and rewire the brain to match.

    `state` is Build #1's `AppState` (or None for an injected test brain, which
    has no providers to swap). Returns the fields that actually moved, so the
    caller can say what it did."""
    changed = dict(changes)
    if not changed:
        return []
    for field, value in changed.items():
        setattr(cfg, field, value)
    if state is not None and _PROVIDER_FIELDS.intersection(changed):
        rebuild(state, cfg, meter=meter)
    return sorted(changed)


def rebuild(state, cfg, *, meter=None) -> None:
    """Build a fresh provider pair from the Config and hand it to the brain.

    Everything that speaks — the reply, the greeting, ambient self-talk, each
    pass of the tool loop — reads `state.chat` at call time, so the swap is
    complete the moment it is assigned. A stream already in flight is holding the
    old provider and finishes on it, which is the only honest way to change a
    model mid-sentence: the words she is saying keep coming from the mind that
    started them.

    The memory store keeps its own reference to the utility model (it is the one
    that updates USER.md and summarises), so it is re-pointed too — miss that and
    her memory would keep using the model she just left."""
    from yurios.app.main import build_chat_model, build_utility_model

    # The context gauge lives on the provider (world/context.py), so a rebuild
    # that forgot it would freeze the masthead readout at the last prompt of the
    # old model. Told one, use it; told none, keep whatever the outgoing one had.
    meter = meter if meter is not None else getattr(state.chat, "meter", None)
    state.chat = build_chat_model(cfg, meter=meter)
    direct_limit = getattr(state.chat, "context_limit", 0)
    if meter is not None and direct_limit:
        meter.set_limit(direct_limit, "direct gguf")
    utility = build_utility_model(cfg) if state.utility is not None else None
    if utility is not None:
        state.utility = utility
        store = getattr(state, "store", None)
        if store is not None and getattr(store, "utility", None) is not None:
            store.utility = utility
    log.info("brain rewired: chat=%s utility=%s", cfg.chat_model,
             cfg.utility_model if utility is not None else "off")
