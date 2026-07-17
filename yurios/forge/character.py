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

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


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
    # Optional durable-identity controls for backends that support them (→ ch. 26).
    trigger: str = ""                                  # LoRA trigger token
    lora: Optional[Tuple[str, float]] = None           # (path-or-name, weight)
    reference_images: List[Path] = field(default_factory=list)
    width: int = 832
    height: int = 1216

    @classmethod
    def load(cls, path: str | Path) -> "Character":
        path = Path(path)
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
            name=d["name"],
            quality_preamble=d.get("quality_preamble", ""),
            identity=d.get("identity", ""),
            base_negative=d.get("base_negative", ""),
            character_negative=d.get("character_negative", ""),
            trigger=d.get("trigger", ""),
            lora=lora,
            reference_images=refs,
            width=int(d.get("width", 832)),
            height=int(d.get("height", 1216)),
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
        if include_character and self.character_negative:
            neg.append(self.character_negative)
        if negative_extra:
            neg.append(negative_extra)
        negative = " ".join(n.strip() for n in neg if n and n.strip())
        return positive, negative
