"""image-forge (SPEC §7.6; → book ch. 26) — her camera.

The swappable image service behind one stable API. YuriOS ships only the two
GPU-free backends — `mock` (deterministic placeholder cards; the tests) and
`openrouter` (any OpenRouter image route; the host picks the model via
SELFIE_MODEL). See README.md for the deliberate deviations from the general
image-forge.

    from yurios.forge import ImageForge
    forge.selfie(scene="window", mood="happy")     # an image "of her"
"""

from .backends import ImageBackend, make_backend
from .character import Character
from .service import ImageForge
from .templates import SelfieBook
from .types import Capabilities, EditRequest, GenRequest, ImageResult

__all__ = [
    "ImageForge", "Character", "SelfieBook", "ImageBackend", "make_backend",
    "GenRequest", "EditRequest", "ImageResult", "Capabilities",
]
