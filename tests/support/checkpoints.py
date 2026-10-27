"""Fake checkpoint files for the local-camera tests: headers, no weights."""
from __future__ import annotations

import json
import struct


def write_safetensors_header(path, keys, metadata=None, shapes=None):
    """A real safetensors header (8-byte length + JSON) with no tensor data —
    which is all the sniffing and the metadata reads ever look at. `shapes`
    gives chosen tensors a shape other than [1]."""
    shapes = shapes or {}
    header = {k: {"dtype": "F32", "shape": shapes.get(k, [1]), "data_offsets": [0, 4]}
              for k in keys}
    if metadata is not None:
        header["__metadata__"] = metadata
    blob = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\x00" * 4)
    return path
