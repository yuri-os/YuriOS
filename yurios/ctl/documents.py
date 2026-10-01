"""Handing her documents: the shelf and the desk (SPEC §20.1, §34.6, §36.6).

Two places, two gifts. `yurios shelf add` puts a document where conversation
searches it; `yurios desk add` puts it in her desk's inbox, for her to read on
purpose and decide what to do with. The upload is the same either way — one
request per file, so one bad file doesn't cost the rest — and only what the
answer means differs.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from collections.abc import Callable
from pathlib import Path

from .client import HostDown, HostError, character_path, connect, fail
from .util import add_json, emit, here


def register(sub: argparse._SubParsersAction) -> None:
    shelf = sub.add_parser("shelf", help="give a character documents to look things up in")
    ssub = shelf.add_subparsers(dest="shelf_command", required=True)
    add = ssub.add_parser("add", help="put .md, .txt or .pdf files on her shelf")
    _files(add)
    add.set_defaults(func=command_shelf_add)
    list_p = ssub.add_parser("list", help="what is on her shelf")
    list_p.add_argument("id", help="character id")
    add_json(list_p)
    list_p.set_defaults(func=command_shelf_list)

    desk = sub.add_parser("desk", help="hand a character documents to read and act on")
    dsub = desk.add_subparsers(dest="desk_command", required=True)
    add = dsub.add_parser("add", help="put .md, .txt or .pdf files in her desk's inbox")
    _files(add)
    add.set_defaults(func=command_desk_add)
    list_p = dsub.add_parser("list", help="what is on her desk")
    list_p.add_argument("id", help="character id")
    add_json(list_p)
    list_p.set_defaults(func=command_desk_list)


def _files(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", help="character id")
    parser.add_argument("files", nargs="+", type=here, metavar="FILE",
                        help="markdown, plain-text or PDF documents")
    add_json(parser)


def _upload(args: argparse.Namespace, resource: str,
            said: Callable[[Path, dict], str]) -> tuple[int, list[dict]]:
    results: list[dict] = []
    status = 0
    try:
        with connect() as host:
            for path in args.files:
                try:
                    data = path.read_bytes()
                    kind = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain"
                    # A book-length PDF is converted before the host answers.
                    answer = host.json(
                        "POST", character_path(args.id, resource),
                        files={"file": (path.name, data, kind)}, timeout=300.0)
                except HostError as exc:
                    fail(HostError(f"{path.name}: {exc.message}", status=exc.status))
                    status = 1
                    continue
                except OSError as exc:
                    print(f"{path}: {exc.strerror or exc}", file=sys.stderr)
                    status = 1
                    continue
                results.append(answer)
                if not args.as_json:
                    print(said(path, answer))
    except HostDown as exc:
        return fail(exc), results
    if args.as_json:
        emit(results, as_json=True, text="")
    return status, results


def _cost(shelved: dict) -> str:
    if shelved.get("unchanged"):
        return "already there, unchanged"
    calls = shelved.get("calls") or 0
    notes = ", read for notes" if shelved.get("digested") else ""
    verb = "replaced" if shelved.get("replaced") else "shelved"
    return f"{verb} · about {calls} model call{'s' if calls != 1 else ''}{notes}"


def command_shelf_add(args: argparse.Namespace) -> int:
    def said(path: Path, shelved: dict) -> str:
        name = shelved["name"]
        shown = name if name == path.name else f"{path.name} → {name}"
        return f"{shown}  {_cost(shelved)}"

    status, results = _upload(args, "mind/research", said)
    if results and not args.as_json:
        print("She reads it on her next tick while her mind is running.")
    return status


def _when(handed: dict) -> str:
    if not handed.get("noticed"):
        return "her mind is off — mention it to her"
    when = ("she'll read it now" if handed.get("wakes")
            else "she'll read it when you're back")
    return f"replaced · {when}" if handed.get("replaced") else when


def command_desk_add(args: argparse.Namespace) -> int:
    def said(path: Path, handed: dict) -> str:
        return f"{path.name} → {handed['path']}  {_when(handed)}"

    status, _ = _upload(args, "mind/workspace/inbox", said)
    return status


def command_shelf_list(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            payload = host.json("GET", character_path(args.id, "mind/research"))
    except (HostDown, HostError) as exc:
        return fail(exc)
    files = payload.get("files") or []
    if args.as_json:
        emit(files, as_json=True, text="")
        return 0
    if not files:
        print("the shelf is empty")
        return 0
    for f in files:
        when = datetime.datetime.fromtimestamp(f.get("mtime") or 0)
        print(f"{when:%Y-%m-%d %H:%M}  {f.get('bytes', 0):>9,}  {f.get('name')}")
    return 0


def command_desk_list(args: argparse.Namespace) -> int:
    try:
        with connect() as host:
            payload = host.json("GET", character_path(args.id, "mind/workspace"))
    except (HostDown, HostError) as exc:
        return fail(exc)
    files = [f for f in payload.get("files") or [] if not f.get("dir")]
    if args.as_json:
        emit(files, as_json=True, text="")
        return 0
    if not files:
        print("the desk is clear")
        return 0
    for f in sorted(files, key=lambda f: f.get("mtime") or 0, reverse=True):
        when = datetime.datetime.fromtimestamp(f.get("mtime") or 0)
        print(f"{when:%Y-%m-%d %H:%M}  {f.get('bytes', 0):>9,}  {f.get('path')}")
    return 0
