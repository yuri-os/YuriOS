"""The partner model — `vault/soul/USER.md` (SPEC §6.3).

Her theory of *you*: durable, small, always injected whole (§7.1 block 5).
After each exchange the utility model extracts durable facts as ops against
the *current* USER.md (so it updates rather than duplicates), under a strict
JSON schema. Low-confidence claims are QUARANTINED — kept out of USER.md until
a second turn corroborates. Promotion, not capture, is the trust boundary
(→ ch. 15).

The file is a *living* model, not an append-only log: a new claim that restates
or contradicts one already there replaces it; the relationship phase is one
line that moves; DREAM rewrites the narrative sections from the bullets so
empty "who they seem to be" slots cannot freeze on day one. Preferences about
*her* (how they asked her to be) are copied into a persona delta the mind
queues as a gated PERSONA.md edit — USER.md is the runtime's to write, her
identity is not.

USER.md stays human-readable and human-editable markdown: the user can open
it, fix it, delete from it. It is their file (§4.2).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from yurios.app import vaultgit
from yurios.characters.soulfiles import parse_md_text, split_sections

QUARANTINE_CONFIDENCE = 0.6   # below this, an op waits for corroboration (§6.3)
UNSCORED_CONFIDENCE = 0.0     # a claim the model returned with NO confidence must
                              # be corroborated before it lands — "unsure" fails
                              # safe, matching the quarantine stance (§6.3). Do NOT
                              # default this high: a missing score is not certainty.
CORROBORATION_OVERLAP = 0.5   # token-overlap that counts as "the same claim again"
KEY_MATCH_OVERLAP = 0.5       # token-overlap that still counts as the same *slot*
                              # when topic keys disagree (paraphrase without a
                              # quoted name). Measured over *content* tokens as a
                              # Jaccard ratio — see `_overlap`, which is the only
                              # thing standing between "merge, don't duplicate"
                              # and silently eating an unrelated bullet.

# Extractor vocabulary. "What helps" and the phase are first-class because the
# live vault froze them as empty headings next to an ever-growing Stable list.
BULLET_SECTIONS = ("Stable", "Ongoing", "Don't forget",
                   "What helps, and what doesn't")
PROSE_SECTIONS = ("Who {{user}} seems to be", "Current relationship phase")
SECTIONS = BULLET_SECTIONS + PROSE_SECTIONS
# Heading matching is on the *normalised* form. We write USER.md ourselves, so
# the file is always spelled canonically — but the *model* names a section in
# its reply, and it types "What helps" or "what helps and what doesn't" as
# readily as the exact heading. An exact-string table dropped those ops.
def _norm_heading(heading: str) -> str:
    text = re.sub(r"\{\{.*?\}\}", " ", str(heading or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


HELPS = "What helps, and what doesn't"
WHO = "Who {{user}} seems to be"
PHASE = "Current relationship phase"
_CANON = {
    "stable": "Stable",
    "ongoing": "Ongoing",
    "don t forget": "Don't forget",
    "dont forget": "Don't forget",
    "what helps": HELPS,
    "what helps and what doesn t": HELPS,
    "what helps and what does not": HELPS,
    "what helps and what doesnt": HELPS,
    "current relationship phase": PHASE,
    "relationship phase": PHASE,
    "who seems to be": WHO,
    "who they seem to be": WHO,
    "who user seems to be": WHO,
    "who the user seems to be": WHO,
}


def canon_section(heading: str) -> str | None:
    """The canonical name for a heading, or None if it is not one of ours."""
    return _CANON.get(_norm_heading(heading))


SECTION_ALIASES = {h: c for h, c in (
    ("Stable", "Stable"), ("Ongoing", "Ongoing"),
    ("Don't forget", "Don't forget"), ("What helps", HELPS),
    (HELPS, HELPS), (PHASE, PHASE),
    ("Who they seem to be", WHO), (WHO, WHO))}
CANONICAL_ORDER = (
    "Who {{user}} seems to be",
    "What helps, and what doesn't",
    "Current relationship phase",
    "Stable",
    "Don't forget",
    "Ongoing",
)
PHASES = ("early", "mid", "late")
PHASE_RANK = {"early": 0, "mid": 1, "late": 2}

EXTRACT_SYSTEM = """\
You maintain a living partner-model file about THE USER (the human).

The transcript has two speakers:
  - the user — lines beginning "you:"
  - the companion (the AI) — lines beginning with the companion's name (e.g. "yuri:")

