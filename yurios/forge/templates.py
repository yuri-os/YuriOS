"""The selfie prompt-template library — the fix for "every selfie is the same pose".

ch. 26 names the failure: every "selfie" collapsing to the same five
framings. The fix is a rotated library of *settings × framing × lighting × mood
× wardrobe*, composed at request time. This loads that library from
``templates/selfie.yaml`` and composes one scene line from it — naming a slot
picks it, leaving it ``None`` rotates one in (seeded, so a seed reproduces a shot).

Wardrobe is a *tier*, not a gate. The intimate tier is here as a normal capability
the user can ask for at any time (→ ch. 11, user-initiated, never gated). Whether
a given tier actually renders is decided by the *backend* you point at, not by this
file refusing it (→ ch. 11, the model requirement; → ch. 26, no enforcement posture).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass
class SelfieBook:
    scenes: Dict[str, str]
    framings: Dict[str, str]
    lighting: Dict[str, str]
    moods: Dict[str, str]
    wardrobe: Dict[str, str]      # tier name -> outfit fragment

    @classmethod
    def load(cls, path: str | Path) -> "SelfieBook":
        d = yaml.safe_load(Path(path).read_text()) or {}

        def norm(section: str) -> Dict[str, str]:
            return {str(k): str(v) for k, v in (d.get(section) or {}).items()}

        return cls(
            scenes=norm("scenes"),
            framings=norm("framings"),
            lighting=norm("lighting"),
            moods=norm("moods"),
            wardrobe=norm("wardrobe"),
        )

    def _pick(self, table: Dict[str, str], key: Optional[str], rng: random.Random) -> Tuple[str, str]:
        if not table:
            return ("", "")
        if key is None:
            key = rng.choice(sorted(table))
        if key not in table:
            raise KeyError(f"unknown option {key!r}; have: {', '.join(sorted(table))}")
        return key, table[key]

    def compose(
        self,
        *,
        scene: Optional[str] = None,
        framing: Optional[str] = None,
        lighting: Optional[str] = None,
        mood: Optional[str] = None,
        wardrobe: Optional[str] = "everyday",
        seed: Optional[int] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """Return (scene_prompt, chosen) where chosen records every picked slot."""
        rng = random.Random(seed)
        chosen: Dict[str, str] = {}
        frags: List[str] = []
        for label, table, key in (
            ("scene", self.scenes, scene),
            ("framing", self.framings, framing),
            ("wardrobe", self.wardrobe, wardrobe),
            ("lighting", self.lighting, lighting),
            ("mood", self.moods, mood),
        ):
            name, frag = self._pick(table, key, rng)
            if name:
                chosen[label] = name
            if frag:
                frags.append(frag)
        return " ".join(frags), chosen
