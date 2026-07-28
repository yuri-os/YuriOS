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
from .main import build_server


log = logging.getLogger("world")


def _warn_missing(cfg) -> None:
    """One consolidated line naming the seams .env selects but hasn't installed,
    with the command that fixes all of them. The per-seam warnings still fire from
    desktop.main._graceful as each backend fails to build, but those land minutes
    into a cold voice warmup — this arrives before anything else does."""
    from yurios.doctor import collect
    missing = [c for c in collect(cfg) if not c.ok and not c.advisory]
    if not missing:
        return
    extras = sorted({c.extra for c in missing if c.extra})
    log.warning("%s not installed (%s) — those seams fall back to the fakes. "
                "Fix: pip install -e \".[%s]\"  ·  details: python -m yurios.doctor",
                ", ".join(c.seam for c in missing),
                ", ".join(f"{c.knob}={c.want}" for c in missing),
                ",".join(extras))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m yurios.world")
    ap.add_argument("--window", action="store_true",
                    help="float her on the desktop in a native transparent window (§6.5)")
    ap.add_argument("--body", choices=("vrm", "live2d"), default=None,
                    help="which body --window floats (§6.6; default: DESKTOP_BODY)")
    ap.add_argument("--check", action="store_true",
                    help="print the dependency check (yurios.doctor) and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    cfg = Config()
    if args.check:
        from yurios.doctor import collect, report
        raise SystemExit(1 if report(collect(cfg)) else 0)
    # The heavy backends are opt-in extras and the seams degrade to fakes rather
    # than refusing to boot (§3), so say up front which ones .env selected but
    # can't have — one line before the log fills with warmup chatter. Cheap:
    # find_spec only, nothing heavy is imported.
    _warn_missing(cfg)
    if args.body:
        cfg = cfg.model_copy(update={"desktop_body": args.body})
    if args.window:
        from .window import run
        run(cfg)
        return
    from yurios.characters import CharacterRegistry
    from yurios.migrate import migrate_legacy_data
    from .host import create_host_app

    result = migrate_legacy_data(cfg, cfg.data_dir)
    log.info("data layout 0.2: %s", result.status)
    app = create_host_app(cfg, CharacterRegistry(cfg.data_dir))
    print(f"\n  YuriOS dashboard → http://{cfg.host}:{cfg.port}\n")
    # uvicorn shuts down gracefully on SIGINT, then re-raises it (its
    # capture_signals contract) — swallow that final KeyboardInterrupt so a
    # single Ctrl+C exits clean, no traceback (§10).
    try:
        build_server(app, cfg).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