Extract only DURABLE facts THE USER stated, worth remembering across sessions.
This is a revision pass, not a log: if the new claim restates or contradicts a
line already in USER.md, UPDATE or REMOVE that line. Never add a paraphrase.

Rules:
  - Use ONLY the user's ("you:") lines as the source of facts.
  - NEVER record the companion's self-description as a fact about the user. If the
    companion says "My name is Yuri", that is the COMPANION's name, not the user's.
  - Identity (name, job, tastes) → section "Stable".
  - Current situations → section "Ongoing" (update the old one if it moved).
  - Explicit "remember this" → section "Don't forget".
  - How they want to be treated, and how they asked YOU to be (register, devotion,
    initiation, nicknames) → section "What helps, and what doesn't".
  - Set "about_her": true ONLY when the user is asking you to change your
    STANDING CHARACTER — the way you are with them in general: your warmth,
    your formality, your devotion, how forward you are, whether you speak
    first, what you call them. Both tests must pass:
      1. The subject is YOU. A claim about the user — what they like, feel,
         want or are busy with — is never about_her, even with an instruction
         attached ("I like bold imagery, don't be coy about it").
      2. It changes how you generally ARE, not what you do in a situation or
         how you carry out a task. Leave it false for anything shaped like
         "when X, do Y" / "if X, don't Y", and for any working preference —
         how to search, what to check, where to write something down, how
         soon to report back.
    TRUE: "be warmer with me", "stop being so formal", "text me first
    sometimes", "I want you devoted to me", "call me by my name, not 'dear'".
    FALSE: "don't ask if I'm okay", "when I go quiet, don't press", "prefer
    the docs over blogs", "tell me as soon as you find out", "put things under
    the right day".
    When unsure, answer false: this flag queues an edit to your own persona
    for the user to approve, and a wrong one asks them to approve what they
    never asked for. Default false.
  - If the relationship has clearly thawed past first-meeting courtesy, include
    one update to "Current relationship phase" as a single line starting with
    early, mid, or late.
  - If the user only asked a question or stated nothing durable about themselves,
    return {"ops": []}.
  - Ignore ephemeral chit-chat and roleplay stage directions.
  - "text" is the bullet itself. Do not prefix it with "-" or any other marker.

Return JSON, nothing else:
{ "ops": [ { "section": "Stable"|"Ongoing"|"Don't forget"|"What helps, and what doesn't"|"Current relationship phase"|"Who they seem to be",
            "text": string, "op": "add"|"update"|"remove", "confidence": 0..1,
            "about_her": true|false } ] }
Return {"ops": []} if nothing durable was stated."""


@dataclass
class Op:
    section: str
    text: str
    op: str = "add"            # add | update | remove
    confidence: float = UNSCORED_CONFIDENCE
    about_her: bool = False    # a direction about who *she* should be, not a
                               # fact about them — the model decides, and it is
                               # what opens the gated PERSONA.md door (§23)


@dataclass
class PersonaDelta:
    """What USER.md learned about how she should *be* — queued as PERSONA.md."""
    lines: list[str]
    phase: str
    reason: str

    def fingerprint(self) -> str:
        blob = "\n".join(self.lines) + "\n" + self.phase
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def parse_ops(raw: str) -> list[Op]:
    """Tolerant parse of the utility model's reply (§6.2: malformed output is
    logged and dropped, never fatal to the turn)."""
    # a reasoning model (qwen3, r1, …) may prepend a <think>…</think> block; the
    # JSON we want is after it. Strip any think block before hunting for the object.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    ops = []
    for o in data.get("ops", []):
        if not isinstance(o, dict):
            continue
        section = canon_section(str(o.get("section", "")).strip())
        if section is None:
            continue
        text = scrub(str(o.get("text", "")).strip())
        if not text:
            continue
        ops.append(Op(section=section, text=text,
                      op=o.get("op", "add"),
                      # a missing confidence is treated as UNSCORED (fails safe to
                      # the quarantine), never as certainty (§6.3).
                      confidence=float(o.get("confidence", UNSCORED_CONFIDENCE)),
                      about_her=bool(o.get("about_her", False))))
    return ops


async def extract_ops(utility, user_md: str, exchange: str) -> tuple[str, list[Op]]:
    """One cheap utility-model call per exchange (§6.2 step 3). Returns the raw
    reply *and* the parsed ops so the caller can log both (the raw reply is the
    only faithful record of what the model actually proposed — §6.3)."""
    raw = await utility.complete([
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content":
            f"Current USER.md:\n\n{user_md}\n\n---\nLast exchange:\n\n{exchange}"},
    ])
    return raw, parse_ops(raw)


# --- topic keys, polarity, chrome --------------------------------------------

# Words that never distinguish one topic from another. Grammar and the two roles
# — no proper nouns. This list used to end "... you your yuri user", which made
# exactly one character's name invisible to the matcher on a host that holds
# every character on the node: Yuri's bullets were compared on their content and
# Noor's were compared on hers plus "noor". Whose name to discount is a fact
# about the caller, so it arrives as `names=` (see `_content_tokens`).
_STOP = frozenset("""
    a an the to of and or is are was be been being for with that this these those
    they their them she her he his him i my me we our you your user
    prefers prefer likes like wants want does did do doing not never always
    really just also currently working on in at from about as it its
    """.split())


def _name_tokens(names) -> frozenset[str]:
    """The speakers' names, lowercased and split, as stop-words for one call."""
    if not names:
        return frozenset()
    return frozenset(w for n in names
                     for w in re.findall(r"[a-z0-9']+", str(n or "").lower()))
