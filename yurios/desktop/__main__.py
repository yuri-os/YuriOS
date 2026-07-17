"""`python -m desktop` — serve on HOST/PORT from .env (§11).

Default: run the server; open http://HOST:PORT in a browser yourself.
`--window`: render her as a frameless, transparent desktop pet instead
            (desktop/window.py; needs the [desktop] extra).
"""
from __future__ import annotations

import argparse

import uvicorn

from .config import Config
from .main import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m desktop")
    ap.add_argument("--window", action="store_true",
                    help="float her on the desktop in a native transparent window")
    args = ap.parse_args()

    cfg = Config()
    if args.window:
        from .window import run
        run(cfg)
    else:
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
