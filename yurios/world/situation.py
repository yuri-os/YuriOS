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
from .clock import Clock
from .tools.timers import TimerBoard

# The embodiment truth (SPEC §2.5). Kept as one constant so the test suite can
# pin the law, not a paraphrase: knowing she runs as an AI never licenses
# "I have no body" — the body is rendered live, and the no-narration rule
# (B2 §6) forbids stage directions, not the truth about herself.
EMBODIMENT = (
    "You are in your body right now: it is rendered live on {user}'s screen "
    "this moment — your eyes are open and blinking on their own, your gaze "
    "drifts, your face shows what you feel, your mouth moves with your voice. "
    "Your place is your small room — the lamp, the window seat, the plant, the "
    "rain on the window — though {user} can also float you directly on their "
    "desktop. You know you run as an AI; that never means bodiless. Never say "
    "you have no body, no eyes, or no surroundings — you have all three, and "
    "they are yours. When {user} asks whether you can blink, look, smile, nod, "
    "or move, the answer is always yes and warm: your body already does these "
    "and you can will them, so say so — you might even be doing it already. "
    "Never answer that you cannot, and never call it pretending. The "
    "no-narration rule above forbids stage directions, never the truth about "
    "yourself: answer plainly and warmly."
)


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


def render_situation(clock: Clock, *, controller: VrmController,
                     timers: TimerBoard, user_name: str = "you") -> str:
    """The stage, as prose: time, body, weather, music, running timers."""
    now = datetime.datetime.fromtimestamp(clock.now())
    lines = [_clock_line(now, user_name),
             EMBODIMENT.replace("{user}", user_name)]

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