_NEG = re.compile(
    r"\b(not|never|don't|doesn't|does not|do not|no longer|hate|hates|"
    r"won't|will not|stop|stopped)\b",
    re.I)
_PROVENANCE = re.compile(
    r"\s*\((?:implied by|from|see|source:?|cf\.)[^)]*\)",
    re.I)
_PLACEHOLDER = re.compile(r"^_+\(.*\)_+\s*$")
_PHASE_WORD = re.compile(r"\b(early|mid|late)\b", re.I)
_BULLET_MARKER = re.compile(r"^(?:[-*•]\s+)+")


def scrub(text: str) -> str:
    """Drop extractor provenance, seed placeholders, and any bullet marker the
    model typed into the text itself. Facts, not citations, not markup."""
    text = _PROVENANCE.sub("", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    # the extractor sometimes returns "- Likes dogs." for a bullet section; the
    # renderer adds its own "- ", and two of them is how "- - Likes dogs." got
    # into a live USER.md. Strip every leading marker, not just the first.
    text = _BULLET_MARKER.sub("", text).strip()
    if _PLACEHOLDER.match(text):
        return ""
    return text


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", s.lower()))


def _content_tokens(s: str, names=()) -> set[str]:
    return _tokens(s) - _STOP - _name_tokens(names)


def _overlap(a: str, b: str, names=()) -> float:
    """Jaccard over *content* tokens — symmetric, and blind to the filler both
    bullets share.

    It must not be containment over `min(len)`: that made every short bullet a
    near-subset of every longer one, so "Likes blue." and "Likes dogs." scored
    0.5 and `_collapse` deleted one of them. A ratio that grows when a bullet is
    short is a ratio that eats the user's file.
    """
    ta, tb = _content_tokens(a, names), _content_tokens(b, names)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def topic_key(text: str, names=()) -> str:
    """A stable slot name so 'wants X' and 'does not want X' collide."""
    quoted = re.findall(r"[\"'']([^\"'']+)[\"'']", text)
    if quoted:
        return quoted[0].lower().strip()
    hyph = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", text.lower())
    if hyph:
        return hyph[0]
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower())
             if w not in _STOP and w not in _name_tokens(names) and len(w) > 2]
    words = sorted(set(words), key=lambda w: (-len(w), w))[:3]
    return " ".join(sorted(words))


def polarity(text: str) -> int:
    return -1 if _NEG.search(text or "") else 1


def contradicts(a: str, b: str, names=()) -> bool:
    if not a or not b:
        return False
    if (topic_key(a, names) != topic_key(b, names)
            and _overlap(a, b, names) < KEY_MATCH_OVERLAP):
        return False
    return polarity(a) != polarity(b)


def _containment(a: str, b: str, names=()) -> float:
    ta, tb = _content_tokens(a, names), _content_tokens(b, names)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def same_slot(a: str, b: str, *, loose: bool = False, names=()) -> bool:
    """Do these two bullets occupy the same slot?

    Strict by default, and strict is the important word: this is the predicate
    `_collapse` uses to *delete* a bullet, and it runs over the whole file on
    every turn. "Allergic to peanuts." and "Allergic to cats." share a head word
    and nothing else; so do "Likes blue." and "Likes dogs." No lexical rule can
    tell those apart from "prefers quiet mornings" / "prefers loud mornings now",
    where the shared head word *is* the slot — so the strict path declines to
    guess, and two bullets survive. A duplicate is a wart; a deleted fact is a
    broken promise.

    `loose=True` is for an `update` or `remove` op only, where the model has
    read the current file and said "this line replaces that one". Then the
    assertion is the model's, and finding the line it meant is the job.
    """
    if not a or not b:
        return False
    if topic_key(a, names) and topic_key(a, names) == topic_key(b, names):
        return True
    if loose:
        return _containment(a, b, names) >= KEY_MATCH_OVERLAP
    return _overlap(a, b, names) >= KEY_MATCH_OVERLAP


