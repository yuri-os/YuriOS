"""Character lifecycle from the terminal (SPEC §36)."""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from . import studio
from .client import HostDown, HostError, character_path, connect, fail
from .util import add_json, confirm, emit, prompt


def register(sub: argparse._SubParsersAction) -> None:
    character = sub.add_parser("character", aliases=["characters"],
                               help="create, import, export, and manage characters")
    csub = character.add_subparsers(dest="character_command", required=True)

    list_p = csub.add_parser("list", help="list registered characters")
    add_json(list_p)
    list_p.set_defaults(func=command_list)

    show_p = csub.add_parser("show", help="show one character's summary")
    show_p.add_argument("id", help="character id")
    add_json(show_p)
    show_p.set_defaults(func=command_show)

    create_p = csub.add_parser("create", help="author a new character from a draft")
    create_p.add_argument("--name", help="display name")
    create_p.add_argument("--id", dest="character_id", help="registry id")
    create_p.add_argument("--draft", help="studio draft JSON file")
    create_p.add_argument("--portrait", help="PNG or JPEG to use as her face")
    add_json(create_p)
    create_p.set_defaults(func=command_create)

    import_p = csub.add_parser("import", help="import a SillyTavern V2/V3 card PNG")
    import_p.add_argument("card", help="path to the card PNG")
    add_json(import_p)
    import_p.set_defaults(func=command_import)

    export_p = csub.add_parser("export", help="write her card PNG")
    export_p.add_argument("id", help="character id")
    export_p.add_argument("-o", "--output", help="destination path")
    export_p.add_argument("--spec", choices=("v2", "v3"), help="card spec")
    export_p.add_argument("--no-soul", action="store_true", help="omit the YuriOS SOUL block")
    export_p.add_argument("--acknowledged", action="store_true",
                          help="retry a privacy refusal after reading the overlaps")
    add_json(export_p)
    export_p.set_defaults(func=command_export)

    approve_p = csub.add_parser("approve", help="accept an imported card's review and start her")
    approve_p.add_argument("id", help="character id")
    add_json(approve_p)
    approve_p.set_defaults(func=command_approve)

    start_p = csub.add_parser("start", help="start a reviewed, enabled character")
    start_p.add_argument("id", help="character id")
    add_json(start_p)
    start_p.set_defaults(func=command_start)

    stop_p = csub.add_parser("stop", help="stop a character without archiving her")
    stop_p.add_argument("id", help="character id")
    add_json(stop_p)
    stop_p.set_defaults(func=command_stop)

    archive_p = csub.add_parser("archive", help="stop her and move the tree to data/archives/")
    archive_p.add_argument("id", help="character id")
    archive_p.add_argument("--yes", action="store_true", help="do not ask")
    add_json(archive_p)
    archive_p.set_defaults(func=command_archive)

    archives_p = csub.add_parser("archives", help="list archived characters")
    add_json(archives_p)
    archives_p.set_defaults(func=command_archives)

    unarchive_p = csub.add_parser("unarchive", help="restore an archived character")
    unarchive_p.add_argument("name", help="archive folder name (id-YYYYMMDD-HHMMSS)")
    unarchive_p.add_argument("--id", dest="character_id", help="restore under this id")
    unarchive_p.add_argument("--start", action="store_true", help="enable and start her")
    unarchive_p.add_argument("--yes", action="store_true", help="do not ask")
    add_json(unarchive_p)
    unarchive_p.set_defaults(func=command_unarchive)

    clone_p = csub.add_parser("clone", help="copy the whole companion under a new id")
    clone_p.add_argument("id", help="source character id")
    clone_p.add_argument("--name", help="display name for the copy")
    clone_p.add_argument("--id", dest="character_id", help="registry id for the copy")
    add_json(clone_p)
    clone_p.set_defaults(func=command_clone)

    studio.register_on(csub)


def _run(fn):
    def wrapped(args: argparse.Namespace) -> int:
        try:
            with connect() as host:
                return fn(host, args)
        except (HostDown, HostError) as exc:
            return fail(exc)
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return wrapped


def _summary_line(row: dict[str, Any]) -> str:
    character_id = str(row.get("id") or "?")
    name = str(row.get("name") or character_id)
    state = str(row.get("state") or row.get("runtime_state") or "unknown")
    review = "  review required" if row.get("review_required") else ""
    error = row.get("error")
    err = f"  {error}" if error else ""
    return f"{character_id:16} {name:20} {state}{review}{err}"


def _print_character(row: dict[str, Any]) -> str:
    lines = [_summary_line(row)]
    model = row.get("model")
    if model:
        lines.append(f"  model     {model}")
    loops = row.get("loops") or {}
    if loops:
        on = ", ".join(name for name, enabled in loops.items() if enabled) or "none"
        lines.append(f"  loops     {on}")
    return "\n".join(lines)


@_run
def command_list(host, args: argparse.Namespace) -> int:
    payload = host.json("GET", "/api/characters")
    rows = list(payload.get("characters") or [])
    primary = payload.get("primary")
    if args.as_json:
        emit(payload, as_json=True, text="")
        return 0
    if not rows:
        print("no characters registered")
        return 0
    for row in rows:
        marker = "*" if row.get("id") == primary else " "
        print(f"{marker} {_summary_line(row)}")
    return 0


@_run
def command_show(host, args: argparse.Namespace) -> int:
    payload = host.json("GET", "/api/characters")
    rows = [row for row in payload.get("characters") or [] if row.get("id") == args.id]
    if not rows:
        raise HostError(f"no such character: {args.id}", status=404)
    row = rows[0]
    emit(row, as_json=args.as_json, text=_print_character(row))
    return 0


