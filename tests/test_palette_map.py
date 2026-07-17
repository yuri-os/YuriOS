"""The palette map (SPEC §3.4) — source-scanned, so a rename in either layer
fails the build instead of silently showing a neutral face."""
from __future__ import annotations

import re
from pathlib import Path

from yurios.desktop.voice.emotion import PALETTE
from yurios.world.avatar.controller import EMOTIONS

ROOT = Path(__file__).resolve().parents[1]
EMOTE_JS = (ROOT / "web" / "js" / "stage" / "EmoteController.js").read_text()
BRIDGE_JS = (ROOT / "web" / "js" / "bridge.js").read_text()
CONTROLLER_PY = (ROOT / "yurios" / "world" / "avatar" / "controller.py").read_text()


def test_every_brain_palette_name_has_a_frontend_catalog_entry():
    """The brain speaks B2's 8 names (desktop/voice/emotion.py); the stage's
    catalog (EmoteController.js) must realise every one of them."""
    for name in PALETTE:
        assert re.search(rf"\[['\"]{name}['\"]", EMOTE_JS), (
            f"brain palette name {name!r} missing from the EmoteController catalog")


def test_every_vrm_preset_is_in_the_catalog_too():
    for name in EMOTIONS:
        assert re.search(rf"\[['\"]{name}['\"]", EMOTE_JS), (
            f"VRM preset {name!r} missing from the EmoteController catalog")


def test_bridge_dispatches_every_command_the_controller_sends():
    """Every {"type": …} the Python controller emits has a case in bridge.js —
    the §4 command union, cross-checked from source."""
    sent = set(re.findall(r'"type":\s*"([a-z_]+)"', CONTROLLER_PY))
    handled = set(re.findall(r"case '([a-z_]+)':", BRIDGE_JS))
    assert sent, "no commands found in controller.py — scan broke?"
    missing = sent - handled
    assert not missing, f"bridge.js has no case for: {sorted(missing)}"
