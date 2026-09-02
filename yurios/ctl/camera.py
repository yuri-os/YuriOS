"""Gallery, selfie, and picture commands (SPEC §7.6, §36)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .client import HostClient, HostDown, HostError, character_path, connect, fail
from .util import add_json, emit


def register(sub: argparse._SubParsersAction) -> None:
    gallery = sub.add_parser("gallery", help="list, fetch, or rate her pictures")
    gsub = gallery.add_subparsers(dest="gallery_command", required=True)

    list_p = gsub.add_parser("list", help="list pictures, newest first")
    list_p.add_argument("id", help="character id")
    list_p.add_argument("--page", type=int, default=0)
    add_json(list_p)
    list_p.set_defaults(func=command_gallery_list)

    fetch_p = gsub.add_parser("fetch", help="download one picture")
    fetch_p.add_argument("id", help="character id")
    fetch_p.add_argument("name", help="file name on the shelf")
    fetch_p.add_argument("-o", "--output", help="destination path")
    fetch_p.set_defaults(func=command_gallery_fetch)

    rate_p = gsub.add_parser("rate", help="score one picture 1–10, or 0 to clear")
    rate_p.add_argument("id", help="character id")
    rate_p.add_argument("name", help="file name on the shelf")
    rate_p.add_argument("score", help="1-10, or 0/none to clear")
    add_json(rate_p)
    rate_p.set_defaults(func=command_gallery_rate)

    selfie = sub.add_parser("selfie", help="ask her camera for a picture of her")
    selfie.add_argument("id", help="character id")
    selfie.add_argument("--look", default="")
    selfie.add_argument("--scene", default="")
    selfie.add_argument("--framing", default="")
    selfie.add_argument("--lighting", default="")
    selfie.add_argument("--mood", default="")
    selfie.add_argument("--wardrobe", default="")
    selfie.add_argument("--avoid", default="")
    selfie.add_argument("--wait", dest="wait", action="store_true", default=None,
                        help="wait for the PNG (default on a TTY)")
    selfie.add_argument("--no-wait", dest="wait", action="store_false",
                        help="print the id and exit")
    selfie.add_argument("-o", "--output", help="write the PNG here after it lands")
    add_json(selfie)
    selfie.set_defaults(func=command_selfie)

    picture = sub.add_parser("picture", help="ask her camera for a picture of something else")
    picture.add_argument("id", help="character id")
    picture.add_argument("--subject", required=True, help="what the picture is of")
    picture.add_argument("--avoid", default="")
    picture.add_argument("--wait", dest="wait", action="store_true", default=None)
    picture.add_argument("--no-wait", dest="wait", action="store_false")
    picture.add_argument("-o", "--output", help="write the PNG here after it lands")
    add_json(picture)
    picture.set_defaults(func=command_picture)


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


def _should_wait(args: argparse.Namespace) -> bool:
    if args.wait is not None:
        return bool(args.wait)
    return sys.stdout.isatty()


@_run
def command_gallery_list(host: HostClient, args: argparse.Namespace) -> int:
    try:
        payload = host.json("GET", character_path(args.id, "gallery"),
                            params={"page": args.page})
        items = list(payload.get("items") or [])
        source = "gallery"
    except HostError as exc:
        if exc.status not in (404, 503):
            raise
        payload = host.json("GET", character_path(args.id, "selfies"))
        items = list(payload.get("selfies") or [])
        source = "selfies"
        payload = {"items": items, "source": source}
    if args.as_json:
        emit(payload, as_json=True, text="")
        return 0
    if not items:
        print("no pictures")
        return 0
    for item in items:
        name = item.get("name") or item.get("image") or "?"
        caption = item.get("caption") or ""
        score = item.get("score")
        extra = f"  {caption}" if caption else ""
        rated = f"  {score}/10" if score is not None else ""
        print(f"{name}{rated}{extra}")
    if payload.get("has_more"):
        print(f"more: yurios gallery list {args.id} --page {args.page + 1}")
    return 0


@_run
def command_gallery_fetch(host: HostClient, args: argparse.Namespace) -> int:
    response = host.get(character_path(args.id, f"selfies/{args.name}"))
    dest = Path(args.output or args.name)
    dest.write_bytes(response.content)
    print(f"wrote {dest} ({len(response.content)} bytes)")
    return 0


@_run
def command_gallery_rate(host: HostClient, args: argparse.Namespace) -> int:
    raw = str(args.score).strip().lower()
    score: int | None
    if raw in ("0", "none", "null", "clear"):
        score = None
    else:
        score = int(raw)
    result = host.json("POST", character_path(args.id, "gallery/rate"),
                       json={"name": args.name, "score": score})
    emit(result, as_json=args.as_json,
         text=f"rated {args.name} {score if score is not None else 'cleared'}")
    return 0


def _wait_for_shot(host: HostClient, character_id: str, shot_id: str,
                   timeout: float = 180.0) -> dict[str, Any]:
    for event in host.iter_sse(character_path(character_id, "events"),
                               timeout=timeout):
        if event.get("type") != "selfie_status":
            continue
        if str(event.get("id") or "") != shot_id:
            continue
        state = event.get("state")
        if state == "done":
            return event
        if state in ("error", "cancelled"):
            raise HostError(f"render {state}: {shot_id}", status=502)
    raise HostError(f"timed out waiting for {shot_id}", status=504)


def _latest_name(host: HostClient, character_id: str) -> str | None:
    try:
        payload = host.json("GET", character_path(character_id, "gallery"),
                            params={"limit": 1})
        items = payload.get("items") or []
        if items:
            return str(items[0].get("name") or "") or None
    except HostError:
        pass
    payload = host.json("GET", character_path(character_id, "selfies"))
    shots = payload.get("selfies") or []
    if shots:
        return str(shots[0].get("name") or "") or None
    return None


def _finish_shot(host: HostClient, args: argparse.Namespace, result: dict[str, Any],
                 kind: str) -> int:
    shot_id = str(result.get("id") or "")
    if not _should_wait(args):
        emit(result, as_json=args.as_json,
             text=f"{kind} {shot_id} started")
        return 0
    print(f"waiting for {kind} {shot_id}…", file=sys.stderr)
    _wait_for_shot(host, args.id, shot_id)
    name = _latest_name(host, args.id)
    if args.output and name:
        png = host.get(character_path(args.id, f"selfies/{name}"))
        Path(args.output).write_bytes(png.content)
        print(f"wrote {args.output}")
    emit({**result, "name": name, "status": "done"},
         as_json=args.as_json,
         text=f"{kind} {shot_id} done" + (f" ({name})" if name else ""))
    return 0


@_run
def command_selfie(host: HostClient, args: argparse.Namespace) -> int:
    body = {key: getattr(args, key) for key in
            ("look", "scene", "framing", "lighting", "mood", "wardrobe", "avoid")
            if getattr(args, key)}
    result = host.json("POST", character_path(args.id, "selfie"), json=body,
                       timeout=30.0)
    return _finish_shot(host, args, result, "selfie")


@_run
def command_picture(host: HostClient, args: argparse.Namespace) -> int:
    body = {"subject": args.subject, "avoid": args.avoid}
    result = host.json("POST", character_path(args.id, "picture"), json=body,
                       timeout=30.0)
    return _finish_shot(host, args, result, "picture")
