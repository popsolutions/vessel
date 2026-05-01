"""Huawei iKVM legacy "OldRLE" codec — port of `ImageDecoder.decodeOldRLE`.

This is the codec the chassis uses when the new fpeg/JPEG path is NOT
negotiated (`fpegAlg=false` in `connectBlade`). Output is a flat
`width × height` byte buffer in BGR233 format (1 byte/pixel):

    bit layout:  BBGGGRRR
                 76543210

The compressed stream is a state machine with two phases (`flagRem`):

* **flagRem = false** (initial):
  - byte[i+0] is the BGR233 colour
  - byte[i+1] high-nibble (bits 7..4) is the run length (1..15)
  - if run length == 0, the low-nibble of byte[i+1] selects an extended
    run-length encoding (6, 10, 18, or 22 bits)
  - if run length != 0, the low-nibble of byte[i+1] becomes the *high
    nibble* of the next colour, and we transition into flagRem = true

* **flagRem = true** (continuation):
  - byte[i+0] supplies the LOW nibble of the colour, OR'd with the
    high nibble carried over from the previous iteration
  - byte[i+1] high-nibble is the run length (1..15)
  - same extended-run-length variants apply when high-nibble == 0

The state-carry trick lets the encoder store one extra colour-nibble
per iteration without spending a whole byte on it.

The Java implementation mutates its input `bytes[]` in-place to shuttle
nibbles between iterations. We use a `bytearray` copy to mirror that
behaviour faithfully — the algorithm depends on the mutation.

Decoded output passes through `bgr233_to_rgb888()` for display.
"""
from __future__ import annotations


def decode_old_rle(data: bytes, width: int, height: int) -> bytes:
    """Decode an OldRLE stream into a width*height BGR233 framebuffer.

    Returns the raw 8 bpp framebuffer. Caller renders it via
    `bgr233_to_rgb888()` or feeds it into a PIL image.
    """
    pix_number = width * height
    image = bytearray(pix_number)
    buf = bytearray(data)  # mutable working copy (Java mutates input)
    i = 1                  # Java starts at i=1; byte 0 is unused / flag
    count = 0
    flag_rem = False

    while i < len(buf):
        if flag_rem:
            if i + 1 >= len(buf):
                return bytes(image)
            buf_color = (buf[i] | ((buf[i + 1] >> 4) & 0x0F)) & 0xFF
            tem = buf[i + 1] & 0x0F
            if tem != 0:
                run = tem
                if run + count > pix_number:
                    run = pix_number - count
                end = count + run
                for j in range(count, end):
                    image[j] = buf_color
                count = end
                if count == pix_number:
                    return bytes(image)
                tem_length = 2
                if len(buf) == i + 3:
                    tem_length = 3
                flag_rem = False
            else:
                if i + 2 >= len(buf):
                    return bytes(image)
                sel = buf[i + 2] & 0xC0
                if sel == 0x00:
                    extend = buf[i + 2] & 0x3F
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 3
                    if len(buf) == i + 4:
                        tem_length = 4
                    flag_rem = False
                elif sel == 0x40:
                    if i + 3 >= len(buf):
                        return bytes(image)
                    extend = ((buf[i + 2] << 4) & 0x3F0) + ((buf[i + 3] >> 4) & 0x0F)
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 3
                    if len(buf) == i + 4:
                        tem_length = 4
                    else:
                        buf[i + 3] = (buf[i + 3] << 4) & 0xFF
                        flag_rem = True
                elif sel == 0x80:
                    if i + 4 >= len(buf):
                        return bytes(image)
                    extend = (((buf[i + 2] << 12) & 0x3F000)
                              + ((buf[i + 3] << 4) & 0xFF0)
                              + ((buf[i + 4] >> 4) & 0x0F))
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 4
                    if len(buf) == i + 5:
                        tem_length = 5
                    else:
                        buf[i + 4] = (buf[i + 4] << 4) & 0xFF
                        flag_rem = True
                else:  # 0xC0
                    if i + 4 >= len(buf):
                        return bytes(image)
                    extend = (((buf[i + 2] << 16) & 0x3F0000)
                              + ((buf[i + 3] << 8) & 0xFF00)
                              + (buf[i + 4] & 0xFF))
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 5
                    if len(buf) == i + 6:
                        tem_length = 6
                    flag_rem = False
        else:
            buf_color = buf[i]
            if i + 1 >= len(buf):
                return bytes(image)
            tem = (buf[i + 1] >> 4) & 0x0F
            if tem != 0:
                run = tem
                if run + count > pix_number:
                    run = pix_number - count
                end = count + run
                for j in range(count, end):
                    image[j] = buf_color
                count = end
                if count == pix_number:
                    return bytes(image)
                tem_length = 1
                if len(buf) == i + 2:
                    tem_length = 2
                else:
                    buf[i + 1] = (buf[i + 1] << 4) & 0xFF
                    flag_rem = True
            else:
                sel = buf[i + 1] & 0x0C
                if sel == 0x00:
                    if i + 2 >= len(buf):
                        return bytes(image)
                    extend = ((buf[i + 1] << 4) & 0x30) + ((buf[i + 2] >> 4) & 0x0F)
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 2
                    if len(buf) == i + 3:
                        tem_length = 3
                    else:
                        buf[i + 2] = (buf[i + 2] << 4) & 0xFF
                        flag_rem = True
                elif sel == 0x04:
                    if i + 2 >= len(buf):
                        return bytes(image)
                    extend = ((buf[i + 1] << 8) & 0x300) + (buf[i + 2] & 0xFF)
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 3
                    if len(buf) == i + 4:
                        tem_length = 4
                    flag_rem = False
                elif sel == 0x08:
                    if i + 3 >= len(buf):
                        return bytes(image)
                    extend = (((buf[i + 1] << 16) & 0x30000)
                              + ((buf[i + 2] << 8) & 0xFF00)
                              + (buf[i + 3] & 0xFF))
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 4
                    if len(buf) == i + 5:
                        tem_length = 5
                    flag_rem = False
                else:  # 0x0C
                    if i + 4 >= len(buf):
                        return bytes(image)
                    extend = (((buf[i + 1] << 20) & 0x300000)
                              + ((buf[i + 2] << 12) & 0xFF000)
                              + ((buf[i + 3] << 4) & 0xFF0)
                              + ((buf[i + 4] >> 4) & 0x0F))
                    size = min(count + extend, pix_number)
                    for j in range(count, size):
                        image[j] = buf_color
                    count = size
                    if count == pix_number:
                        return bytes(image)
                    tem_length = 4
                    if len(buf) == i + 5:
                        tem_length = 5
                    else:
                        buf[i + 4] = (buf[i + 4] << 4) & 0xFF
                        flag_rem = True
        i += tem_length

    return bytes(image)