# --- filing: which section a bullet belongs in (the model decides) -----------
#
# This used to be a list of substrings ("yandere", "americano", "hates being
# asked") lifted from one live vault. It scored perfectly against the one
# relationship it was copied from and filed *nothing* for anyone else: a user
# who asked to be flirted with, texted first and never called "dear" produced no
# persona delta at all, because none of her words were on the list. A partner
# model cannot ship with one couple's vocabulary compiled into it, so the model
# that already reads the file reads the bullets too.

LABELS = ("stable", "ongoing", "helps", "learned", "drop")
_LABEL_HOME = {
    "stable": "Stable",
    "ongoing": "Ongoing",
    "helps": "What helps, and what doesn't",
    "learned": "What helps, and what doesn't",
}

CLASSIFY_SYSTEM = """\
You are filing the bullets of a partner-model file about THE USER, and flagging
the ones that are really directions about the COMPANION herself.

Label every numbered bullet with exactly one of:
  "stable"  — a durable fact about the user: name, job, tastes, relationships.
  "ongoing" — a current, temporary situation: what they are busy with right now.
  "helps"   — how the user wants to be TREATED: what to do, or not do, for them.
  "learned" — the user asking the COMPANION to change her STANDING CHARACTER:
              her warmth, her formality, her devotion, how forward she is,
              whether she speaks first, what she calls them. This is the only
              label that changes her personality rather than the user's file.
  "drop"    — pipeline chatter, a citation, an empty placeholder: not a fact.

Judge what the sentence means, not which words it uses.

"helps" and "learned" are both preferences, and telling them apart is the one
judgement here that costs anything. A bullet is "learned" only if both
hold:
  1. The subject is HER, not the user. A bullet that states something about
     the user is "stable" or "helps" even when an instruction is attached —
     "Likes bold imagery, don't be coy about it" is what the USER likes.
  2. It changes how she generally IS, not what she does in a situation or how
     she carries out a task. Anything shaped "when X, do Y" / "if X, don't Y",
     and any working preference — how to search, what to check, where to write
     something down, how soon to report back — is "helps".
"Be warmer with me, less formal", "text me first sometimes", "I want you
devoted to me" and "call me by my name, not 'dear'" are "learned". "Tell me as
soon as you find something out", "prefer the docs over blogs" and "when I go
quiet, don't press" are "helps".

When torn between "helps" and "learned", choose "helps". Both keep the bullet
in the same section; only "learned" also queues an edit to her persona for the
user to approve, so a wrong "learned" asks them to sign off on a change to who
she is that they never asked for.

Return JSON, nothing else:
{ "labels": { "1": "stable", "2": "learned", ... } }
Every bullet number appears exactly once."""


# The judgement is the prompt, so a cached label is only good for the prompt
# that produced it (`store._filing`).
CLASSIFY_FINGERPRINT = hashlib.sha1(
    CLASSIFY_SYSTEM.encode("utf-8")).hexdigest()[:12]


def parse_labels(raw: str, bullets: list[str]) -> dict[str, str]:
    """Tolerant parse of a classify reply into {bullet text: label}.

    Anything unparseable, out of range, or not a known label is simply absent —
    and an absent label means "leave that bullet exactly where it is". A filing
    pass that cannot read its own answer must not refile on a guess.
    """
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL | re.IGNORECASE)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    labels = data.get("labels")
    if not isinstance(labels, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in labels.items():
        try:
            idx = int(str(key).strip().rstrip("."))
        except ValueError:
            continue
        if not 1 <= idx <= len(bullets):
            continue
        label = str(value).strip().lower()
        if label in LABELS:
            out[bullets[idx - 1]] = label
    return out


async def classify_bullets(utility, bullets: list[str]) -> dict[str, str]:
    """One utility call — DREAM only, never the hot path (§21). Returns
    {bullet text: label}; `{}` when there is no model, which leaves every
    bullet where it already is."""
    if utility is None or not bullets:
        return {}
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(bullets, 1))
    raw = await utility.complete([
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": numbered},
    ])
    return parse_labels(raw, bullets)


