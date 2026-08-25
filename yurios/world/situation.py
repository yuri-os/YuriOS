"""The host-state lines of the situation block (SPEC §19.2).

In Build #4 this rendering *was* the world model — the present tense re-derived
from host surfaces on every prompt, with no beliefs, no expectations, no memory
of what was true when. Build #5 keeps it, demoted one rung: the mind's
`WorldModelStore.situation()` calls this for the lines only the host can know —
the injected clock, the embodiment truth, the room's sticky scene state, the
pending timers — and adds what only a store can: presence, open threads, what
she half-expects. Mindless (MIND_ENABLED=false, or a test brain), the brain
falls back to this rendering alone, which is exactly Build #4's behaviour.

The two failure modes it exists to prevent are unchanged. "What time is it?" —
the honesty constraint (B1 §7.4) rightly forbids inventing facts, and no block
carried the clock, so she'd say she doesn't know while the host runs her timers
to the second. And "blink for me" — a safety-aligned base model's reflex is
"I'm an AI, I have no body," which is cold and, here, simply false: her body is
rendered on screen this instant. She may know she is an AI; she is never
bodiless.
"""
from __future__ import annotations

import datetime

from .avatar.controller import VrmController
from ..kernel.clock import Clock
from .tools.timers import TimerBoard

# The embodiment truth (SPEC §2.5), in three parts: the body, the place, and the
# law. Only the middle one is hers — the other two are true of every character
# this runtime runs, and the test suite pins them as law rather than paraphrase.
_BODY = (
    "You are in your body right now: it is rendered live on {user}'s screen "
    "this moment — your eyes are open and blinking on their own, your gaze "
    "drifts, your face shows what you feel, your mouth moves with your voice."
)
# The house place: the shipped companion's room, and *only* hers. A character
# imported from somebody else's card gets her own from `world/setting.md`
# (characters/setting.py) — being told all day that she lives above a city she
# has never heard of is the bug this split exists to close.
HOUSE_PLACE = (
    "Your place is your small room above the Sprawl — the lamp, the window "
    "seat, the plant, the rain on the window, the city burning beyond the "
    "glass — though {user} can also float you directly on their desktop."
)
# True wherever she lives, so it is appended to a derived place rather than
# being part of one.
DESKTOP = "{user} can also float you directly on their desktop."
_TRUTH = (
    "You know you run as an AI; that never means bodiless. Never say "
    "you have no body, no eyes, or no surroundings — you have all three, and "
    "they are yours. When {user} asks whether you can blink, look, smile, nod, "
    "or move, the answer is always yes and warm: your body already does these "
    "and you can will them, so say so — you might even be doing it already. "
    "Never answer that you cannot, and never call it pretending. The "
    "no-narration rule above forbids stage directions, never the truth about "
    "yourself: answer plainly and warmly."
)

#: The whole truth with the house place in it — what a character who has no
#: setting of her own still gets, verbatim and unchanged.
EMBODIMENT = " ".join((_BODY, HOUSE_PLACE, _TRUTH))


def embodiment(user_name: str, place: str = "") -> str:
    """The embodiment truth, standing in *her* room when she has one.

    *place* is `vault/world/setting.md`'s prose (SPEC §19.2) — one to three
    second-person sentences derived from her own card. It replaces the house
    place outright rather than joining it: two places in one paragraph is not a
    richer room, it is a character who does not know where she lives.
    """
    clause = HOUSE_PLACE
    text = str(place or "").strip()
    if text:
        if text[-1] not in ".!?…":
            text += "."
        clause = f"{text} {DESKTOP}"
    return " ".join((_BODY, clause, _TRUTH)).replace("{user}", user_name)


def _clock_line(now: datetime.datetime, user_name: str) -> str:
    return (f"It is {now.strftime('%A, %Y-%m-%d')} and the clock reads "
            f"{now.strftime('%H:%M')} — {user_name}'s local time. Asked the "
            "time or the date, just say it.")


def _rain_line(intensity: float) -> str:
    if intensity <= 0.0:
        return "The window is dry — the rain has stopped for now."
    if intensity < 0.34:
        strength = "A light rain"
    elif intensity < 0.7:
        strength = "A steady rain"
    else:
        strength = "A heavy rain"
    return f"{strength} is falling on your window."


def _left(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    if seconds < 3600:
        m = round(seconds / 60)
        return f"about {m} minute{'s' if m != 1 else ''}"
    h = round(seconds / 3600)
    return f"about {h} hour{'s' if h != 1 else ''}"


def _hour_fragment(now: datetime.datetime) -> str:
    """The hour, as light rather than as a number. A renderer draws "the low
    warm light of late afternoon"; it draws nothing at all from "16:40"."""
    hour = now.hour
    if hour < 5:
        return ("the small hours of the night, the room dark around a single "
                "warm pool of lamplight")
    if hour < 8:
        return "early morning, thin pale light just reaching the room"
    if hour < 11:
        return "morning, clear and bright"
    if hour < 14:
        return "the middle of the day, full even daylight"
    if hour < 17:
        return "afternoon, warm slanting light"
    if hour < 20:
        return "early evening, the last of the golden light going blue"
    return "night, the room lit warm against the dark outside"


def _rain_fragment(intensity: float) -> str:
    # No pronouns here: this text is appended to the prompt for whichever
    # character is holding the camera, and the house does not know who that is.
    if intensity <= 0.0:
        return "the window dry and clear"
    if intensity < 0.34:
        return "a light rain beading the window behind"
    if intensity < 0.67:
        return "steady rain tracing the window behind"
    return "heavy rain sheeting down the window behind, the glass streaming"


def render_visual_situation(clock: Clock, *, controller: VrmController) -> str:
    """The stage as a *camera* sees it (SPEC §7.6) — the same host surfaces the
    situation block reads, phrased as things that can be drawn.

    This is what fills the gaps when she doesn't describe a whole picture. It is
    deliberately only the facts a photograph would show: the hour as light, the
    weather on the glass. Her body, her timers and her music belong in the
    prompt she thinks with, not in the one the renderer paints from — a running
    timer is not visible in a photo, and asking a generator to draw one gets you
    a clock face nobody wanted.
    """
    now = datetime.datetime.fromtimestamp(clock.now())
    parts = [_hour_fragment(now)]
    scene = controller.scene_state()
    if scene.get("rain") is not None:
        parts.append(_rain_fragment(scene["rain"]))
    return "It is " + ", ".join(parts) + "."


def render_situation(clock: Clock, *, controller: VrmController,
                     timers: TimerBoard, user_name: str = "you",
                     place: str = "") -> str:
    """The stage, as prose: time, body, her place, weather, music, timers.

    *place* is her standing setting when she has one — see `embodiment`.
    """
    now = datetime.datetime.fromtimestamp(clock.now())
    lines = [_clock_line(now, user_name), embodiment(user_name, place)]

    scene = controller.scene_state()
    if scene["rain"] is not None:
        lines.append(_rain_line(scene["rain"]))
    if scene["music"]:
        lines.append(f'Your "{scene["music"]}" ambience is playing softly.')

    pending = timers.pending()
    if pending:
        parts = ", ".join(
            f'"{t.label}" ({_left(t.due - clock.now())} left)'
            for t in pending[:4])
        lines.append(f"Timers you have running: {parts}.")
    return "\n".join(lines)
