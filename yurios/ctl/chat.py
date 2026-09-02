"""`yurios chat` — the terminal channel (SPEC §10.5, §36)."""
from __future__ import annotations

import argparse
import asyncio
import os

from yurios.world.config import Config


def register(sub: argparse._SubParsersAction) -> None:
    chat = sub.add_parser("chat", help="talk to a character in the terminal")
    chat.add_argument("character", nargs="?", help="character id to enter")
    chat.add_argument("-m", "--message", help="send one turn and exit")
    chat.add_argument("--new", action="store_true", help="start a fresh conversation")
    chat.add_argument("--model", help="set this card's reply model first")
    chat.add_argument("--utility-model", help="set this card's utility model first")
    chat.set_defaults(func=command_chat)


def command_chat(args: argparse.Namespace) -> int:
    from yurios.chat.__main__ import main as chat_main

    cfg = Config()
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    argv = [f"--url=http://{host}:{cfg.port}"]
    if cfg.owner_token and not os.environ.get("YURIOS_OWNER_TOKEN"):
        os.environ["YURIOS_OWNER_TOKEN"] = cfg.owner_token
    if args.character:
        argv += ["--character", args.character]
    if args.message is not None:
        argv += ["--message", args.message]
    if args.new:
        argv.append("--new")
    if args.model:
        argv += ["--model", args.model]
    if getattr(args, "utility_model", None):
        argv += ["--utility-model", args.utility_model]
    return asyncio.run(chat_main(argv))
