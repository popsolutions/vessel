"""Port of `com.kvm.decoder.ImageCreater` — PIL-backed tile renderer.

Java's ImageCreater turns the codec's raw byte buffers into AWT
`BufferedImage` instances:
    - `createPaletteImage(byte[] pixels, byte[] palette, int w, int h)`
        wraps an indexed-colour 8-bit-per-pixel buffer.
    - `jpegDecodeAsImage(byte[] header, byte[] body)` concatenates
        the synthetic JPEG header + chassis-sent body + EOI tail and
        feeds it to `ImageIO.read()`.

We mirror those two entry points using Pillow:
    - `palette_image()`  → PIL.Image in "P" mode with a custom
        palette, converted to "RGB" so the upstream framebuffer
        stitching code can paste it directly.
    - `jpeg_decode_as_image()` → PIL.Image read from a BytesIO
        containing `create_syn_head_data() + body + TAIL`.

The chassis NewRLE codec uses one of two pixel encodings per tile:
    1. **BGR233 palette tiles** — 64×64 indices (one byte per pixel),
       palette is up-to-256 BGR233 colours.
    2. **JPEG tiles** — header-stripped JFIF body the chassis sends
       when it has too much detail to RLE efficiently.

Both produce 64×64 PIL.Image objects that the renderer pastes into
the full-frame buffer at the tile's (x, y) origin.
"""
from __future__ import annotations

import io
import logging

from PIL import Image

from .color_converter import bgr233_to_rgb888
from .jpeg_data import TAIL, create_syn_head_data

log = logging.getLogger(__name__)

TILE_SIZE = 64


def palette_image(
    pixels: bytes,
    palette_bgr233: bytes,
    width: int = TILE_SIZE,
    height: int = TILE_SIZE,
) -> Image.Image:
    """Build a PIL.Image from indexed-colour pixel + BGR233 palette.

    Mirrors Java's `createPaletteImage`. Pillow's "P" mode wants a
    768-byte palette (256 RGB triples), so we expand each BGR233 byte
    in the supplied palette into the matching 24-bit RGB triple.
    """
    if len(pixels) < width * height:
        raise ValueError(
            f"pixels too short for {width}x{height}: got {len(pixels)}"
        )

    rgb_palette = bytearray(768)
    for i, bgr233 in enumerate(palette_bgr233[:256]):
        r, g, b = bgr233_to_rgb888(bgr233)
        rgb_palette[i * 3]     = r
        rgb_palette[i * 3 + 1] = g
        rgb_palette[i * 3 + 2] = b

    img = Image.frombytes("P", (width, height), bytes(pixels[:width * height]))
    img.putpalette(bytes(rgb_palette))
    return img.convert("RGB")


def jpeg_decode_as_image(jpeg_body: bytes) -> Image.Image:
    """Reassemble + decode a chassis-stripped JPEG tile.

    Java does the same in JPEGData.createSynHeadData() + ImageIO.read().
    The chassis omits the SOI/APP0/DQT/DHT/SOF/SOS markers and the
    trailing EOI to save bandwidth — we rebuild them and append the
    EOI `TAIL`. On decode failure return a neutral grey tile so the
    framebuffer doesn't get garbage (Java caught the IOException and
    skipped the tile; we keep the frame intact).
    """
    blob = create_syn_head_data() + jpeg_body + TAIL
    try:
        with Image.open(io.BytesIO(blob)) as decoded:
            decoded.load()
            return decoded.convert("RGB")
    except Exception as exc:
        log.warning(
            "JPEG tile decode failed (body=%d bytes): %s",
            len(jpeg_body), exc,
        )
        return Image.new("RGB", (TILE_SIZE, TILE_SIZE), color=(64, 64, 64))


def blank_tile(color: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Empty 64×64 tile — used as the initial framebuffer fill."""
    return Image.new("RGB", (TILE_SIZE, TILE_SIZE), color=color)
