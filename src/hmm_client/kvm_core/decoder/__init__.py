"""Port of `com.kvm.decoder.*` — NewRLE + JPEG video codec.

Module layout mirrors Java's `com.kvm.decoder` package:

    color_converter.py  ←  ColorConverter.java     (YCbCr ↔ BGR233)
    image_block.py      ←  ImageBlock.java         (64×64 tile holder)
    image_creater.py    ←  ImageCreater.java       (palette image build,
                                                    JPEG decode wrapper)
    jpeg_data.py        ←  JPEGData.java + DQTZData + CreateDQTData
                                                   (synthetic JPEG header)
    image_decoder.py    ←  ImageDecoder.java       (the actual codec
                                                    state machine)
"""
