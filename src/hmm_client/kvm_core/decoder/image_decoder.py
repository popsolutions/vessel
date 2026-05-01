"""Port of `com.kvm.decoder.ImageDecoder` — NewRLE/JPEG codec.

This implements `decodeRLEorJPEG1` (the live applet's entry point) and
its helper `decodeRle`. Reference Java source:
`re/decompiled/sources/com/kvm/decoder/ImageDecoder.java` (1049 lines).

## Wire layout

The chassis sends one packet per frame. After our chunk-reassembly
strips the protocol header, the payload starts with a single zero
byte (the Java `data[0]` preamble) followed immediately by the tile
token stream. Frame width/height are NOT in the payload — they're
carried in the surrounding session state from BLADE_STATE handshake
and supplied as parameters.

## Tile token byte

Each tile starts with a control byte::

    bits 7..5  zip_type    (top-level tile type, 0..6)
    bits 4..2  rZipType    (RLE sub-type when zip_type ∈ {0, 1})
    bits 1..0  unused (consumed by following bytes)

`zip_type` dispatch:

    0 / 1   RLE tile.    Sub-type rZipType selects the RLE variant
                          (decodeRle types 0..3) or copies an earlier
                          tile (rZipType 4..7). zip_type=1 marks
                          this as the LAST tile of a row (sets
                          fill/cut metadata for edge cropping).

    2 / 3   JPEG tile.   Body is `(byte[1]<<8 | byte[2])` bytes of
                          chassis-stripped JFIF payload. zip_type=3
                          is the row-edge variant.

    4       Skip tile.   No body, no rendering.

    5       Copy from above tile (blocknum - blockXcount).

    6       Copy from left tile (blocknum - 1).

## decodeRle types

Java's `decodeRle()` handles types 0..3:

    0   Solid colour.    Palette = 1 YCbCr triple, body = none.
                         All 4096 pixels = ycbcr2rgb(palette[0..2]).

    1   2-colour RLE.    Palette = 2 YCbCr triples (6 bytes),
                         body = bitstream. Each token is a 6-bit
                         run length + (when length=64) a 1-bit
                         "don't flip colour" flag. Default behaviour:
                         alternate between the two palette colours
                         after each run.

    2   3-colour RLE.    Palette = 3 YCbCr triples (9 bytes),
                         body = byte stream. Each byte = 6-bit
                         run length + 2-bit palette index.

    3   4-colour RLE.    Palette = 4 YCbCr triples (12 bytes),
                         body = byte stream (same encoding as type 2).

When this tile inherits its palette from an earlier tile (outer rZipType
4..7), `coefficient = 0` and the body length offset is reduced by the
palette-byte count — the decoder reuses the prior palette in-place
(with optional swap for rZipType 5/7).
"""
from __future__ import annotations

import logging
from typing import Sequence

from .color_converter import ycbcr2rgb
from .image_block import ImageBlock
from .image_creater import (
    TILE_SIZE,
    blank_tile,
    create_rle_img,
    jpeg_decode_as_image,
)

log = logging.getLogger(__name__)


_TILE_PIXELS = TILE_SIZE * TILE_SIZE   # 4096

# Java's `Base.USB_KEY_CTRL = 224 = 0xE0` — top-3-bits mask.
_ZIPTYPE_MASK = 0xE0
_RZIPTYPE_MASK = 0x1C   # bits 4..2


class DecoderError(RuntimeError):
    """Raised when a tile token cannot be parsed."""


def _to_signed_byte(value: int) -> int:
    """Java byte arithmetic is signed; ColorConverter is bug-compatible
    when fed the signed value, so feed it the way Java did."""
    return value if value < 128 else value - 256


def _to_unsigned_byte(value: int) -> int:
    return value & 0xFF


