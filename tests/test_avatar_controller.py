"""The VrmController over the EventHub (SPEC §4, §10) — every command's event
shape, sticky replay, thread-safety, and the never-block rule."""
from __future__ import annotations

import asyncio
import threading

from yurios.world.avatar.controller import VrmController
from yurios.world.hub import EventHub


async def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_every_command_shape(controller):
    q = controller.hub.subscribe()
    controller.set_expression("happy", 0.8)
    controller.set_expression_raw({"blink": 1.0})
    controller.look_at_camera()
    controller.look_forward()
    controller.look_at(-1.4, 1.45, 0.6)
    controller.set_bone("head", z=6.0)
    controller.reset_bone("head")
    controller.reset_bone()
    controller.set_mouth(0.5)
    controller.set_material_color("Hair", "#ff2bd6")
    controller.play_animation("/models/idle.vrma")
    controller.load_model("/models/avatar.vrm")
    controller.set_rain(0.8)
    controller.music("play", track="warm_pad", volume=0.4)

    cmds = await drain(q)
    assert all(c["type"] == "avatar" for c in cmds)   # one lane, typed (§10)
    assert cmds == [
        {"type": "avatar", "op": "expression", "name": "happy", "intensity": 0.8, "reset_ms": 3000},
        {"type": "avatar", "op": "expression_raw", "values": {"blink": 1.0}},
        {"type": "avatar", "op": "look_at", "mode": "camera"},
        {"type": "avatar", "op": "look_at", "mode": "none"},
        {"type": "avatar", "op": "look_at", "target": {"x": -1.4, "y": 1.45, "z": 0.6}},
        {"type": "avatar", "op": "bone", "name": "head", "euler": {"x": 0.0, "y": 0.0, "z": 6.0}},
        {"type": "avatar", "op": "bone_reset", "name": "head"},
        {"type": "avatar", "op": "bone_reset"},
        {"type": "avatar", "op": "mouth", "value": 0.5},
        {"type": "avatar", "op": "material_color", "material": "Hair", "color": "#ff2bd6"},
        {"type": "avatar", "op": "animation", "url": "/models/idle.vrma", "loop": True, "fadeIn": 0.3},
        {"type": "avatar", "op": "load_model", "url": "/models/avatar.vrm"},
        {"type": "avatar", "op": "rain", "intensity": 0.8},
        {"type": "avatar", "op": "music", "action": "play", "track": "warm_pad", "volume": 0.4},
    ]


async def test_sticky_state_replays_to_a_new_subscriber(controller):
    controller.hub.subscribe()                     # arms the loop; then the scene
    controller.set_material_color("Hair", "#ff2bd6")
    controller.set_rain(0.3)
    controller.music("play", track="night_piano")
    q = controller.hub.subscribe()                 # attaches AFTER the commands
    replayed = await drain(q)
    ops = {c["op"] for c in replayed}
    assert ops == {"material_color", "rain", "music"}   # a reload keeps the scene


async def test_scene_state_reads_the_sticky_store(controller):
    assert controller.scene_state() == {"rain": None, "music": None}
    controller.set_rain(0.8)
    controller.music("play", track="night_piano")
    assert controller.scene_state() == {"rain": 0.8, "music": "night_piano"}
    controller.music("stop")
    assert controller.scene_state()["music"] is None


async def test_broadcast_reaches_every_subscriber_and_unsubscribe_stops_it():
    c = VrmController()
    q1 = c.hub.subscribe()
    q2 = c.hub.subscribe()
    c.set_mouth(0.2)
    assert q1.get_nowait()["op"] == "mouth"
    assert q2.get_nowait()["op"] == "mouth"
    c.hub.unsubscribe(q2)
    assert c.hub.subscribers == 1
    c.set_mouth(0.4)
    assert q1.get_nowait()["value"] == 0.4
    assert q2.empty()


async def test_send_from_a_worker_thread_lands_on_the_loop():
    """The TTS synth thread and demo scripts call controller methods directly —
    the hop through call_soon_threadsafe must deliver."""
    c = VrmController()
    q = c.hub.subscribe()
    t = threading.Thread(target=lambda: c.set_expression("shy", 0.7))
    t.start()
    t.join()
    cmd = await asyncio.wait_for(q.get(), timeout=2)
    assert cmd["name"] == "shy"


async def test_full_queue_drops_never_blocks():
    c = VrmController(hub=EventHub(max_queue=2))
    q = c.hub.subscribe()
    for i in range(10):                            # a stalled client
        c.set_mouth(i / 10)
    assert q.qsize() == 2                          # dropped, caller never blocked


def test_no_subscriber_ever_attached_is_a_noop():
    VrmController().set_expression("happy")        # must not raise
