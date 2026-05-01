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
import struct

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


def create_rle_img(
    int_pixels: list[int] | tuple[int, ...],
    width: int = TILE_SIZE,
    height: int = TILE_SIZE,
) -> Image.Image:
    """Java's `ImageCreater.createRLEImg(int[], 64, 64)`.

    The NewRLE decoder builds a 4096-element int array where each
    element is a packed `0x00RRGGBB` value (output of
    `ColorConverter.ycbcr2rgb`). Java wraps that in a
    `MemoryImageSource` and an AWT `Image`. We pack the same ints into
    PIL "RGB" mode bytes, three per pixel, in row-major order.
    """
    if len(int_pixels) < width * height:
        raise ValueError(
            f"int_pixels too short for {width}x{height}: got {len(int_pixels)}"
        )
    buf = bytearray(width * height * 3)
    for idx in range(width * height):
        v = int_pixels[idx]
        buf[idx * 3]     = (v >> 16) & 0xFF   # R
        buf[idx * 3 + 1] = (v >>  8) & 0xFF   # G
        buf[idx * 3 + 2] =  v        & 0xFF   # B
    return Image.frombytes("RGB", (width, height), bytes(buf))


def create_rle_img_bgr233(
    byte_pixels: bytes,
    width: int = TILE_SIZE,
    height: int = TILE_SIZE,
) -> Image.Image:
    """Java's `ImageCreater.createRLEImg_0(byte[], 64, 64)` — BGR233 path.

    The legacy `decodeRLEorJPEG3` path emits one BGR233 byte per pixel
    instead of a packed RGB888 int. This helper handles that by
    expanding each byte through `bgr233_to_rgb888()` on the fly. The
    live `decodeRLEorJPEG1` path uses `create_rle_img()` instead.
    """
    if len(byte_pixels) < width * height:
        raise ValueError(
            f"byte_pixels too short for {width}x{height}: got {len(byte_pixels)}"
        )
    buf = bytearray(width * height * 3)
    for idx in range(width * height):
        r, g, b = bgr233_to_rgb888(byte_pixels[idx])
        buf[idx * 3]     = r
        buf[idx * 3 + 1] = g
        buf[idx * 3 + 2] = b
    return Image.frombytes("RGB", (width, height), bytes(buf))
