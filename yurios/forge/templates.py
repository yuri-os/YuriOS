"""The selfie prompt-template library — the fix for "every selfie is the same pose".

ch. 26 names the failure: every "selfie" collapsing to the same five
framings. The fix is a rotated library of *settings × framing × lighting × mood
× wardrobe*, composed at request time. This loads that library from
``templates/selfie.yaml`` and composes one scene line from it — naming a slot
picks it, leaving it ``None`` rotates one in (seeded, so a seed reproduces a shot).

Wardrobe is a *tier*, not a gate: whether a given tier actually renders is decided
by the *backend* you point at, never by this file refusing it (→ ch. 26, no
enforcement posture). The shipped library stays in the everyday register; further
registers layer on from optional **overlay** files — ``load(path, overlays=[…])``
merges them key-by-key over the base sections. Overlays are user-supplied and
live outside the repo, exactly like a local checkpoint. Either file may also set
a top-level ``tool_hint`` — one line that the `take_selfie` tool description
carries verbatim (the tools server builds its description from the same merged
book, so an overlay's tiers AND its guidance are what she actually sees).

The same posture applies off-menu: a slot value that names no library entry is NOT
rejected — it passes through verbatim as the prompt fragment (her own words are a
better description of the moment than any refusal). The library is a starting
point, not a limit.

Two mechanics make a tier *real* instead of decorative:

- **per-tier negatives** — an entry may be a mapping ``{prompt, negative}``
  instead of a bare string. When a tier's look collides with what the generator
  produces by default, the positive words alone lose to the model's prior; the
  tier carries the negation its look needs and compose() returns it alongside
  the prompt (Character.assemble folds it in via ``negative_extra``).
- **pinned tiers** — ``pinned: true`` marks an entry as named-asks-only: it is
  never rotated into an unprompted shot (→ ch. 11: unprompted stays everyday;
  some tiers should only ever be something you ask for).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

import yaml


@dataclass
class SelfieBook:
    scenes: Dict[str, str]
    framings: Dict[str, str]
    lighting: Dict[str, str]
    moods: Dict[str, str]
    wardrobe: Dict[str, str]      # tier name -> outfit fragment
    # "slot/key" -> negative fragment the tier needs to render (e.g. when the
    # tier's look fights the generator's default); only named tiers carry one —
    # free-form words speak for themselves.
    negatives: Dict[str, str] = field(default_factory=dict)
    # "slot/key" never offered to the seeded rotation — named asks only.
    pinned: FrozenSet[str] = frozenset()
    # One line for the take_selfie tool description, carried verbatim — how a
    # library (or an overlay) explains its own register to the model. Empty =
    # the description stands on the key lists alone.
    tool_hint: str = ""

    @classmethod
    def load(cls, path: str | Path,
             overlays: Iterable[str | Path] = ()) -> "SelfieBook":
        """Load the base library, then merge any overlay files over it —
        each overlay's sections update the base key-by-key (overlay wins),
        so a personal file can add or re-skin tiers without forking the
        shipped library. Missing overlay paths are skipped (the caller warns)."""
        d = yaml.safe_load(Path(path).read_text()) or {}
        tool_hint = str(d.pop("tool_hint", "") or "")
        for extra in overlays:
            extra = Path(extra)
            if not extra.is_file():
                continue
            o = yaml.safe_load(extra.read_text()) or {}
            tool_hint = str(o.pop("tool_hint", "") or "") or tool_hint
            for section, entries in o.items():
                if isinstance(entries, dict):
                    d.setdefault(section, {}).update(entries)
                else:
                    d[section] = entries
        tables: Dict[str, Dict[str, str]] = {}
        negatives: Dict[str, str] = {}
        pinned = set()
        for section in ("scenes", "framings", "lighting", "moods", "wardrobe"):
            table: Dict[str, str] = {}
            for k, v in (d.get(section) or {}).items():
                key = str(k)
                if isinstance(v, dict):            # {prompt, negative?, pinned?}
                    table[key] = str(v.get("prompt", ""))
                    if v.get("negative"):
                        negatives[f"{section}/{key}"] = str(v["negative"])
                    if v.get("pinned"):
                        pinned.add(f"{section}/{key}")
                else:
                    table[key] = str(v)
            tables[section] = table
        return cls(**tables, negatives=negatives, pinned=frozenset(pinned),
                   tool_hint=tool_hint)

    def _pick(self, label: str, table: Dict[str, str], key: Optional[str],
              rng: random.Random) -> Tuple[str, str]:
        if not table and key is None:
            return ("", "")
        if key is None:
            pool = sorted(k for k in table if f"{label}/{k}" not in self.pinned)
            if not pool:
                return ("", "")
            key = rng.choice(pool)
        if key in table:
            return key, table[key]
        # Free-form pass-through (→ ch. 11, no enforcement posture): an ask that
        # names no library entry is used verbatim — her own words describe the
        # scene, expression, or outfit better than a refusal ever could.
        return key, key

    def compose(
        self,
        *,
        look: str = "",
        scene: Optional[str] = None,
        framing: Optional[str] = None,
        lighting: Optional[str] = None,
        mood: Optional[str] = None,
        wardrobe: Optional[str] = "everyday",
        situation: str = "",
        seed: Optional[int] = None,
        rotate: bool = False,
    ) -> Tuple[str, Dict[str, str], str]:
        """Return (scene_prompt, chosen, negative_extra): every picked slot is
        recorded, and any per-tier negatives come along for the render.

        `look` is her own description of the picture, and it leads — the slots
        that follow refine it rather than compete with it. It is the answer to
        the rigidity this library used to impose: five dropdowns could not
        express "curled on the window seat with my sleeves over my hands,
        grinning at you sideways", and a companion who can only pick from a menu
        takes the same photo forever.

        `rotate` decides what an unnamed slot means. False — the default now —
        means *nothing*: she said what she wanted and the rest is left to the
        renderer, or to `situation`. True restores the old seeded rotation,
        which is still what an unprompted shot with nothing to go on wants.
        Rolling dice for slots she didn't ask about is precisely how one request
        became two different photos and how every selfie ended up a costume
        change she never requested.
        """
        rng = random.Random(seed)
        chosen: Dict[str, str] = {}
        frags: List[str] = []
        neg_frags: List[str] = []
        if look.strip():
            frags.append(look.strip())
            chosen["look"] = look.strip()
        for label, table, key in (
            ("scene", self.scenes, scene),
            ("framing", self.framings, framing),
            ("wardrobe", self.wardrobe, wardrobe),
            ("lighting", self.lighting, lighting),
            ("mood", self.moods, mood),
        ):
            if key is None and not rotate:
                continue                        # unasked is unasked
            name, frag = self._pick(label, table, key, rng)
            if name:
                chosen[label] = name
                neg = self.negatives.get(f"{label}/{name}")
                if neg:
                    neg_frags.append(neg)
            if frag:
                frags.append(frag)
        # The world as it is, last: it fills what nobody named (the hour, the
        # weather, the room she is actually in) and must never argue with what
        # she did name, which is why it comes after every slot above.
        if situation.strip():
            frags.append(situation.strip())
            chosen["situation"] = situation.strip()
        return " ".join(frags), chosen, " ".join(neg_frags)
