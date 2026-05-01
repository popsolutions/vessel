"""Server→client transport framing for the Huawei iKVM video stream.

Three layers, top-down:

1. **Frames** (`parse_kvm_stream`):
       [FE F6 lenH lenL] [CRC16 LE 2] [op 1] [payload N]
   Same wire format as `vmedia.kvm_stream`, except server-side responses do
   NOT echo the sessionID. `op == 0x02` carries IMAGE_DATA.

2. **Image chunks** (`reassemble_images`): inside an IMAGE_DATA payload:
       [00 00 chunk_no img_id] [data...]
   - chunk 0 is the *init chunk*. Its 18-byte payload is:
       [4 chunk header] [4 BE total_size] [2 BE width] [2 BE height]
       [1 flag=0xdc?] [4 reserved] [1 encoding=0x02?]
   - chunks 1..N carry pure data, 220 bytes each (last chunk is short).
   Reassembly concatenates chunks 1..N until `total_size` bytes are
   collected, indexed by `img_id` (a 1-byte rolling counter on the wire).

3. **Tile tokens** (`classify_tiles_jpeg_walk`): the reassembled image is a
   stream of `imageWidth/64 × imageHeight/64` tiles (130 tiles for 800×600).
   Each tile begins with a 1-byte type, top 3 bits = `zipType`, next 3 bits
   = `rZipType`. Mapping (from `decoder/ImageDecoder.decodeRLEorJPEG1`):

       zipType 0,1   RLE encoding (rZipType 0..7 picks variant)
       zipType 2,3   JPEG: [type 1] [length 2 BE] [JPEG-SOS body N]
                       (DQT/DHT/SOF come from JPEGData.createSynHeadData())
       zipType 4     skip — same as last tile
       zipType 5,6,7 copy from neighbouring tile (left / above / above-left)

K1 only *classifies* tiles and reports stats — actual decoding is K2.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

# --- layer 1: KVM frames -----------------------------------------------------

PACKHEAD1 = 0xFE
PACKHEAD2 = 0xF6
KVM_OP_IMAGE_DATA = 0x02
KVM_OP_KEY_STATE = 0x04
KVM_OP_HEART_BEAT = 0x09


@dataclass(frozen=True)
class KvmFrame:
    offset: int
    body_len: int
    op: int
    crc_wire: int  # CRC as it appears on wire (little-endian decode)
    payload: bytes  # body[3:]


def parse_kvm_stream(data: bytes) -> list[KvmFrame]:
    """Walk a server→client byte stream and yield well-formed KVM frames.

    Stops on truncation or magic mismatch (no resync — caller can slice).
    """
    out: list[KvmFrame] = []
    pos = 0
    while pos + 4 <= len(data):
        if data[pos] != PACKHEAD1 or data[pos + 1] != PACKHEAD2:
            break
        body_len = (data[pos + 2] << 8) | data[pos + 3]
        if pos + 4 + body_len > len(data):
            break
        body = data[pos + 4 : pos + 4 + body_len]
        if body_len < 3:
            break
        crc_wire = struct.unpack("<H", body[:2])[0]
        op = body[2]
        out.append(
            KvmFrame(
                offset=pos,
                body_len=body_len,
                op=op,
                crc_wire=crc_wire,
                payload=body[3:],
            )
        )
        pos += 4 + body_len
    return out


# --- layer 2: image reassembly ------------------------------------------------


@dataclass
class Image:
    img_id: int  # 1-byte rolling counter
    total_size: int  # bytes of encoded image data
    width: int
    height: int
    flags_tail: bytes  # leftover bytes of the init chunk header (debug)
    data: bytearray = field(default_factory=bytearray)

    @property
    def complete(self) -> bool:
        return len(self.data) >= self.total_size


def reassemble_images(frames: list[KvmFrame]) -> list[Image]:
    """Group IMAGE_DATA frames by image-id; return one `Image` per id seen.

    Header layout in the IMAGE_DATA payload (matches KvmClient.frames;
    see KVMUtil.setVar for the original Java source of truth):

        p[1..2] BE   chunk position (0 = first chunk)
        p[3]         frame number (img_id)
        p[4..7] BE   total compressed size
        p[8]         top bit = diff flag, low 7 bits = width hi
        p[9]         width lo
        p[10..11] BE height
    """
    images: dict[int, Image] = {}
    chunks_by_id: dict[int, dict[int, bytes]] = {}
    order: list[int] = []
    for fr in frames:
        if fr.op != KVM_OP_IMAGE_DATA:
            continue
        p = fr.payload
        if len(p) < 4:
            continue
        chunk_pos = (p[1] << 8) | p[2]
        img_id = p[3]
        if chunk_pos == 0:
            if len(p) < 18:
                continue
            total = int.from_bytes(p[4:8], "big")
            w = ((p[8] & 0x7F) << 8) | p[9]
            h = int.from_bytes(p[10:12], "big")
            images[img_id] = Image(
                img_id=img_id,
                total_size=total,
                width=w,
                height=h,
                flags_tail=bytes(p[12:18]),
            )
            chunks_by_id[img_id] = {}
            order.append(img_id)
        else:
            img = images.get(img_id)
            if img is None:
                continue
            # Store by chunk_pos and assemble in order at the end —
            # see KvmClient.frames for why arrival-order assembly
            # rotates each chassis row by half its width.
            chunks_by_id[img_id][chunk_pos] = bytes(p[4:])
    for img_id in order:
        img = images[img_id]
        for cp in sorted(chunks_by_id[img_id]):
            img.data.extend(chunks_by_id[img_id][cp])
    return [images[i] for i in order]


# --- layer 3: tile classification ---------------------------------------------


@dataclass(frozen=True)
class TileToken:
    index: int  # tile index in reading order (0..N-1)
    offset: int  # byte offset within the encoded image data
    zip_type: int  # 0..7 (top 3 bits of first byte)
    r_zip_type: int  # 0..7 (next 3 bits)
    length: int  # bytes consumed from the stream by this tile
    body: bytes  # raw bytes for this tile (including the type byte)


JPEG_ZIP_TYPES = {2, 3}
COPY_ZIP_TYPES = {4, 5, 6, 7}  # 1-byte tokens (no payload)


def classify_tiles_jpeg_walk(image: Image) -> Iterator[TileToken]:
    """Walk only the JPEG and copy-tile tokens.

    Bails out at the first RLE token because RLE consumes a variable-length
    pixColors palette that K1 doesn't decode. Useful for classifying
    mostly-JPEG screens and for measuring how much of a frame is RLE vs
    JPEG.
    """
    data = bytes(image.data)
    pos = 0
    idx = 0
    while pos < len(data):
        b = data[pos]
        zt = (b >> 5) & 0x07
        rt = (b >> 2) & 0x07
        if zt in JPEG_ZIP_TYPES:
            if pos + 3 > len(data):
                return
            length = (data[pos + 1] << 8) | data[pos + 2]
            tot = 3 + length
            if pos + tot > len(data):
                return
            yield TileToken(
                index=idx,
                offset=pos,
                zip_type=zt,
                r_zip_type=rt,
                length=tot,
                body=data[pos : pos + tot],
            )
            pos += tot
            idx += 1
        elif zt in COPY_ZIP_TYPES:
            yield TileToken(
                index=idx,
                offset=pos,
                zip_type=zt,
                r_zip_type=rt,
                length=1,
                body=data[pos : pos + 1],
            )
            pos += 1
            idx += 1
        else:
            return


def summarise(image: Image) -> dict[str, object]:
    """Cheap stats over one image: tile counts by type, JPEG vs unknown bytes."""
    expected_tiles = (image.width // 64) * (image.height // 64)
    counts: dict[int, int] = {}
    walked_bytes = 0
    n_tokens = 0
    for tok in classify_tiles_jpeg_walk(image):
        counts[tok.zip_type] = counts.get(tok.zip_type, 0) + 1
        walked_bytes += tok.length
        n_tokens += 1
    return {
        "img_id": image.img_id,
        "size": image.total_size,
        "wxh": f"{image.width}x{image.height}",
        "expected_tiles": expected_tiles,
        "walked_tokens": n_tokens,
        "tile_types": counts,
        "walked_bytes": walked_bytes,
        "unwalked_bytes": image.total_size - walked_bytes,
        "complete": image.complete,
    }
