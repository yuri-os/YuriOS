"""`python -m yurios.world` — boot the world companion (SPEC §2).

Default: serve on HOST/PORT from .env (§11); open the sanctuary in a browser.
`--window`: set the room aside and float her on the desktop instead — a
            frameless, transparent native window (world/window.py, SPEC §6.5;
            needs the [desktop] extra).
`--body`:   which body the window floats (SPEC §6.6): `vrm` (the 3D stage) or
            `live2d` (the Build #2 client). Default: DESKTOP_BODY
            from .env.
"""
from __future__ import annotations

import argparse
import logging

from .config import Config
from .main import build_server, create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m yurios.world")
    ap.add_argument("--window", action="store_true",
                    help="float her on the desktop in a native transparent window (§6.5)")
    ap.add_argument("--body", choices=("vrm", "live2d"), default=None,
                    help="which body --window floats (§6.6; default: DESKTOP_BODY)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    cfg = Config()
    if args.body:
        cfg = cfg.model_copy(update={"desktop_body": args.body})
    if args.window:
        from .window import run
        run(cfg)
        return
    app = create_app(cfg)
    print(f"\n  world-companion → http://{cfg.host}:{cfg.port}\n")
    # uvicorn shuts down gracefully on SIGINT, then re-raises it (its
    # capture_signals contract) — swallow that final KeyboardInterrupt so a
    # single Ctrl+C exits clean, no traceback (§10).
    try:
        build_server(app, cfg).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
