"""Puppet demo (SPEC §4) — prove the body without the brain.

Boots the server with fake voice backends, no tools and no mind, then
drives the in-process `VrmController` through a scripted routine: expressions,
gaze, bone offsets, rain, music. Open the printed URL, click into the room, and
watch. This is the Phase-1 acceptance check: if she moves here, the event bus
(/api/events) and the whole stage are wired.

    ./.venv/bin/python scripts/demo_avatar.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TTS_BACKEND", "fake")
os.environ.setdefault("STT_BACKEND", "fake")
os.environ.setdefault("VAD_BACKEND", "fake")
os.environ.setdefault("TOOLS_BACKEND", "off")
os.environ.setdefault("MIND_ENABLED", "false")

from yurios.world.config import Config  # noqa: E402
from yurios.world.main import build_server, create_app  # noqa: E402


def routine(controller) -> None:
    steps = [
        ("she smiles", lambda: controller.set_expression("happy", 0.9, reset_ms=2500)),
        ("looks at the window", lambda: controller.look_at(-1.4, 1.45, 0.6)),
        ("thoughtful…", lambda: controller.set_expression("thinking", 0.7, reset_ms=2500)),
        ("head tilt", lambda: controller.set_bone("head", z=6.0)),
        ("back to you", lambda: (controller.reset_bone(),
                                 controller.look_at_camera())),
        ("shy", lambda: controller.set_expression("shy", 0.8, reset_ms=2500)),
        ("rain up", lambda: controller.set_rain(1.0)),
        ("music on", lambda: controller.music("play", track="night_piano", volume=0.35)),
        ("surprised!", lambda: controller.set_expression("surprised", 1.0, reset_ms=2000)),
        ("tender", lambda: controller.set_expression("tender", 0.8, reset_ms=3000)),
        ("music off, rain down", lambda: (controller.music("stop"),
                                          controller.set_rain(0.4))),
    ]
    while controller.viewers == 0:
        time.sleep(0.5)
    print("viewer attached — running the routine (Ctrl+C to stop)")
    while True:
        for label, act in steps:
            print(f"  → {label}")
            act()
            time.sleep(3.0)


def main() -> None:
    cfg = Config()
    app = create_app(cfg)
    controller = app.state.rt.controller
    threading.Thread(target=routine, args=(controller,), daemon=True).start()
    print(f"\n  demo → http://{cfg.host}:{cfg.port}  (open it, click to enter)\n")
    try:
        build_server(app, cfg).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
