"""The character identity — the locked look, expressed as prompt parts.

This is the image-side of the SOUL (→ yuri-soul/): a small, human-editable file
that *is* the character's appearance, kept separate from any one generator. The
four fields below mirror the locked D-011 register carried in
``artworks/manifest.json`` so a runtime selfie reads as the *same person* as the
brand art (→ ch. 26, "one source of truth").

Prompt assembly follows ``artworks/generate.py`` exactly:

    positive = quality_preamble + identity + scene
    negative = base_negative + (character_negative if a person is in frame)

so on-demand selfies stay on-register with the batch-generated canon set.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

# The house register every appearance file inherits from (see `Character.register`).
REGISTER_PATH = Path(__file__).resolve().parent / "characters" / "_register.yaml"


@dataclass
class Character:
    name: str
    # The locked register preamble (D-011: 2.5D semi-realistic anime). {AAA}
    quality_preamble: str = ""
    # Who she is, visually — the {YURI} block: face, build, hair, signature marks.
    identity: str = ""
    # Negatives every render gets (no text/watermark/UI…).
    base_negative: str = ""
    # Negatives that only make sense when a person is in frame (the clothing /
    # anatomy guard). Appended only when include_character=True (→ generate.py).
    character_negative: str = ""
    # Her *own* guard, appended after the inherited one rather than replacing it.
    # This is the field a derived appearance file writes (characters/appearance.py):
    # "no tan — she is pallid", "exactly two horns". Kept separate so inheriting
    # the house's structural guard and adding to it are not the same edit — the
    # register can improve later and every character gets the improvement.
    character_negative_extra: str = ""
    # Optional durable-identity controls for backends that support them (→ ch. 26).
    trigger: str = ""                                  # LoRA trigger token
    lora: Optional[Tuple[str, float]] = None           # (path-or-name, weight)
    reference_images: List[Path] = field(default_factory=list)
    width: int = 832
    height: int = 1216

    @classmethod
    def register(cls) -> "Character":
        """The house register — quality preamble, text guard, structural anatomy
        guard, canvas — everything about *how* a picture is made rather than who
        is in it. A per-character appearance file inherits this, so a character
        derived from an imported card need only say who she is."""
        return cls.load(REGISTER_PATH, defaults=cls(name=""))

    @classmethod
    def neutral(cls, name: str) -> "Character":
        """The stand-in for a character with no appearance file yet.

        The rule it exists to enforce: *never render someone else's likeness*.
        Before this, one hardcoded file meant every character in the house wore
        Yuri's face — cat ears and all — and the provenance sidecar cheerfully
        recorded it as hers. An unknown look is a bad photo; the wrong person's
        look is a broken promise, so the fallback describes nobody in
        particular and lets the backend invent a face.
        """
        return replace(
            cls.register(), name=name,
            identity=("a person, appearance unspecified — render them plainly "
                      "and naturally, with no distinguishing features assumed"))

    @classmethod
    def load(cls, path: str | Path, *,
             defaults: Optional["Character"] = None) -> "Character":
        """Load an appearance file. Any field the file omits falls back to
        `defaults` (the house register, normally) rather than to an empty
        string — that inheritance is what lets a derived file be two fields
        long. Passing `defaults=None` keeps the old all-or-nothing behaviour,
        which is what the shipped, fully-specified characters want."""
        path = Path(path)
        base = defaults if defaults is not None else cls(name="")
        d = yaml.safe_load(path.read_text()) or {}
        lora = None
        if d.get("lora"):
            lp = Path(d["lora"]["path"])
            # Resolve a relative LoRA path against the character file's folder (not the
            # CWD); leave absolute paths and bare HF repo ids alone.
            if not lp.is_absolute() and (path.parent / lp).exists():
                lp = path.parent / lp
            lora = (str(lp), float(d["lora"].get("weight", 1.0)))
        refs = [Path(p) for p in d.get("reference_images", [])]
        # Resolve reference paths relative to the character file's folder.
        refs = [r if r.is_absolute() else (path.parent / r) for r in refs]
        return cls(
            name=d.get("name", base.name),
            quality_preamble=d.get("quality_preamble", base.quality_preamble),
            identity=d.get("identity", base.identity),
            base_negative=d.get("base_negative", base.base_negative),
            character_negative=d.get("character_negative", base.character_negative),
            character_negative_extra=d.get("character_negative_extra",
                                           base.character_negative_extra),
            trigger=d.get("trigger", base.trigger),
            lora=lora if lora is not None else base.lora,
            reference_images=refs or list(base.reference_images),
            width=int(d.get("width", base.width)),
            height=int(d.get("height", base.height)),
        )

    def assemble(
        self,
        scene_prompt: str,
        *,
        include_character: bool = True,
        negative_extra: str = "",
    ) -> Tuple[str, str]:
        """Build the (positive, negative) pair for a scene.

        ``include_character=False`` renders scenery only (worldbuilding atlas,
        → ch. 26) and drops the anatomy/clothing guard so no figure is primed
        into an empty room — the exact rule generate.py uses.
        """
        parts = [self.quality_preamble]
        if include_character:
            if self.trigger:
                parts.append(self.trigger)          # LoRA path uses the token
            parts.append(self.identity)
        parts.append(scene_prompt)
        positive = " ".join(p.strip() for p in parts if p and p.strip())

        neg = [self.base_negative]
        if include_character:
            neg.append(self.character_negative)
            neg.append(self.character_negative_extra)
        if negative_extra:
            neg.append(negative_extra)
        negative = " ".join(n.strip() for n in neg if n and n.strip())
        return positive, negative
