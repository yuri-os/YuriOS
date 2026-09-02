"""Small terminal helpers the host-client commands share."""
from __future__ import annotations

import json
import sys
from typing import Any


def add_json(parser) -> None:
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print machine-readable JSON instead of a table")


def emit(data: Any, *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(text, end="" if text.endswith("\n") or not text else "\n")


def confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing unattended action; rerun with --yes.", file=sys.stderr)
        return False
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default