_R_LUT = bytes((((b & 0x07) << 5) | ((b & 0x07) << 2) | ((b & 0x07) >> 1)) & 0xFF
               for b in range(256))
_G_LUT = bytes((((b & 0x38) << 2) | ((b & 0x38) >> 1) | ((b & 0x38) >> 4)) & 0xFF
               for b in range(256))
_B_LUT = bytes(((b & 0xC0) | ((b & 0xC0) >> 2) | ((b & 0xC0) >> 4) | ((b & 0xC0) >> 6)) & 0xFF
               for b in range(256))


def bgr233_to_rgb888(framebuffer: bytes) -> bytes:
    """Expand a BGR233 framebuffer to packed RGB888 (3 bytes/pixel).

    Replicates top bits into low bits so peak channel values reach 0xFF
    instead of 0xE0 / 0xC0.

    NOTE: ~250 ms per call for a 640×480 buffer (pure-Python loop over
    every pixel). The hot live-streaming path uses `BGR233_PALETTE` +
    PIL's "P" mode instead — same colour mapping, but the per-pixel
    work happens in C. Keep this function for CLI dump/snapshot paths
    where throughput doesn't matter.
    """
    out = bytearray(len(framebuffer) * 3)
    for i, b in enumerate(framebuffer):
        out[3 * i + 0] = _R_LUT[b]
        out[3 * i + 1] = _G_LUT[b]
        out[3 * i + 2] = _B_LUT[b]
    return bytes(out)


# BGR233 → RGB888 LUT in the format `PIL.Image.putpalette()` expects:
# 256 entries × (R, G, B), concatenated, 768 bytes total. The live KVM
# pipeline uses this so the per-pixel work stays in libpng instead of a
# Python loop — ~50× speedup over `bgr233_to_rgb888` for 640×480.
BGR233_PALETTE: bytes = bytes(
    c for i in range(256) for c in (_R_LUT[i], _G_LUT[i], _B_LUT[i])
)
