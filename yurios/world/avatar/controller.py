"""The puppet strings (SPEC §4) — the in-process VrmController.

This is ch. 34's canonical control surface: *all decisions live in Python; the
browser is a render-and-control client.* The method surface is the vrm-viewer
reference impl's `VrmController`, kept verbatim — plus Build #4's two scene
channels (`set_rain`, `music`) — because this exact seam is what Build #5's tick
loop will hold (SPEC §14): swap the caller, not the wire.

The transport, though, is no longer its own: every method is one `avatar` event
published on the **EventHub** (SPEC §10 — the YuriOS shape), fanned out to every
attached frontend over `/api/events` alongside chat messages and everything
else. A command dict's internal `type` becomes the event's `op`:

    controller.set_expression("happy")
      → {type: avatar, op: expression, name: happy, …}   on the bus

The hub owns what the old controller owned itself: per-subscriber queues, the
never-block rule, thread-safe publishing, and sticky replay (material colors,
rain, music survive a reload). `scene_state()` reads the hub's sticky store —
the same bytes a late-joining frontend gets.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from ..hub import EventHub

log = logging.getLogger("world.avatar")

# Humanoid bones the viewer understands (VRM standard subset) — vrm-viewer's list.
HUMANOID_BONES = (
    "hips", "spine", "chest", "upperChest", "neck", "head",
    "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
    "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
)

# The six VRM preset expressions (CS §5.2) — set_expression's guaranteed catalog.
# The frontend catalog also realises the Build #2 palette names (SPEC §3.4).
EMOTIONS = ("happy", "sad", "angry", "surprised", "neutral", "relaxed")

# Generative ambience tracks the frontend knows (web/js/music.js, SPEC §7.5).
MUSIC_TRACKS = ("warm_pad", "night_piano")


class VrmController:
    """Drives every attached frontend by publishing `avatar` events (SPEC §4)."""

    def __init__(self, hub: EventHub | None = None):
        # standalone construction (unit tests, scripts) gets a private hub; the
        # Runtime hands in the app's hub so her body shares the one bus
        self.hub = hub or EventHub()

    # ---- transport: one avatar event on the bus ----

    def _send(self, cmd: Dict[str, Any], sticky: Optional[Tuple[str, str]] = None) -> None:
        event = dict(cmd)
        event["op"] = event.pop("type")           # internal `type` → wire `op`
        self.hub.publish("avatar", event, sticky=sticky)

    # ---- control API (the vrm-viewer channels, SPEC §4) ----

    def set_expression(self, name: str, intensity: float = 1.0,
                       reset_ms: int = 3000) -> None:
        """Channel 2 — trigger an emotion; the viewer resets to neutral after
        `reset_ms` (0 = hold until the next change)."""
        self._send({"type": "expression", "name": name,
                    "intensity": float(intensity), "reset_ms": int(reset_ms)})

    def set_expression_raw(self, values: Dict[str, float]) -> None:
        """Channel 2 — set raw blendshape weights directly, e.g. {"blink": 1.0}."""
        self._send({"type": "expression_raw",
                    "values": {k: float(v) for k, v in values.items()}})

    def look_at_camera(self) -> None:
        """Channel 5 — make eye contact with the viewer's camera."""
        self._send({"type": "look_at", "mode": "camera"})

    def look_forward(self) -> None:
        """Channel 5 — neutral straight-ahead gaze."""
        self._send({"type": "look_at", "mode": "none"})

    def look_at(self, x: float, y: float, z: float) -> None:
        """Channel 5 — aim gaze at an explicit world-space point (the window,
        when she's rain-gazing — SPEC §8.1; the point is scene canon, §6)."""
        self._send({"type": "look_at",
                    "target": {"x": float(x), "y": float(y), "z": float(z)}})

    def set_bone(self, name: str, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        """Channel 6 — override a humanoid bone's local rotation (Euler degrees)."""
        self._send({"type": "bone", "name": name,
                    "euler": {"x": float(x), "y": float(y), "z": float(z)}})

    def reset_bone(self, name: Optional[str] = None) -> None:
        """Release a bone override (or all overrides when name is None)."""
        cmd: Dict[str, Any] = {"type": "bone_reset"}
        if name is not None:
            cmd["name"] = name
        self._send(cmd)

    def set_mouth(self, value: float) -> None:
        """Channel 4 — scripted mouth override in [0,1]. The live mouth is the
        viseme driver on real audio (SPEC §5); this is the puppet channel."""
        self._send({"type": "mouth", "value": float(value)})

    def set_material_color(self, material: str, color: str) -> None:
        """Tint a material by name (CSS hex). A tint, not an outfit — VRM
        clothing is baked (→ ch. 29). Sticky: replayed to new subscribers."""
        self._send({"type": "material_color", "material": material, "color": color},
                   sticky=("material_color", material))

    def play_animation(self, url: str, loop: bool = True, fade_in: float = 0.3) -> None:
        """Channel 1 — play a .vrma clip (served from web/models/)."""
        self._send({"type": "animation", "url": url, "loop": loop,
                    "fadeIn": float(fade_in)})

    def load_model(self, url: str) -> None:
        """Swap the .vrm — the user's own-body slot (→ ch. 25 licensing)."""
        self._send({"type": "load_model", "url": url})

    # ---- Build #4 scene channels (SPEC §4/§6/§7.5) ----

    def scene_state(self) -> Dict[str, Any]:
        """The sticky scene, read-side (SPEC §2.5): what the room is doing right
        now, for the situation block. `rain` is the last set intensity (None if
        never set); `music` is the playing track name (None when silent)."""
        rain = self.hub.sticky.get(("rain", ""))
        music = self.hub.sticky.get(("music", ""))
        playing = bool(music and music.get("action") == "play")
        return {"rain": rain["intensity"] if rain else None,
                "music": (music.get("track") or "warm_pad") if playing else None}

    def set_rain(self, intensity: float) -> None:
        """Rain on the window + the noise bed, 0..1. Sticky."""
        self._send({"type": "rain", "intensity": max(0.0, min(1.0, float(intensity)))},
                   sticky=("rain", ""))

    def music(self, action: str, track: Optional[str] = None,
              volume: Optional[float] = None) -> None:
        """Start/stop the generative ambience (play_music's realisation). Sticky."""
        cmd: Dict[str, Any] = {"type": "music", "action": action}
        if track is not None:
            cmd["track"] = track
        if volume is not None:
            cmd["volume"] = max(0.0, min(1.0, float(volume)))
        self._send(cmd, sticky=("music", ""))
