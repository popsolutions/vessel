"""Port of `com.kvm.decoder.ImageDecoder` — NewRLE/JPEG codec entry point.

This is the **skeleton** of the 1049-line Java state machine. The full
tile-by-tile bytecode interpretation (RLE variants 0/1, JPEG variants
2/3, copy-from-neighbour variants 4/5/6, plus all the per-tile palette
fixup) is the next port pass. What lives here today:

    - `decode_rle_or_jpeg1(payload, prev_frame=None) -> PIL.Image`
        the public entry point; matches Java's
        `decodeRLEorJPEG1(byte[], BufferedImage)` signature.
    - `parse_header(payload)`  → frame width/height/blockcount
    - `iterate_tile_tokens(payload, header)` → generator that yields
        (block_index, zip_type, body_bytes) tuples. The body parser
        is a TODO — current implementation does NOT advance through
        tile bodies, it just walks the count metadata so the rest of
        the pipeline can be wired and tested end-to-end.

Until the full tile interpreter lands, the entry point falls back to
returning a blank frame (or the previous frame if supplied) so the
upstream WebSocket pipeline doesn't crash on first NewRLE packet.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator

from PIL import Image

from .image_block import ImageBlock
from .image_creater import TILE_SIZE, blank_tile

log = logging.getLogger(__name__)


# ----- Header layout (NewRLE frame envelope) -----
# Java's ImageDecoder reads from a fixed offset; layout established
# from JPEGData/ImageDecoder.java header parsing:
#     [0..1]   frame width  (uint16 BE)
#     [2..3]   frame height (uint16 BE)
#     [4..5]   block_x_count (tiles per row, uint16 BE)
#     [6..7]   block_y_count (tile rows,     uint16 BE)
#     [8]      flags  (bit 0 = full keyframe, bit 1 = subsample 4:2:0)
#     [9..]    tile token stream
#
# These offsets MUST be re-validated against the original Java reader
# once the full state machine is ported — they are documented from the
# field reordering pass during chassis traffic capture and may need
# adjusting for chassis revisions that emit extra header bytes.
HEADER_BYTES = 10


@dataclass
class FrameHeader:
    width: int
    height: int
    block_x_count: int
    block_y_count: int
    flags: int

    @property
    def block_count(self) -> int:
        return self.block_x_count * self.block_y_count


def parse_header(payload: bytes) -> FrameHeader:
    """Pull the 10-byte NewRLE frame envelope off the front of payload."""
    if len(payload) < HEADER_BYTES:
        raise ValueError(
            f"NewRLE payload too short for header: {len(payload)} bytes"
        )
    w, h, bxc, byc = struct.unpack(">HHHH", payload[0:8])
    flags = payload[8]
    return FrameHeader(
        width=w,
        height=h,
        block_x_count=bxc,
        block_y_count=byc,
        flags=flags,
    )


def iterate_tile_tokens(
    payload: bytes,
    header: FrameHeader,
) -> Iterator[tuple[int, int, bytes]]:
    """Walk the tile token stream. Currently a count-only walker.

    Each Java tile token starts with a control byte whose top 3 bits
    are `block_type` and next 3 bits are `block_rle_type` (Java calls
    these `zip_type`). The body length depends on which combination is
    set. The full byte-for-byte parser is the next port pass — for now
    we only emit the index sequence so callers can wire a TODO path.
    """
    cursor = HEADER_BYTES
    end = len(payload)
    for block_index in range(header.block_count):
        if cursor >= end:
            log.debug(
                "tile stream truncated at block %d/%d",
                block_index, header.block_count,
            )
            return
        token = payload[cursor]
        zip_type = (token >> 3) & 0x07   # Java: getBlockRleType
        cursor += 1
        # Body length is variable. The full state machine sets it
        # per zip_type (RLE chains read until run-length exhaust;
        # JPEG tiles read a uint16 length prefix; copy variants
        # have zero body). Skeleton emits an empty body and lets
        # the caller no-op until the parser lands.
        yield block_index, zip_type, b""


def decode_rle_or_jpeg1(
    payload: bytes,
    prev_frame: Image.Image | None = None,
) -> Image.Image:
    """Public entry — Java's `decodeRLEorJPEG1(buffer, prevFrame)`.

    SKELETON: parses the header, walks tile tokens, but does not yet
    interpret tile bodies. Returns the previous frame (if supplied)
    or a black frame at the header's resolution so upstream rendering
    can be plumbed end-to-end before the full state machine lands.
    """
    try:
        header = parse_header(payload)
    except ValueError as exc:
        log.warning("NewRLE header parse failed: %s", exc)
        return prev_frame if prev_frame is not None else blank_tile()

    width = header.block_x_count * TILE_SIZE
    height = header.block_y_count * TILE_SIZE
    frame = (prev_frame.copy()
             if prev_frame is not None and prev_frame.size == (width, height)
             else Image.new("RGB", (width, height), color=(0, 0, 0)))

    blocks: list[ImageBlock | None] = [None] * header.block_count

    # Skeleton walk — the body parser is the next port pass.
    for block_index, zip_type, _body in iterate_tile_tokens(payload, header):
        bx = block_index % header.block_x_count
        by = block_index // header.block_x_count
        blocks[block_index] = ImageBlock(
            x=bx * TILE_SIZE,
            y=by * TILE_SIZE,
            block_rle_type=zip_type,
        )
        # TODO(next-pass): dispatch by zip_type
        #   0/1 → RLE chain → palette_image(...)
        #   2/3 → JPEG body → jpeg_decode_as_image(...)
        #   4/5/6 → clone from neighbour tile in `blocks[]`

    return frame


__all__ = [
    "FrameHeader",
    "HEADER_BYTES",
    "decode_rle_or_jpeg1",
    "iterate_tile_tokens",
    "parse_header",
]
