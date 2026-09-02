"""Studio-shaped CLI: get/set fields, optimize, improve her setting (SPEC §30.6, §36)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from yurios.characters.studio import Draft

from .client import HostClient, HostDown, HostError, character_path, connect, fail
from .util import add_json, emit

# Draft slots the studio will accept, minus nothing — `description` is derived
# and refused on set. Profile/loop keys go through the other routes.
DRAFT_FIELDS = tuple(Draft.__slots__)
PROFILE_FIELDS = (
    "name", "model", "utility_model", "voice", "connection_profile",
    "body_backend", "body_model", "enabled", "autostart",
)
CONTROL_FIELDS = ("mind", "utility", "dream", "hands", "notify")
BOOL_FIELDS = frozenset(CONTROL_FIELDS + ("enabled", "autostart"))
LIST_FIELDS = frozenset({
    "tags", "drives", "alternate_greetings", "group_only_greetings", "examples",
})
DERIVED_FIELDS = frozenset({"description"})


def field_help() -> str:
    draft = ", ".join(DRAFT_FIELDS)
    profile = ", ".join(PROFILE_FIELDS)
    controls = ", ".join(CONTROL_FIELDS)
    return (
        f"draft: {draft}\n"
        f"profile: {profile}\n"
        f"loops: {controls}\n"
        "place: setting\n"
        "description is derived from identity/history/appearance/manner and cannot be set."
    )


def register_on(sub: argparse._SubParsersAction) -> None:
    get_p = sub.add_parser("get", help="read one card or profile field")
    get_p.add_argument("id", help="character id")
    get_p.add_argument("field", nargs="?", help="field name; omit to list them")
    add_json(get_p)
    get_p.set_defaults(func=command_get)

    set_p = sub.add_parser("set", help="write one card or profile field")
    set_p.add_argument("id", help="character id")
    set_p.add_argument("field", help="field name")
    set_p.add_argument("value", nargs="?", help="new value; omit when using --file")
    set_p.add_argument("--file", dest="file", help="read the value from this file")
    add_json(set_p)
    set_p.set_defaults(func=command_set)

    opt = sub.add_parser("optimize", help="re-file her card with a model (proposes only)")
    opt.add_argument("id", help="character id")
    opt.add_argument("--instructions", default="", help="preference notes for the model")
    opt.add_argument("--model", default="", help="LiteLLM id; default is her utility model")
    opt.add_argument("--apply", action="store_true",
                     help="write the proposed draft; without this, only print the diff")
    add_json(opt)
    opt.set_defaults(func=command_optimize)

    improve = sub.add_parser("improve-setting",
                             help="propose a better 'where she is' from her card")
    improve.add_argument("id", help="character id")
    improve.add_argument("--model", default="", help="LiteLLM id; default is her utility model")
    improve.add_argument("--apply", action="store_true",
                         help="write the proposed setting")
    add_json(improve)
    improve.set_defaults(func=command_improve_setting)


def command_get(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            return _get(host, args)
    except (HostDown, HostError) as exc:
        return fail(exc)


def command_set(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            return _set(host, args)
    except (HostDown, HostError) as exc:
        return fail(exc)


def command_optimize(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            return _optimize(host, args)
    except (HostDown, HostError) as exc:
        return fail(exc)


def command_improve_setting(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            return _improve_setting(host, args)
    except (HostDown, HostError) as exc:
        return fail(exc)


def _get(host: HostClient, args: argparse.Namespace) -> int:
    field = (args.field or "").strip()
    if not field:
        emit({"draft": list(DRAFT_FIELDS), "profile": list(PROFILE_FIELDS),
              "loops": list(CONTROL_FIELDS), "place": ["setting"]},
             as_json=args.as_json, text=field_help())
        return 0
    value = _read_field(host, args.id, field)
    emit({"id": args.id, "field": field, "value": value},
         as_json=args.as_json, text=_render_value(field, value))
    return 0


def _render_value(field: str, value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return f"{field}={value}" if not str(value).count("\n") else f"{field}:\n{value}"


def _read_field(host: HostClient, character_id: str, field: str) -> Any:
    if field in DERIVED_FIELDS or field in DRAFT_FIELDS:
        draft = host.json("GET", character_path(character_id, "studio"))["draft"]
        if field not in draft and field not in DERIVED_FIELDS and field not in DRAFT_FIELDS:
            raise HostError(f"unknown field: {field}", status=400)
        return draft.get(field, "")
    if field == "setting":
        return host.json("GET", character_path(character_id, "setting")).get("setting", "")
    if field in PROFILE_FIELDS or field in CONTROL_FIELDS:
        settings = host.json("GET", character_path(character_id, "profile"))["settings"]
        if field not in settings:
            raise HostError(f"unknown field: {field}", status=400)
        return settings[field]
    raise HostError(f"unknown field: {field}\n{field_help()}", status=400)


def _parse_value(field: str, raw: str) -> Any:
    if field in BOOL_FIELDS:
        key = raw.strip().lower()
        if key in ("1", "true", "yes", "on"):
            return True
        if key in ("0", "false", "no", "off"):
            return False
        raise HostError(f"{field} wants true or false, not {raw!r}", status=400)
    if field in LIST_FIELDS:
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HostError(f"{field} is not JSON: {exc}", status=400) from exc
            if not isinstance(parsed, list):
                raise HostError(f"{field} wants a JSON list", status=400)
            return parsed
        return [item.strip() for item in raw.split(",") if item.strip()]
    if field == "lorebook":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HostError(f"lorebook wants JSON: {exc}", status=400) from exc
        if not isinstance(parsed, dict):
            raise HostError("lorebook wants a JSON object", status=400)
        return parsed
    return raw


def _set(host: HostClient, args: argparse.Namespace) -> int:
    field = args.field.strip()
    if field in DERIVED_FIELDS:
        raise HostError("description is derived from the four backbone sections "
                        "and cannot be set", status=400)
    raw = args.value
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
        if field not in LIST_FIELDS and field != "lorebook":
            raw = raw.rstrip("\n")
    if raw is None:
        raise HostError("give a value, or --file", status=400)
    value = _parse_value(field, raw)

    if field in DRAFT_FIELDS:
        payload = host.json("GET", character_path(args.id, "studio"))
        draft = payload["draft"]
        draft[field] = value
        result = host.json("PATCH", character_path(args.id, "studio"),
                           json={"draft": draft})
        emit(result, as_json=args.as_json,
             text=f"saved {field} for {args.id}")
        return 0
    if field == "setting":
        result = host.json("PUT", character_path(args.id, "setting"),
                           json={"setting": value})
        emit(result, as_json=args.as_json,
             text=f"saved setting for {args.id}")
        return 0
    if field in CONTROL_FIELDS:
        result = host.json("PATCH", character_path(args.id, "controls"),
                           json={field: value})
        emit(result, as_json=args.as_json,
             text=f"saved {field} for {args.id}")
        return 0
    if field in PROFILE_FIELDS:
        result = host.json("PATCH", character_path(args.id, "profile"),
                           json={field: value})
        emit(result, as_json=args.as_json,
             text=f"saved {field} for {args.id}")
        return 0
    raise HostError(f"unknown field: {field}\n{field_help()}", status=400)


def _print_changes(changes: list[Any]) -> None:
    if not changes:
        print("no changes")
        return
    for change in changes:
        if not isinstance(change, dict):
            print(f"  {change}")
            continue
        field = change.get("field") or change.get("name") or "?"
        kind = change.get("op") or change.get("kind") or "changed"
        print(f"  {field}: {kind}")


def _optimize(host: HostClient, args: argparse.Namespace) -> int:
    draft = host.json("GET", character_path(args.id, "studio"))["draft"]
    body = {"draft": draft, "character": args.id,
            "instructions": args.instructions, "model": args.model}
    result: dict[str, Any] | None = None
    for event in host.stream_ndjson("POST", "/api/studio/optimize", json=body,
                                    timeout=600.0):
        kind = event.get("event")
        if kind == "pass":
            state = event.get("state")
            label = event.get("label") or event.get("name") or ""
            print(f"  pass {event.get('index')}/{event.get('total')} "
                  f"{state} {label}".rstrip(), file=sys.stderr)
        elif kind == "error":
            raise HostError(str(event.get("message") or "optimize failed"), status=502)
        elif kind == "done":
            result = event.get("result") if isinstance(event.get("result"), dict) \
                else event
    if result is None:
        # A client that didn't stream still gets one object; if the host ignored
        # Accept, fall back to the JSON POST.
        result = host.json("POST", "/api/studio/optimize", json=body, timeout=600.0)
    if args.as_json and not args.apply:
        emit(result, as_json=True, text="")
    else:
        notes = str(result.get("notes") or "").strip()
        if notes:
            print(notes)
        _print_changes(list(result.get("changes") or []))
        if result.get("truncated"):
            print("partial: the model truncated; apply would keep the finished fields")
        if result.get("failed"):
            print(f"failed passes: {result['failed']}")
    if not args.apply:
        print("proposed only — pass --apply to write this draft")
        return 0
    proposed = result.get("draft")
    if not isinstance(proposed, dict):
        raise HostError("optimizer returned no draft", status=502)
    saved = host.json("PATCH", character_path(args.id, "studio"),
                      json={"draft": proposed})
    emit(saved, as_json=args.as_json, text=f"applied optimized draft to {args.id}")
    return 0


def _improve_setting(host: HostClient, args: argparse.Namespace) -> int:
    body = {"model": args.model} if args.model else {}
    result = host.json("POST", character_path(args.id, "setting/derive"),
                       json=body, timeout=180.0)
    place = str(result.get("setting") or "")
    emit(result, as_json=args.as_json, text=place)
    if not args.apply:
        print("proposed only — pass --apply to write this setting")
        return 0
    saved = host.json("PUT", character_path(args.id, "setting"),
                      json={"setting": place})
    emit(saved, as_json=args.as_json, text=f"applied setting for {args.id}")
    return 0