def _portrait_b64(path: str | None) -> str | None:
    if not path:
        return None
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")


@_run
def command_create(host, args: argparse.Namespace) -> int:
    if args.draft:
        draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
        if isinstance(draft, dict) and "draft" in draft:
            draft = draft["draft"]
        if not isinstance(draft, dict):
            raise HostError("draft file must be a JSON object", status=400)
    else:
        template = host.json("GET", "/api/studio/template")["draft"]
        name = args.name or (sys.stdin.isatty() and prompt("Name")) or ""
        if not name:
            raise HostError("a character needs a name", status=400)
        draft = dict(template)
        draft["name"] = name
        if sys.stdin.isatty() and not args.name:
            description = prompt("Description (optional)")
            if description:
                draft["identity"] = description
    body: dict[str, Any] = {"draft": draft}
    if args.character_id:
        body["character_id"] = args.character_id
    portrait = _portrait_b64(args.portrait)
    if portrait:
        body["portrait"] = portrait
    result = host.json("POST", "/api/characters", json=body)
    row = result.get("character") or result
    emit(result, as_json=args.as_json,
         text=f"created {row.get('id')} ({row.get('name')})")
    return 0


@_run
def command_import(host, args: argparse.Namespace) -> int:
    path = Path(args.card)
    files = {"file": (path.name, path.read_bytes(), "image/png")}
    result = host.json("POST", "/api/characters/import", files=files)
    row = result.get("character") or result
    character_id = row.get("id")
    text = f"imported {character_id} ({row.get('name')})"
    if row.get("review_required"):
        text += f"\nunder review — yurios character approve {character_id}"
    emit(result, as_json=args.as_json, text=text)
    return 0


def _print_export_refusal(payload: Any) -> None:
    if not isinstance(payload, dict):
        print(str(payload), file=sys.stderr)
        return
    print(payload.get("detail") or "export refused", file=sys.stderr)
    overlaps = payload.get("overlaps") or []
    for item in overlaps:
        if not isinstance(item, dict):
            continue
        surface = item.get("surface") or ""
        excerpt = item.get("excerpt") or ""
        print(f"  {surface}: {excerpt}", file=sys.stderr)
    if payload.get("code") == "review_required":
        print("Re-run with --acknowledged after reading the passages.", file=sys.stderr)


@_run
def command_export(host, args: argparse.Namespace) -> int:
    configured = any((args.spec, args.no_soul, args.acknowledged))
    try:
        if configured:
            body = {"acknowledged": bool(args.acknowledged),
                    "include_soul": not args.no_soul}
            if args.spec:
                body["spec"] = args.spec
            response = host.post(character_path(args.id, "export"), json=body)
        else:
            response = host.get(character_path(args.id, "export"))
    except HostError as exc:
        if exc.status == 422:
            _print_export_refusal(exc.payload)
            return 1
        raise
    filename = args.output or f"{args.id}.png"
    dest = Path(filename)
    dest.write_bytes(response.content)
    emit({"id": args.id, "path": str(dest), "bytes": len(response.content)},
         as_json=args.as_json, text=f"wrote {dest} ({len(response.content)} bytes)")
    return 0


@_run
def command_approve(host, args: argparse.Namespace) -> int:
    result = host.json("POST", character_path(args.id, "approve"))
    text = f"approved {args.id}"
    if result.get("started"):
        text += " and started"
    elif result.get("error"):
        text += f" but start failed: {result['error']}"
    emit(result, as_json=args.as_json, text=text)
    return 0


@_run
def command_start(host, args: argparse.Namespace) -> int:
    result = host.json("POST", character_path(args.id, "start"))
    emit(result, as_json=args.as_json, text=f"started {args.id}")
    return 0


@_run
def command_stop(host, args: argparse.Namespace) -> int:
    result = host.json("POST", character_path(args.id, "stop"))
    emit(result, as_json=args.as_json, text=f"stopped {args.id}")
    return 0


@_run
def command_archive(host, args: argparse.Namespace) -> int:
    if not confirm(f"Archive {args.id}? She leaves the board; files survive.",
                   yes=args.yes):
        print("Left alone.")
        return 0
    result = host.json("POST", character_path(args.id, "archive"))
    archive = result.get("archive") or ""
    emit(result, as_json=args.as_json,
         text=f"archived {args.id}" + (f" as {archive}" if archive else ""))
    return 0


@_run
def command_archives(host, args: argparse.Namespace) -> int:
    payload = host.json("GET", "/api/archives")
    rows = list(payload.get("archives") or [])
    if args.as_json:
        emit(payload, as_json=True, text="")
        return 0
    if not rows:
        print("no archives")
        return 0
    for row in rows:
        when = row.get("archived_at") or row.get("stamp") or ""
        print(f"{row.get('name'):28}  {row.get('id')}  {when}")
    return 0


@_run
def command_unarchive(host, args: argparse.Namespace) -> int:
    if not confirm(f"Restore archive {args.name}?", yes=args.yes):
        print("Left alone.")
        return 0
    body: dict[str, Any] = {"start": bool(args.start)}
    if args.character_id:
        body["id"] = args.character_id
    result = host.json("POST", f"/api/archives/{args.name}/restore", json=body)
    row = result.get("character") or {}
    emit(result, as_json=args.as_json,
         text=f"restored {row.get('id') or args.name}")
    return 0


@_run
def command_clone(host, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.name:
        body["name"] = args.name
    if args.character_id:
        body["character_id"] = args.character_id
    result = host.json("POST", character_path(args.id, "clone"), json=body)
    row = result.get("character") or {}
    emit(result, as_json=args.as_json,
         text=f"cloned {args.id} → {row.get('id')} ({row.get('name')})")
    return 0