class NewRleDecoder:
    """Stateful NewRLE/JPEG decoder.

    Holds tile-grid bookkeeping across calls so the copy-from-neighbour
    tile types (rZipType 4..7, zip_type 5/6) can reach back into the
    previous frame's blocks.
    """

    def __init__(self, image_width: int, image_height: int) -> None:
        self._init(image_width, image_height)

    def _init(self, image_width: int, image_height: int) -> None:
        self.image_width = image_width
        self.image_height = image_height
        self.block_x_count = (
            image_width // TILE_SIZE
            + (0 if image_width % TILE_SIZE == 0 else 1)
        )
        self.block_y_count = (
            image_height // TILE_SIZE
            + (0 if image_height % TILE_SIZE == 0 else 1)
        )
        self.block_count = self.block_x_count * self.block_y_count
        self.block_cut_width = TILE_SIZE - (
            self.block_x_count * TILE_SIZE - image_width
        )
        self.block_cut_height = TILE_SIZE - (
            self.block_y_count * TILE_SIZE - image_height
        )
        self.image_blocks: list[ImageBlock | None] = [None] * self.block_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decode(
        self,
        payload: bytes,
        image_width: int,
        image_height: int,
    ) -> list[ImageBlock | None]:
        """Java's `decodeRLEorJPEG1`. Returns a list of decoded tiles.

        Use `compose_frame()` to stitch the tiles into a final
        PIL.Image at chassis resolution.
        """
        if self.image_width != image_width or self.image_height != image_height:
            self._init(image_width, image_height)

        i = 1   # Java starts at i=1 — skip the leading zero preamble
        blocknum = 0
        end = len(payload)

        while i < end and blocknum < self.block_count:
            i2 = i
            token = payload[i2] & 0xFF
            zip_type = (token & _ZIPTYPE_MASK) >> 5
            r_zip_type = (token & _RZIPTYPE_MASK) >> 2

            try:
                syclen, image_block = self._dispatch_tile(
                    payload, i2, blocknum, zip_type, r_zip_type
                )
            except DecoderError as exc:
                log.warning(
                    "tile decode error at block %d (i=%d): %s",
                    blocknum, i2, exc,
                )
                return list(self.image_blocks)
            except IndexError as exc:
                log.warning(
                    "tile read out of bounds at block %d (i=%d): %s",
                    blocknum, i2, exc,
                )
                return list(self.image_blocks)

            if image_block is not None:
                image_block.x = (blocknum % self.block_x_count) * TILE_SIZE
                image_block.y = (blocknum // self.block_x_count) * TILE_SIZE
                # zip_type 1 / 3 mark row-edge tiles that need cropping.
                if zip_type == 1 or zip_type == 3:
                    image_block.fill = True
                    image_block.cut_width = self.block_cut_width
                    image_block.cut_height = self.block_cut_height
                    if (blocknum // self.block_x_count) < self.block_y_count - 1:
                        image_block.cut_height = TILE_SIZE
                    elif blocknum != self.block_count - 1:
                        image_block.cut_width = TILE_SIZE

            blocknum += 1
            i = i2 + syclen

        return list(self.image_blocks)

    # ------------------------------------------------------------------
    # Tile dispatch
    # ------------------------------------------------------------------

    def _dispatch_tile(
        self,
        payload: bytes,
        i2: int,
        blocknum: int,
        zip_type: int,
        r_zip_type: int,
    ) -> tuple[int, ImageBlock | None]:
        """Mirror Java's outer switch on zip_type."""
        if zip_type in (0, 1):
            return self._decode_rle_dispatch(payload, i2, blocknum, r_zip_type)
        if zip_type in (2, 3):
            return self._decode_jpeg(payload, i2, blocknum, zip_type)
        if zip_type == 4:
            # Skip tile — Java just advances 1 byte and emits no block.
            return 1, None
        if zip_type == 5:
            return 1, self._copy_block(blocknum, blocknum - self.block_x_count)
        if zip_type == 6:
            return 1, self._copy_block(blocknum, blocknum - 1)
        raise DecoderError(f"unknown zip_type={zip_type}")

    def _decode_rle_dispatch(
        self,
        payload: bytes,
        i2: int,
        blocknum: int,
        r_zip_type: int,
    ) -> tuple[int, ImageBlock | None]:
        """Mirror Java's inner switch on rZipType for zip_type ∈ {0, 1}."""
        if r_zip_type in (0, 1, 2, 3):
            syclen, block = self._decode_rle(payload, i2, blocknum, r_zip_type, None)
            self.image_blocks[blocknum] = block
            return syclen, block

        if r_zip_type == 4:
            # Reuse left tile. If it was solid (type 0), no re-decode.
            prev = self.image_blocks[blocknum - 1]
            if prev is None:
                raise DecoderError("rZipType 4 with no left neighbour")
            if prev.block_rle_type == 0:
                block = ImageBlock()
                block.clone_metadata_from(prev)
                block.image = prev.image
                block.pix_colors = prev.pix_colors
                self.image_blocks[blocknum] = block
                return 1, block
            pix_colors = bytes(prev.pix_colors)
            syclen, block = self._decode_rle(
                payload, i2, blocknum, prev.block_rle_type, pix_colors
            )
            self.image_blocks[blocknum] = block
            return syclen, block

        if r_zip_type == 5:
            # Reuse left tile with palette-swap (only meaningful for
            # 2-colour palette: swap colour 0 and colour 1).
            prev = self.image_blocks[blocknum - 1]
            if prev is None:
                raise DecoderError("rZipType 5 with no left neighbour")
            cols = prev.pix_colors
            pix_colors = bytearray(cols)
            if prev.block_rle_type == 1 and len(cols) >= 6:
                pix_colors[0:3] = cols[3:6]
                pix_colors[3:6] = cols[0:3]
            syclen, block = self._decode_rle(
                payload, i2, blocknum, prev.block_rle_type, bytes(pix_colors)
            )
            self.image_blocks[blocknum] = block
            return syclen, block

        if r_zip_type == 6:
            # Reuse above tile.
            prev = self.image_blocks[blocknum - self.block_x_count]
            if prev is None:
                raise DecoderError("rZipType 6 with no upper neighbour")
            if prev.block_rle_type == 0:
                block = ImageBlock()
                block.clone_metadata_from(prev)
                block.image = prev.image
                block.pix_colors = prev.pix_colors
                self.image_blocks[blocknum] = block
                return 1, block
            pix_colors = bytes(prev.pix_colors)
            syclen, block = self._decode_rle(
                payload, i2, blocknum, prev.block_rle_type, pix_colors
            )
            self.image_blocks[blocknum] = block
            return syclen, block

        if r_zip_type == 7:
            # Reuse above tile with palette-swap (same swap rules as 5).
            prev = self.image_blocks[blocknum - self.block_x_count]
            if prev is None:
                raise DecoderError("rZipType 7 with no upper neighbour")
            cols = prev.pix_colors
            pix_colors = bytearray(cols)
            if prev.block_rle_type == 1 and len(cols) >= 6:
                pix_colors[0:3] = cols[3:6]
                pix_colors[3:6] = cols[0:3]
            syclen, block = self._decode_rle(
                payload, i2, blocknum, prev.block_rle_type, bytes(pix_colors)
            )
            self.image_blocks[blocknum] = block
            return syclen, block

        raise DecoderError(f"unknown rZipType={r_zip_type}")

    # ------------------------------------------------------------------
    # JPEG tile (zip_type 2/3)
    # ------------------------------------------------------------------

    def _decode_jpeg(
        self,
        payload: bytes,
        i2: int,
        blocknum: int,
        zip_type: int,
    ) -> tuple[int, ImageBlock]:
        length = ((payload[i2 + 1] & 0xFF) << 8) + (payload[i2 + 2] & 0xFF)
        body = bytes(payload[i2 + 3 : i2 + 3 + length])
        pil = jpeg_decode_as_image(body)
        block = ImageBlock(image=pil, block_type=zip_type)
        self.image_blocks[blocknum] = block
        return 3 + length, block

    def _copy_block(
        self,
        blocknum: int,
        source_index: int,
    ) -> ImageBlock | None:
        if source_index < 0 or source_index >= self.block_count:
            raise DecoderError(
                f"copy block source out of range: src={source_index}"
            )
        src = self.image_blocks[source_index]
        if src is None:
            raise DecoderError(
                f"copy block source not yet decoded: src={source_index}"
            )
        block = ImageBlock()
        block.clone_metadata_from(src)
        block.image = src.image
        block.pix_colors = src.pix_colors
        self.image_blocks[blocknum] = block
        return block

    # ------------------------------------------------------------------
    # decodeRle — types 0/1/2/3
    # ------------------------------------------------------------------

    def _read_palette(
        self,
        payload: bytes,
        src_pos: int,
        rle_type: int,
        pix_colors: bytes | None,
    ) -> tuple[bytes, int, int]:
        """Mirror Java's `butPixColors` setup. Returns (palette_bytes,
        srcColPos within palette, coefficient {0|1})."""
        if pix_colors is not None:
            return bytes(pix_colors), 0, 0
        src_col_pos2 = src_pos + 1 if rle_type == 0 else src_pos + 3
        # Java reads up to 12 palette bytes (4 colours × 3 bytes).
        col_len = min(12, len(payload) - src_col_pos2)
        col_len = max(col_len, 0)
        palette = bytes(payload[src_col_pos2 : src_col_pos2 + col_len])
        return palette, 0, 1

    def _decode_rle(
        self,
        payload: bytes,
        src_pos: int,
        blocknum: int,
        rle_type: int,
        pix_colors: bytes | None,
    ) -> tuple[int, ImageBlock]:
        """Java's `decodeRle()` — RLE types 0/1/2/3."""
        butPixColors, src_col_pos, coefficient = self._read_palette(
            payload, src_pos, rle_type, pix_colors
        )

        if rle_type == 0:
            image_data, syclen = self._decode_rle_type0(butPixColors, src_col_pos)
        elif rle_type == 1:
            image_data, syclen = self._decode_rle_type1(
                payload, src_pos, butPixColors, src_col_pos, coefficient,
            )
        elif rle_type in (2, 3):
            image_data, syclen = self._decode_rle_type23(
                payload, src_pos, butPixColors, src_col_pos, coefficient, rle_type,
            )
        else:
            raise DecoderError(f"decodeRle type {rle_type} not supported")

        pil = create_rle_img(image_data, TILE_SIZE, TILE_SIZE)
        block = ImageBlock(
            image=pil,
            block_rle_type=rle_type,
            block_type=0,
            pix_colors=butPixColors,
        )
        return syclen, block

    def _decode_rle_type0(
        self,
        palette: bytes,
        src_col_pos: int,
    ) -> tuple[list[int], int]:
        """Solid-colour tile: one YCbCr triple expanded across 4096 pixels."""
        if src_col_pos + 3 > len(palette):
            raise DecoderError("type 0 palette too short")
        colour = ycbcr2rgb(
            palette[src_col_pos + 0],
            palette[src_col_pos + 1],
            palette[src_col_pos + 2],
        )
        return [colour] * _TILE_PIXELS, 4

    def _decode_rle_type1(
        self,
        payload: bytes,
        src_pos: int,
        palette: bytes,
        src_col_pos: int,
        coefficient: int,
    ) -> tuple[list[int], int]:
        """2-colour RLE — bitstream of 6-bit run lengths + flip flag."""
        if src_col_pos + 6 > len(palette):
            raise DecoderError("type 1 palette too short")
        bufColor1 = ycbcr2rgb(
            palette[src_col_pos + 0],
            palette[src_col_pos + 1],
            palette[src_col_pos + 2],
        )
        bufColor2 = ycbcr2rgb(
            palette[src_col_pos + 3],
            palette[src_col_pos + 4],
            palette[src_col_pos + 5],
        )
        bufColor3 = bufColor1

        length = ((payload[src_pos + 1] & 0xFF) << 8) + (payload[src_pos + 2] & 0xFF)
        body_start = src_pos + 1 + 2 + (6 * coefficient)
        body = payload[body_start : body_start + length]

        image_data = [0] * _TILE_PIXELS
        tmpdata = 0
        lastnum = 0
        is_last_cyc = False
        sub_syclen = 0
        m = 0

        while m < len(body):
            if not is_last_cyc and lastnum < 8:
                tmpdata = (tmpdata & 0xFFFF) | ((body[m] & 0xFF) << (8 - lastnum))
                lastnum += 8
                m += 1

            sub_pixlen = ((tmpdata & 0xFC00) >> 10) + 1
            tmpdata = (tmpdata << 6) & 0xFFFF
            lastnum -= 6

            if sub_pixlen < 64:
                change_flag = 0
            else:
                change_flag = (tmpdata & 0x8000) >> 15
                tmpdata = (tmpdata << 1) & 0xFFFF
                lastnum -= 1

            for j in range(sub_pixlen):
                if sub_syclen + j >= _TILE_PIXELS:
                    break
                image_data[sub_syclen + j] = bufColor3
            sub_syclen += sub_pixlen

            if change_flag == 0:
                bufColor3 = bufColor2 if bufColor3 == bufColor1 else bufColor1

            if is_last_cyc or m >= length:
                if is_last_cyc:
                    m += 1
                    is_last_cyc = False
                if tmpdata != 0 or (lastnum != 0 and sub_syclen < _TILE_PIXELS):
                    is_last_cyc = True
                    m -= 1

        if sub_syclen != _TILE_PIXELS:
            log.debug(
                "RLE type 1 mismatch: sub_syclen=%d vs %d",
                sub_syclen, _TILE_PIXELS,
            )

        syclen = 3 + (6 * coefficient) + length
        return image_data, syclen

    def _decode_rle_type23(
        self,
        payload: bytes,
        src_pos: int,
        palette: bytes,
        src_col_pos: int,
        coefficient: int,
        rle_type: int,
    ) -> tuple[list[int], int]:
        """3- or 4-colour palette RLE — byte stream of 6-bit run + 2-bit index."""
        index_type = 4 if rle_type == 3 else 3   # type 2 → 3 colours, type 3 → 4
        palette_bytes = index_type * 3
        if src_col_pos + palette_bytes > len(palette):
            raise DecoderError(
                f"type {rle_type} palette too short ({len(palette)} < {palette_bytes})"
            )
        temcolor = [
            ycbcr2rgb(
                palette[src_col_pos + 3 * k + 0],
                palette[src_col_pos + 3 * k + 1],
                palette[src_col_pos + 3 * k + 2],
            )
            for k in range(index_type)
        ]
        # Type 2/3 with 3 colours uses indices 0..2; index 3 maps back to
        # the last colour (Java initialises temcolor[4] only when type=3).
        # We pad to 4 entries for safe indexing either way.
        while len(temcolor) < 4:
            temcolor.append(temcolor[-1])

        length = ((payload[src_pos + 1] & 0xFF) << 8) + (payload[src_pos + 2] & 0xFF)
        body_start = src_pos + 1 + 2 + (palette_bytes * coefficient)
        body = payload[body_start : body_start + length]

        image_data = [0] * _TILE_PIXELS
        sub_syclen = 0
        for token in body:
            sub_pixlen = ((token & 0xFC) >> 2) + 1
            index = token & 0x03
            colour = temcolor[index]
            for j in range(sub_pixlen):
                if sub_syclen + j >= _TILE_PIXELS:
                    break
                image_data[sub_syclen + j] = colour
            sub_syclen += sub_pixlen

        if sub_syclen != _TILE_PIXELS:
            log.debug(
                "RLE type %d mismatch: sub_syclen=%d vs %d",
                rle_type, sub_syclen, _TILE_PIXELS,
            )

        syclen = 3 + (palette_bytes * coefficient) + length
        return image_data, syclen


# ----------------------------------------------------------------------
# Frame composition
# ----------------------------------------------------------------------


def compose_frame(
    blocks: Sequence[ImageBlock | None],
    image_width: int,
    image_height: int,
) -> "Image.Image":
    """Paste decoded tiles into a single PIL.Image at chassis resolution.

    Edge tiles (those marked `fill=True` by the decoder) get cropped to
    cut_width × cut_height before paste so the rightmost column and
    bottom row don't bleed past the active framebuffer area.
    """
    from PIL import Image

    canvas = Image.new("RGB", (image_width, image_height), color=(0, 0, 0))
    for block in blocks:
        if block is None or block.image is None:
            continue
        tile = block.image
        if block.fill and (block.cut_width != TILE_SIZE or block.cut_height != TILE_SIZE):
            tile = tile.crop((0, 0, block.cut_width, block.cut_height))
        canvas.paste(tile, (block.x, block.y))
    return canvas


def decode_payload(
    payload: bytes,
    image_width: int,
    image_height: int,
    decoder: NewRleDecoder | None = None,
) -> "Image.Image":
    """Convenience wrapper: decode + compose in one call.

    Pass an existing `NewRleDecoder` to preserve cross-frame state (for
    incremental updates that copy from the prior frame's tiles).
    """
    if decoder is None:
        decoder = NewRleDecoder(image_width, image_height)
    blocks = decoder.decode(payload, image_width, image_height)
    if not blocks:
        return blank_tile()
    return compose_frame(blocks, image_width, image_height)


__all__ = [
    "DecoderError",
    "NewRleDecoder",
    "compose_frame",
    "decode_payload",
]
