"""Port of `com.kvm.decoder.ColorConverter`.

The Java applet uses two colour spaces:

- **YCbCr 4:4:4** for the NewRLE / JPEG-tile codec. The chassis sends
  raw Y, Cb, Cr samples (or JPEG-decoded coefficients) which need to
  be converted to the chassis's display-time BGR233 palette.

- **BGR233 (BBGGGRRR, 1 byte/pixel)** is the chassis's framebuffer
  pixel format. The browser-side display goes through PIL's "P"
  palette mode using `BGR233_PALETTE` from `kvm/codec_old.py`.

This module exposes only the conversions the live decoder needs.
The reverse direction (RGB→YCbCr) was used by Java's encoder side
and is omitted here.
"""

from __future__ import annotations

# Java's `Base.USB_KEY_CTRL = 224 = 0xE0` — used as the top-3-bits
# mask in `ycbcr2rgb332` for packing R and G into BGR233.
TOP3_MASK: int = 0xE0
TOP2_MASK: int = 0xC0


def ycbcr2rgb332(y: int, cb: int, cr: int) -> int:
    """Java's `ColorConverter.ycbcr2rgb332(int, int, int)`.

    Studio-range YCbCr → RGB ITU-R BT.601 (1.164 multiplier instead
    of 1.0):

        R = 1.164*(Y-16) + 1.596*(|Cr|-128)
        G = 1.164*(Y-16) - 0.813*(Cr-128) - 0.392*(Cb-128)
        B = 1.164*(Y-16) + 2.017*(Cb-128)

    Bizarrely Java applies `Math.abs(Cr2)` to the R channel only
    (line 151 of the original). For the typical Y/Cb/Cr 0..255
    inputs, `|Cr|` == `Cr` so this is a no-op — kept here for
    byte-fidelity with the Java decoder.

    Returns one BGR233 byte: `BBGGGRRR` (B in the top 2 bits).
    """
    y2 = y & 0xFF
    cb2 = cb & 0xFF
    cr2 = cr & 0xFF
    r = int(1.164 * (y2 - 16) + 1.596 * (abs(cr2) - 128))
    g = int(1.164 * (y2 - 16) - 0.813 * (cr2 - 128) - 0.392 * (cb2 - 128))
    b = int(1.164 * (y2 - 16) + 2.017 * (cb2 - 128))
    r = 0 if r < 0 else (255 if r > 255 else r)
    g = 0 if g < 0 else (255 if g > 255 else g)
    b = 0 if b < 0 else (255 if b > 255 else b)
    # Pack into BGR233 (BBGGGRRR layout, B in high bits):
    #   bgr233 = (B & 0xC0) | ((G & 0xE0) >> 2) | ((R & 0xE0) >> 5)
    return ((b & TOP2_MASK) | ((g & TOP3_MASK) >> 2) | ((r & TOP3_MASK) >> 5)) & 0xFF


def ycbcr2rgb(y: int, cb: int, cr: int) -> int:
    """Java's `ColorConverter.ycbcr2rgb(int, int, int)` — full RGB888.

    Standard JFIF YCbCr → RGB (offset 0 instead of 16, 1.0
    multiplier):

        R = Y + 1.402*(Cr-128)
        G = Y - 0.34414*(Cb-128) - 0.71414*(Cr-128)
        B = Y + 1.772*(Cb-128)

    Returns a packed `0xRRGGBB` int.
    """
    y2 = y & 0xFF
    cb2 = cb & 0xFF
    cr2 = cr & 0xFF
    r = int(y2 + 1.402 * (cr2 - 128))
    g = int(y2 - 0.34414 * (cb2 - 128) - 0.71414 * (cr2 - 128))
    b = int(y2 + 1.772 * (cb2 - 128))
    r = 0 if r < 0 else (255 if r > 255 else r)
    g = 0 if g < 0 else (255 if g > 255 else g)
    b = 0 if b < 0 else (255 if b > 255 else b)
    return (r << 16) | (g << 8) | b


def bgr233_to_rgb888(bgr233: int) -> tuple[int, int, int]:
    """Inverse of `rgb888_to_bgr233` — expand a single BGR233 byte.

    Layout `BBGGGRRR`:
      - B = bits 7..6 (2 bits)
      - G = bits 5..3 (3 bits)
      - R = bits 2..0 (3 bits)

    Each component is replicated into the high bits so 0b111 → 0xFF
    and 0b000 → 0x00 (standard bit-replication upscale used by VGA
    palette hardware). Distinct from `kvm.codec_old.bgr233_to_rgb888`
    which operates on a whole framebuffer; this single-pixel form is
    used by `image_creater.palette_image()` to build PIL palettes.
    """
    b2 = (bgr233 >> 6) & 0x03
    g3 = (bgr233 >> 3) & 0x07
    r3 = bgr233 & 0x07
    r8 = (r3 << 5) | (r3 << 2) | (r3 >> 1)
    g8 = (g3 << 5) | (g3 << 2) | (g3 >> 1)
    b8 = (b2 << 6) | (b2 << 4) | (b2 << 2) | b2
    return r8, g8, b8


def rgb888_to_bgr233(r: int, g: int, b: int) -> int:
    """Java's `ColorConverter.rgb888Tobgr233(byte, byte, byte)`.

    Bug-compatible with Java's original — Java masks `r` and `g`
    with `0xE0` (3 bits each) but stores them in the same 3-bit
    slot, then OR's `b & 0xC0` for the top 2 bits:

        bgr233 = (B & 0xC0) | (G & 0xE0) | (R & 0xE0)

    Note all three OR'd into the same byte without shifting — Java
    does literal `(b & 192) | (g & 224) | (r & 224)`. That's only
    correct if R and G have already been pre-shifted by the caller.
    Kept here for fidelity; live code paths use `ycbcr2rgb332`.
    """
    return ((b & TOP2_MASK) | (g & TOP3_MASK) | (r & TOP3_MASK)) & 0xFF