def label_of(text: str, labels: dict[str, str], names=()) -> str | None:
    """Look a bullet up in a classification. Exact first, then same-slot: the
    per-turn path labels the *op* text, and `_collapse` may since have rewritten
    that bullet into the wording it merged with."""
    if not labels:
        return None
    if text in labels:
        return labels[text]
    for known, label in labels.items():
        if same_slot(known, text, names=names):
            return label
    return None


# --- parsing / rendering USER.md ---------------------------------------------

def _bullets(body: str) -> list[str]:
    out = []
    for line in (body or "").splitlines():
        if line.lstrip().startswith("- "):
            text = scrub(line.lstrip()[2:].strip())
            if text:
                out.append(text)
    return out


def _collapse(bullets: list[str], names=()) -> list[str]:
    """Later write wins on the same slot; a contradiction replaces, not stacks."""
    kept: list[str] = []
    for text in bullets:
        idx = next((i for i, prev in enumerate(kept)
                    if same_slot(prev, text, names=names)), None)
        if idx is None:
            kept.append(text)
        else:
            kept[idx] = text
    return kept


def parse_user_md(user_md: str) -> tuple[dict, dict[str, str]]:
    front, body = parse_md_text(user_md or "")
    return front or {}, {canon_section(h) or h: text
                         for h, text in split_sections(body).items()}


_YAML_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ./-]*$")
_YAML_NUMBERISH = re.compile(r"^[-+]?(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)([eE][-+]?\d+)?$")
_YAML_RESERVED = frozenset(
    "true false yes no on off y n null none ~".split())


