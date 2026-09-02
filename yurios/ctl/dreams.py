"""Dream job commands (SPEC §21.2, §36)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .client import HostClient, HostDown, HostError, character_path, connect, fail
from .util import add_json, confirm, emit


def register(sub: argparse._SubParsersAction) -> None:
    dream = sub.add_parser("dream", help="run or edit a character's night jobs")
    dsub = dream.add_subparsers(dest="dream_command", required=True)

    status = dsub.add_parser("status", help="what will run tonight")
    status.add_argument("id", help="character id")
    add_json(status)
    status.set_defaults(func=command_status)

    list_p = dsub.add_parser("list", help="job files on disk")
    list_p.add_argument("id", help="character id")
    add_json(list_p)
    list_p.set_defaults(func=command_list)

    show = dsub.add_parser("show", help="print one job file")
    show.add_argument("id", help="character id")
    show.add_argument("job", help="job name")
    add_json(show)
    show.set_defaults(func=command_show)

    run = dsub.add_parser("run", help="run DREAM now")
    run.add_argument("id", help="character id")
    run.add_argument("job", nargs="?", help="one job instead of the whole night")
    run.add_argument("--day", help="YYYY-MM-DD")
    run.add_argument("--dry-run", action="store_true", help="think, write nothing")
    run.add_argument("--verbose", action="store_true", help="print the prompts")
    add_json(run)
    run.set_defaults(func=command_run)

    write = dsub.add_parser("write", help="create or replace a job file")
    write.add_argument("id", help="character id")
    write.add_argument("job", help="job name")
    write.add_argument("--file", required=True, help="markdown with YAML frontmatter")
    add_json(write)
    write.set_defaults(func=command_write)

    delete = dsub.add_parser("delete", help="remove a job file")
    delete.add_argument("id", help="character id")
    delete.add_argument("job", help="job name")
    delete.add_argument("--yes", action="store_true", help="do not ask")
    add_json(delete)
    delete.set_defaults(func=command_delete)


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


@_run
def command_status(host: HostClient, args: argparse.Namespace) -> int:
    payload = host.json("GET", character_path(args.id, "mind/dream"))
    if args.as_json:
        emit(payload, as_json=True, text="")
        return 0
    enabled = "on" if payload.get("enabled") else "off"
    print(f"dream {enabled}  state={payload.get('state')}  "
          f"window={payload.get('window')}")
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            print(f"  {job}")
            continue
        name = job.get("name") or "?"
        flag = "on" if job.get("enabled", True) else "off"
        print(f"  {name:16} {flag}")
    return 0


@_run
def command_list(host: HostClient, args: argparse.Namespace) -> int:
    payload = host.json("GET", character_path(args.id, "mind/dream/jobs"))
    if args.as_json:
        emit(payload, as_json=True, text="")
        return 0
    jobs = payload.get("jobs") or []
    if not jobs:
        print("no job files")
        return 0
    for job in jobs:
        name = job.get("name") or "?"
        builtin = "  builtin" if job.get("builtin") else ""
        title = (job.get("front") or {}).get("title") or ""
        print(f"{name:16}{builtin}  {title}")
    return 0


@_run
def command_show(host: HostClient, args: argparse.Namespace) -> int:
    payload = host.json("GET", character_path(args.id, f"mind/dream/jobs/{args.job}"))
    emit(payload, as_json=args.as_json, text=str(payload.get("text") or ""))
    return 0


def _summarise_run(report: dict[str, Any], *, verbose: bool) -> str:
    lines: list[str] = []
    summary = str(report.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    jobs = report.get("jobs") or report.get("results") or []
    if isinstance(jobs, list):
        for job in jobs:
            if not isinstance(job, dict):
                lines.append(str(job))
                continue
            name = job.get("name") or job.get("job") or "?"
            status = (job.get("result") or job.get("note") or job.get("status")
                      or job.get("ok") or "")
            line = f"{name}: {status}".rstrip(": ")
            if job.get("failed"):
                line += f"  failed: {job['failed']}"
            lines.append(line)
            if verbose:
                for key in ("system", "user", "completion", "output"):
                    if job.get(key):
                        lines.append(f"  {key}:")
                        lines.append(str(job[key]))
    if not lines:
        lines.append(json_fallback(report))
    return "\n".join(lines)


def json_fallback(report: dict[str, Any]) -> str:
    import json
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)[:4000]


@_run
def command_run(host: HostClient, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"dry_run": bool(args.dry_run)}
    if args.job:
        body["job"] = args.job
    if args.day:
        body["day"] = args.day
    result = host.json("POST", character_path(args.id, "mind/dream/run"),
                       json=body, timeout=600.0)
    emit(result, as_json=args.as_json,
         text=_summarise_run(result, verbose=args.verbose))
    return 0


@_run
def command_write(host: HostClient, args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    result = host.json("PUT", character_path(args.id, f"mind/dream/jobs/{args.job}"),
                       json={"text": text})
    emit(result, as_json=args.as_json, text=f"wrote job {args.job}")
    return 0


@_run
def command_delete(host: HostClient, args: argparse.Namespace) -> int:
    if not confirm(f"Delete dream job {args.job}?", yes=args.yes):
        print("Left alone.")
        return 0
    result = host.json("DELETE", character_path(args.id, f"mind/dream/jobs/{args.job}"))
    extra = " (reverted to builtin)" if result.get("reverted") else ""
    emit(result, as_json=args.as_json, text=f"deleted job {args.job}{extra}")
    return 0
