"""Replay a captured KVM byte stream as a sequence of PNG frames.

Used by the GUI's WebSocket bridge as a stand-in for the live capturer
until the K3.5 handshake client lands. Same pipeline as the live path
will use:

    raw bytes -> parse_kvm_stream -> reassemble_images
              -> decode_old_rle    -> bgr233_to_rgb888
              -> PIL.Image -> PNG bytes -> WebSocket

The browser side draws each PNG into a `<canvas>` as it arrives.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

from .codec_old import bgr233_to_rgb888, decode_old_rle
from .transport import Image as _DecImage
from .transport import parse_kvm_stream, reassemble_images


def image_to_png_bytes(image: _DecImage) -> bytes:
    """Decode one reassembled image and encode it as PNG."""
    from PIL import Image as PILImage  # local import: optional dep at runtime

    fb = decode_old_rle(bytes(image.data), image.width, image.height)
    rgb = bgr233_to_rgb888(fb)
    pim = PILImage.frombytes("RGB", (image.width, image.height), rgb)
    buf = io.BytesIO()
    pim.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def replay_capture_to_pngs(capture_path: Path | str) -> Iterator[tuple[int, bytes]]:
    """Yield (img_id, png_bytes) for every reassembled image in a capture.

    `capture_path` must point at a server→client `.bin` (extracted with
    `tshark follow,tcp,raw,N`). Caller controls pacing — the replay
    yields all available frames immediately; the WebSocket loop adds
    its own delay.
    """
    data = Path(capture_path).read_bytes()
    frames = parse_kvm_stream(data)
    for img in reassemble_images(frames):
        if not img.complete:
            continue
        yield img.img_id, image_to_png_bytes(img)