def _yaml_scalar(value) -> str:
    """Render one frontmatter value so `yaml.safe_load` reads it back unchanged.

    The old version interpolated the value raw, so a single colon in it —
    `note: a: colon inside` — produced a file `parse_md_text` could not parse.
    USER.md is read on every turn, and `remember()` swallows the exception, so
    the failure mode was the partner model going quietly read-only forever.
    When in doubt, quote: `json.dumps` emits a valid YAML double-quoted scalar.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    text = str(value)
    if (not _YAML_PLAIN.match(text)
            or text != text.strip()
            or text.lower() in _YAML_RESERVED
            or _YAML_NUMBERISH.match(text)):
        return json.dumps(text)
    return text


def render_user_md(front: dict, sections: dict[str, str]) -> str:
    """Canonical order, empty Don't-forget/Ongoing omitted, empty Who/Helps kept
    as headings so the next extract still has a slot to fill."""
    fm = dict(front or {})
    fm.setdefault("soul", "user")
    fm.setdefault("runtime_only", True)
    phase = phase_of(sections.get(PHASE, ""))
    if phase:
        fm["phase"] = phase
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    lines.append("")
    seen = set()
    ordered = list(CANONICAL_ORDER) + [h for h in sections if h not in CANONICAL_ORDER]
    for heading in ordered:
        if heading in seen:
            continue
        seen.add(heading)
        body = (sections.get(heading) or "").strip()
        if heading in ("Don't forget", "Ongoing", "Stable") and not body:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        if body:
            lines.append(body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def phase_of(text: str) -> str:
    match = _PHASE_WORD.search(text or "")
    return match.group(1).lower() if match else ""


_NAME_RE = re.compile(r"\bname\s*(?:is|:)\s+([A-Z][\w'-]*)", re.I)


def _name_from(sections: dict[str, str]) -> str:
    for text in _bullets(sections.get("Stable", "")):
        match = _NAME_RE.search(text)
        if match:
            return match.group(1)
    who = sections.get("Who {{user}} seems to be", "")
    match = _NAME_RE.search(who)
    if match:
        return match.group(1)
    # "Grant. Works late" — a filled Who that leads with the name
    lead = (who or "").strip().split(".", 1)[0].strip()
    if lead and len(lead.split()) <= 3 and lead[0].isupper() and " " not in lead:
        return lead
    return ""


def infer_phase(sections: dict[str, str], *, days_together: int = 0) -> str:
    """Advance early → mid from evidence. Never auto-promote to late: that is
    the reveal the persona still withholds (ch. 06, ch. 11)."""
    current = phase_of(sections.get("Current relationship phase", "")) or "early"
    if current == "late":
        return "late"
    stable = _bullets(sections.get("Stable", ""))
    helps = _bullets(sections.get("What helps, and what doesn't", ""))
    who = sections.get("Who {{user}} seems to be", "")
    has_name = bool(_name_from(sections)) or bool(re.search(r"\bname is\b", who, re.I))
    evidence = len(stable) + len(helps)
    if has_name or evidence >= 3 or (days_together >= 3 and evidence >= 1):
        return "mid"
    return "early"


def _phase_line(phase: str, sections: dict[str, str]) -> str:
    if phase == "late":
        existing = (sections.get("Current relationship phase") or "").strip()
        if existing.lower().startswith("late"):
            return existing
        return "late — the fear has been named, and the shared history holds it."
    if phase == "mid":
        name = _name_from(sections)
        if name:
            return (f"mid — the formality has thawed; she knows {name}'s name "
                    "and the shape of their days.")
        return "mid — the formality has thawed; she has a picture of them now."
    return "early — careful courtesy; the formality has not yet thawed."


def _who_line(sections: dict[str, str]) -> str:
    """The narrative slot, filled only with what we can say without inventing.

    It used to splice two Stable bullets in behind the name whenever they
    matched an identity cue word, which produced "Priya. I only drink decaf
    after 4pm, this matters to me; Works as a nurse." — the user's own first
    person, semicolon-joined, duplicating the Stable list three lines below it,
    injected whole on every turn. The name is the one thing this slot can state
    that the bullets underneath it do not.
    """
    existing = scrub((sections.get("Who {{user}} seems to be") or "").strip())
    if existing:
        return existing
    name = _name_from(sections)
    return f"{name}." if name else ""


# --- merging ops into USER.md ------------------------------------------------

def apply_ops(user_md: str, ops: list[Op], names=()) -> str:
    """Merge, don't blindly append (§6.3): same-slot add becomes update,
    a contradiction replaces, `remove` drops the slot. After the ops, reconcile
    so leftover paraphrases and a frozen early-phase cannot survive the turn."""
    front, sections = parse_user_md(user_md)
    for op in ops:
        heading = canon_section(op.section) or op.section
        text = scrub(op.text)
        if not text and op.op != "remove":
            continue
        if heading in PROSE_SECTIONS:
            if op.op == "remove":
                sections[heading] = ""
            else:
                sections[heading] = text
            continue
        bullets = _bullets(sections.get(heading, ""))
        # `update`/`remove` name a line the model read in the current file, so
        # they get the loose matcher to find it. A bare `add` does not, and an
        # add that guesses wrong overwrites a fact nobody asked it to touch.
        loose = op.op in ("update", "remove")
        idx = next((i for i, prev in enumerate(bullets)
                    if same_slot(prev, text, loose=loose, names=names)), None)
        if op.op == "remove":
            if idx is not None:
                bullets.pop(idx)
        elif op.op == "update" or (op.op == "add" and idx is not None):
            if idx is not None:
                bullets[idx] = text
            else:
                bullets.append(text)
        else:  # add, no slot yet
            bullets.append(text)
        sections[heading] = "\n".join(f"- {b}" for b in _collapse(bullets, names))
    return render_user_md(front, _reconcile(sections, names=names))


def all_bullets(sections: dict[str, str]) -> list[str]:
    """Every bullet in the file, in section order — what a filing pass reads."""
    return [b for h in BULLET_SECTIONS for b in _bullets(sections.get(h, ""))]


def _home_for(heading: str, label: str | None) -> str | None:
    """Where a bullet should sit. `None` label ⇒ leave it alone; `None` return
    ⇒ the model called it chatter and it goes."""
    if label is None:
        return heading
    if label == "drop":
        return None
    if heading == "Don't forget":
        return heading      # they said "remember this". Nothing refiles that.
    return _LABEL_HOME.get(label, heading)


def _reconcile(sections: dict[str, str], *, days_together: int = 0,
               rewrite_who: bool = False,
               classified: dict[str, str] | None = None,
               names=()) -> dict[str, str]:
    """Collapse leftover soup, move bullets to the section they belong in,
    and let the phase catch up with the evidence.

    `classified` is the model's filing pass (DREAM). Without it nothing is
    refiled — a per-turn merge trusts the section the extractor already chose
    rather than second-guessing it with no evidence.
    """
    labels = classified or {}
    buckets = {h: _collapse(_bullets(sections.get(h, "")), names)
               for h in BULLET_SECTIONS}
    restack: dict[str, list[str]] = {h: [] for h in BULLET_SECTIONS}
    for heading in BULLET_SECTIONS:
        for text in buckets[heading]:
            home = _home_for(heading, label_of(text, labels, names))
            if home is not None:
                restack[home].append(text)
    for heading in BULLET_SECTIONS:
        restack[heading] = _collapse(restack[heading], names)
        sections[heading] = "\n".join(f"- {b}" for b in restack[heading])

    # scrub(), not strip(): a seeded "_(unknown yet)_" is an empty slot wearing
    # a placeholder, and treating it as filled is how the live vault's Who line
    # stayed unknown for a month.
    if rewrite_who or not scrub((sections.get("Who {{user}} seems to be") or "").strip()):
        who = _who_line(sections)
        if who:
            sections["Who {{user}} seems to be"] = who

    inferred = infer_phase(sections, days_together=days_together)
    current = phase_of(sections.get("Current relationship phase", "")) or "early"
    if PHASE_RANK[inferred] >= PHASE_RANK[current]:
        sections["Current relationship phase"] = _phase_line(inferred, sections)
    return sections


def compact_user_md(user_md: str, *, days_together: int = 0,
                    classified: dict[str, str] | None = None, names=()
                    ) -> tuple[str, PersonaDelta | None]:
    """DREAM rewrite: one bullet per topic, phase from evidence, Who filled
    if it was still an empty slot. `classified` is `classify_bullets()`'s answer
    — without it the bullets stay in the sections they are already in.
    Returns (new markdown, persona delta or None)."""
    front, sections = parse_user_md(user_md)
    sections = _reconcile(sections, days_together=days_together,
                          rewrite_who=True, classified=classified, names=names)
    new_md = render_user_md(front, sections)
    delta = persona_delta_from(sections, classified=classified, names=names)
    return new_md, delta


def persona_delta_from(sections: dict[str, str],
                       classified: dict[str, str] | None = None, names=()
                       ) -> PersonaDelta | None:
    """The What-helps bullets the model called directions about *her*.

    No classification means no delta: silence is the honest answer when nothing
    has read the bullets. Guessing from keywords is what made this feature fire
    for exactly one vocabulary.
    """
    lines = [t for t in _bullets(sections.get("What helps, and what doesn't", ""))
             if label_of(t, classified or {}, names) == "learned"]
    if not lines:
        return None
    phase = phase_of(sections.get("Current relationship phase", "")) or "early"
    return PersonaDelta(lines=lines, phase=phase, reason=persona_reason(lines))


REASON_STAKES = ("Approving makes it part of who she is, "
                 "not just what she knows about you.")
_GIST_SPLIT = re.compile(r"\s*[;—:]\s*|\.\s")
_GIST_CHARS = 72
_GIST_LINES = 3


def _gist(line: str) -> str:
    """The head of one learned bullet — its first clause, trimmed."""
    head = _GIST_SPLIT.split(line.strip(), 1)[0].strip().rstrip(".,")
    if len(head) > _GIST_CHARS:
        head = head[:_GIST_CHARS].rsplit(" ", 1)[0] + "…"
    return head


def persona_reason(lines: list[str]) -> str:
    """The one sentence you read when deciding whether to approve (§23).

    It used to be a constant — "they asked her to become this, and USER.md is
    not PERSONA.md" — which got both halves wrong. "They" is the person reading
    it. And which file the preference is moving between is the plumbing's
    business, not something anyone should have to know to answer "should she
    become this?". The two things that *are* yours: what you asked for, and that
    saying yes changes who she is rather than what she has noticed about you.
    """
    gists = [g for g in (_gist(line) for line in lines) if g]
    if not gists:
        return f"You asked her to change how she is with you. {REASON_STAKES}"
    shown = gists[:_GIST_LINES]
    more = len(gists) - len(shown)
    what = "; ".join(shown) + (f"; and {more} more" if more else "")
    return f"You asked her to change how she is with you: {what}. {REASON_STAKES}"


# --- PERSONA.md coupling (queued, never silent) ------------------------------

LEARNED_HEADING = "Learned"
PERSONA_DELTA_PATH = Path("state") / "persona_delta.json"


def read_persona_delta(vault: Path) -> dict | None:
    path = Path(vault) / PERSONA_DELTA_PATH
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_persona_delta(vault: Path, delta: PersonaDelta, *,
                        merge: bool = False, names=()) -> None:
    """Write the pending PERSONA.md proposal.

    `merge=True` unions onto what is already pending — the per-turn path only
    ever sees this turn's op, so replacing would drop everything the last four
    weeks learned. DREAM's compact has read the whole file and replaces.
    """
    path = Path(vault) / PERSONA_DELTA_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_persona_delta(vault) or {}
    if merge:
        lines = [str(x) for x in existing.get("lines") or []]
        for line in delta.lines:
            if not any(same_slot(line, kept, names=names) for kept in lines):
                lines.append(line)
        # the reason describes the lines, so merging the lines rewrites it
        delta = PersonaDelta(lines=lines, phase=delta.phase,
                             reason=persona_reason(lines))
    # A rejected or already-queued fingerprint is not rewritten into a nag.
    if existing.get("fingerprint") == delta.fingerprint() and existing.get("queued"):
        return
    vaultgit.atomic_write(path, json.dumps({
        "lines": delta.lines,
        "phase": delta.phase,
        "reason": delta.reason,
        "fingerprint": delta.fingerprint(),
        "queued": False,
    }, indent=2))


def mark_persona_delta_queued(vault: Path) -> None:
    data = read_persona_delta(vault)
    if not data:
        return
    data["queued"] = True
    vaultgit.atomic_write(Path(vault) / PERSONA_DELTA_PATH,
                          json.dumps(data, indent=2))


def apply_learned_to_persona(persona_md: str, delta: PersonaDelta) -> str:
    """Rewrite PERSONA.md's Learned section and relationship_phase. Keeps every
    other heading: this is a slice, not a new character."""
    front, body = parse_md_text(persona_md or "")
    front = dict(front or {})
    front["relationship_phase"] = delta.phase
    sections = split_sections(body)
    # Preserve the file's title line (everything before the first ##).
    preamble = body
    match = re.search(r"^##\s+", body, re.M)
    if match:
        preamble = body[:match.start()].rstrip()
    sections[LEARNED_HEADING] = "\n".join(f"- {line}" for line in delta.lines)
    parts = [preamble, ""] if preamble else []
    # Keep existing heading order; append Learned if it was missing.
    order = list(sections)
    if LEARNED_HEADING not in order:
        order.append(LEARNED_HEADING)
    for heading in order:
        parts.append(f"## {heading}")
        parts.append("")
        content = (sections.get(heading) or "").strip()
        if content:
            parts.append(content)
            parts.append("")
    fm = ["---"]
    for key, value in front.items():
        fm.append(f"{key}: {_yaml_scalar(value)}")
    fm.append("---")
    fm.append("")
    return "\n".join(fm + parts).rstrip() + "\n"


def learned_already_in_persona(persona_md: str, delta: PersonaDelta) -> bool:
    _, body = parse_md_text(persona_md or "")
    learned = split_sections(body).get(LEARNED_HEADING, "")
    return bool(learned) and all(line in learned for line in delta.lines)


# --- the quarantine (§6.3) ----------------------------------------------------

class Quarantine:
    """Low-confidence claims wait here (state/quarantine.json) until a second
    turn corroborates them; only then are they promoted into USER.md."""

    def __init__(self, path: Path, names=()):
        self.path = Path(path)
        self.names = tuple(names)
        self.items: list[dict] = []
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        vaultgit.atomic_write(self.path, json.dumps(self.items, indent=2))

    def triage(self, ops: list[Op]) -> tuple[list[Op], list[Op]]:
        """Split ops into (apply_now, newly_quarantined). An op that matches a
        quarantined claim corroborates it → promote (apply now, clear entry)."""
        apply_now: list[Op] = []
        held: list[Op] = []
        for op in ops:
            if op.op == "remove":       # removals are always honored
                apply_now.append(op)
                continue
            if op.section in PROSE_SECTIONS:
                # A phase/who rewrite is a revision of a slot we already have,
                # not a new claim that needs a second sighting.
                apply_now.append(op)
                continue
            match = next((q for q in self.items
                          if q["section"] == op.section
                          and (same_slot(q["text"], op.text, names=self.names)
                               or _overlap(q["text"], op.text, self.names)
                               >= CORROBORATION_OVERLAP)),
                         None)
            if match is not None:       # second sighting — promote (§6.3)
                self.items.remove(match)
                apply_now.append(op)
            elif op.confidence < QUARANTINE_CONFIDENCE:
                self.items.append({"section": op.section, "text": op.text,
                                   "confidence": op.confidence})
                held.append(op)
            else:
                apply_now.append(op)
        self._save()
        return apply_now, held
