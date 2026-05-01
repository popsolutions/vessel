"""Port of `com.kvm.decoder.ImageBlock` — NewRLE tile container.

A NewRLE/JPEG image is decoded into a grid of 64×64 pixel tiles.
ImageBlock holds one such tile plus the metadata Java's renderer
needs to position and re-use it (fill flag, edge-tile cut dimensions,
the palette colour list for later "copy-and-recolour" tile types).

This is a straightforward port of the Java getters/setters into a
mutable dataclass — the codec state machine in `image_decoder.py`
keeps an `ImageBlock[blockcount]` array and patches fields in place
when later tile tokens reference earlier tiles by index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageBlock:
    """One 64×64 tile in a NewRLE-decoded frame.

    Field map (Java → Python):
      Image       image          → image (bytes — packed BGR233 or
                                   PIL.Image for JPEG tiles)
      boolean     fill           → fill   (True for edge tiles that
                                   need their cut dimensions)
      int         cutWidth       → cut_width  (0..64, edge crop)
      int         cutHeight      → cut_height (0..64, edge crop)
      int         x              → x   (top-left X in the framebuffer,
                                   set by the decoder as
                                   `(blocknum % blockXcount) * 64`)
      int         y              → y   (top-left Y, similarly)
      int         blockType      → block_type   (top 3 bits of token)
      int         blockRleType   → block_rle_type (next 3 bits, RLE
                                   variant 0..3 + copy variants 4..7)
      byte[]      pixColors      → pix_colors   (palette for RLE
                                   variants that re-use earlier tile's
                                   colour list)
    """

    image: Any | None = None
    fill: bool = False
    cut_width: int = 0
    cut_height: int = 0
    x: int = 0
    y: int = 0
    block_type: int = 0
    block_rle_type: int = 0
    pix_colors: bytes = field(default_factory=bytes)

    def clone_metadata_from(self, other: "ImageBlock") -> None:
        """Mirror Java's "copy-from-neighbour" tile case (zip_type 4/5/6).

        Used when a tile token says "I'm the same as the tile to my
        left / above / above-left". We copy the image reference plus
        the fill / cut / type metadata so the renderer can paint the
        same tile twice without re-decoding.
        """
        self.image = other.image
        self.fill = other.fill
        self.cut_width = other.cut_width
        self.cut_height = other.cut_height
        self.block_type = other.block_type
        self.block_rle_type = other.block_rle_type
