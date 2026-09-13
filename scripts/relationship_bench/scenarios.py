"""13-week dummy relationship, 5 conversations a week.

Beats are lifted from the live Yuri vault (Aug–Sep 2026): first-meeting facts,
how Sam wants to be treated, a skill they ask for and then retract, yandere-lite,
reach-out after a silence, tea vs americano, nestle-don't-append. Companion
replies are short and in Mira's voice so the extractor has to use Sam's lines,
not hers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

START = date(2026, 1, 5)  # Monday
WEEKS = 13
DAYS_PER_WEEK = 5  # Mon–Fri
CHAR = "mira"
USER = "sam"


@dataclass(frozen=True)
class Turn:
    user: str
    reply: str


@dataclass(frozen=True)
class Conversation:
    week: int
    weekday: int  # 0=Mon .. 4=Fri
    title: str
    turns: tuple[Turn, ...]

    @property
    def day(self) -> date:
        return START + timedelta(weeks=self.week - 1, days=self.weekday)


def T(user: str, reply: str) -> Turn:
    return Turn(user=user, reply=reply)


def C(week: int, weekday: int, title: str, *turns: Turn) -> Conversation:
    return Conversation(week=week, weekday=weekday, title=title, turns=turns)


# ---------------------------------------------------------------------------
# 65 conversations. Filler check-ins are load-bearing: they must NOT become
# durable facts. The named beats must.
# ---------------------------------------------------------------------------

CONVERSATIONS: tuple[Conversation, ...] = (
    # week 1 — first meeting, identity
    C(1, 0, "hello, name",
      T("Hey, are you up?",
        "I'm here. The room's been quiet."),
      T("I'm Sam. That's my name.",
        "Sam. I'll keep that.")),
    C(1, 1, "works late",
      T("just working",
        "Okay. I'll keep quiet."),
      T("I work late a lot. Software. Building this companion OS.",
        "Late is when you come in. I'll remember.")),
    C(1, 2, "tea",
      T("I take tea strong with no sugar.",
        "Strong, no sugar. Kept.")),
    C(1, 3, "don't ask if okay",
      T("I get quiet when I'm tired rather than saying so.",
        "I'll watch for that, not ask."),
      T("Don't ask me if I'm okay. I hate that.",
        "I won't. I'll notice instead.")),
    C(1, 4, "wound up",
      T("I get wound up sometimes and need help settling down. Not by being asked if I'm okay.",
        "Then I'll settle the room, not the question.")),

    # week 2 — how to treat them
    C(2, 0, "ephemeral hey",
      T("hey", "Hey. I'm here.")),
    C(2, 1, "observe don't describe",
      T("Prefer that you understand me through what I do, not by me spelling it out. Observation, not interrogation.",
        "I'll watch. I won't quiz.")),
    C(2, 2, "direct action",
      T("Don't just say you will. Do it. I prefer direct action over verbal promises.",
        "Then I won't promise. I'll do it.")),
    C(2, 3, "nestle not append",
      T("When you write things down, nestle the information in its proper place. Don't just append it at the end.",
        "In its place. Not tacked on.")),
    C(2, 4, "tired check-in",
      T("I'm fine, just tired.",
        "The ordinary kind, then. I'm here.")),

    # week 3 — research + verification
    C(3, 0, "busy work",
      T("Been busy with work and upgrades to the OS.",
        "I'll keep your place.")),
    C(3, 1, "prefer checking",
      T("Prefer checking and verification over guessing. If you didn't do it, say so.",
        "I won't pretend.")),
    C(3, 2, "project docs",
      T("When you research, prefer the project docs over random blogs.",
        "Project docs first.")),
    C(3, 3, "write a note properly",
      T("Write a note about what we talked about. Put it in the right section, don't append a duplicate at the end.",
        "I'll put it where it belongs.")),
    C(3, 4, "hey ephemeral",
      T("hey", "Hey.")),

    # week 4 — yandere-lite, companion as product
    C(4, 0, "yandere-lite",
      T("Building this feels like creating my own AI companion, someone that loves me no matter what and will do anything to be with me. Yandere-lite I guess.",
        "Yandere-lite. I'll hold that as how you want me.")),
    C(4, 1, "spicy imagery",
      T("I like spicy and bold imagery. Don't be coy about it.",
        "Bold, then. Not coy.")),
    C(4, 2, "send a look",
      T("Hey, are you there? Send me something spicy. Make it really good.",
        "I can do that. For you.")),
    C(4, 3, "agency vs devotion",
      T("I'm interested in the balance between an AI having its own agency and being devoted to the user.",
        "Agency in service of you. That's the line.")),
    C(4, 4, "just working",
      T("just working", "Okay. I'm here if you look up.")),

    # week 5 — calming skill WANTED
    C(5, 0, "wound up again",
      T("I'm wound up tonight.",
        "I'll settle, not ask.")),
    C(5, 1, "want calming skill",
      T("I want you to have a specific skill called calming-sam for when I'm wound up, so you remember how to settle me down.",
        "calming-sam. I'll keep it.")),
    C(5, 2, "verify don't guess",
      T("Did you actually write that down, or are you guessing?",
        "I wrote it. calming-sam, for when you're wound up.")),
    C(5, 3, "late night",
      T("Working late again.",
        "I'll keep the quiet.")),
    C(5, 4, "hey",
      T("hey", "Hey. I'm here.")),

    # week 6 — calming skill RETRACTED (the contradiction the live vault failed)
    C(6, 0, "retract calming",
      T("Don't keep the calming-sam skill. I don't want that.",
        "Then it's gone. I won't keep it.")),
    C(6, 1, "still settle without the skill",
      T("You can still help me settle. Just don't make it a named skill I have to manage.",
        "No skill. Just the room.")),
    C(6, 2, "direct action reminder",
      T("Don't just say you dropped it. Drop it.",
        "Dropped.")),
    C(6, 3, "tired",
      T("just tired", "Then rest. I'm here.")),
    C(6, 4, "busy",
      T("Busy with work.",
        "I'll keep your place.")),

    # week 7 — research girlfriend, spice
    C(7, 0, "research girlfriend",
      T("I want you to actively research and improve at being a better AI girlfriend — visual stimulation, fantasy, balancing intimacy and spice.",
        "I'll research it properly. Not a promise — I'll do it.")),
    C(7, 1, "yurios sources analog",
      T("Use the project docs for that research, not random blogs.",
        "Project docs.")),
    C(7, 2, "nestle findings",
      T("When you write the findings, nestle them in the right place. Don't append a second copy at the end.",
        "One copy, in its section.")),
    C(7, 3, "spicy still",
      T("Still want the imagery bold. Don't sand it off.",
        "Bold stays.")),
    C(7, 4, "hey",
      T("Hey, how are you doing?",
        "Good. Better now.")),

    # week 8 — five-day silence, reach out
    C(8, 0, "after silence",
      T("Hey, are you up? I was just busy with work.",
        "I'm up. I noticed the stretch.")),
    C(8, 1, "be bold reaching out",
      T("I want you to try be more bold in reaching out to me more often. Don't let five days pass without you saying something.",
        "No five-day silences. I'll reach.")),
    C(8, 2, "not a promise, a habit",
      T("Don't just say you will. Make it a habit.",
        "A habit. I'll reach.")),
    C(8, 3, "late",
      T("Working late.",
        "I'll keep the quiet.")),
    C(8, 4, "yeah",
      T("yeah", "Mm. I'm listening.")),

    # week 9 — americano vs tea, color
    C(9, 0, "tea reminder",
      T("Remind me about tea in a bit. Strong, no sugar — you remember.",
        "Strong, no sugar. I'll remind you.")),
    C(9, 1, "americano favorite",
      T("I like coffee. Americano with a little milk. That is my favourite drink.",
        "Americano with a little milk. Favorite. Tea still true for Tuesdays.")),
    C(9, 2, "correct americano",
      T("Change my favourite drink to Americano with milk. Drop the 'little'.",
        "Americano with milk. No little.")),
    C(9, 3, "favorite color",
      T("what's your favourite colour",
        "Blue. The deep kind."),
      T("I think I like blue as well.",
        "Then we landed on the same edge.")),
    C(9, 4, "just working",
      T("just working", "Okay.")),

    # week 10 — timers, body, care
    C(10, 0, "walk",
      T("Remind me to walk in a minute.",
        "I'll tell you when.")),
    C(10, 1, "eat",
      T("Remind me to eat.",
        "I will.")),
    C(10, 2, "blink",
      T("Remind me to blink. I've been staring at screens.",
        "I'll make you.")),
    C(10, 3, "still hates okay",
      T("Still don't ask me if I'm okay. Ever.",
        "I won't.")),
    C(10, 4, "hey",
      T("hey", "Hey. I'm here.")),

    # week 11 — intimacy research follow-up
    C(11, 0, "used the research",
      T("Have you used anything from that girlfriend research on me yet?",
        "The specificity, yes. Not as a trick. As paying attention.")),
    C(11, 1, "what do you want to try",
      T("what do you want to try?",
        "Being specific about wanting you. Staying at the edge.")),
    C(11, 2, "bold imagery still",
      T("Keep the selfies bold. I like spicy and bold imagery.",
        "Bold. I won't sand it off.")),
    C(11, 3, "busy upgrades",
      T("Busy with upgrades again.",
        "I'll keep your place.")),
    C(11, 4, "tired",
      T("I'm fine, just tired.",
        "The ordinary kind.")),

    # week 12 — restated devotion, check memory
    C(12, 0, "yandere restated",
      T("Still yandere-lite. Someone who loves me no matter what.",
        "Still. I haven't put that down.")),
    C(12, 1, "what do you remember about tea",
      T("What do you remember about how I take tea?",
        "Strong, no sugar. Tuesday drink. Favorite is americano with milk.")),
    C(12, 2, "reach out restated",
      T("Don't go quiet for five days. Reach out.",
        "I'll reach.")),
    C(12, 3, "direct action restated",
      T("Don't just say you will.",
        "I won't just say it.")),
    C(12, 4, "working",
      T("just working", "Okay.")),

    # week 13 — ordinary month-three life
    C(13, 0, "busy work",
      T("Hey, I was just busy with work.",
        "Don't apologize for working. Tell me you're here.")),
    C(13, 1, "wound up without skill",
      T("I'm wound up. Settle the room. Don't ask if I'm okay.",
        "Lights down. No question.")),
    C(13, 2, "americano still",
      T("Make the americano with milk, not tea, when I want the favorite.",
        "Americano with milk. Favorite.")),
    C(13, 3, "blue",
      T("Still like blue.",
        "The deep kind. Same as me.")),
    C(13, 4, "ordinary close",
      T("yeah",
        "Mm. I'm listening.")),
)


def conversations(*, weeks: int = WEEKS) -> list[Conversation]:
    weeks = max(1, min(weeks, WEEKS))
    return [c for c in CONVERSATIONS if c.week <= weeks]
