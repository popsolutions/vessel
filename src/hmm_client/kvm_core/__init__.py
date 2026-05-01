"""Faithful Python port of `vconsole.jar` (`com.kvm.*`).

Goal: byte-for-byte equivalent to the Java applet, validated against
captured Palemoon traffic. Replaces the ad-hoc `hmm_client.kvm.*`
modules.

Module layout mirrors the Java packages:

    base.py          ←  Base.java                (state, constants, init keys)
    aes.py           ←  AESHandler.java          (PBKDF2, AES-CBC)
    key_map.py       ←  Base.KEY_MAP + javaCodeToUSB  (HID mapping)
    pack.py          ←  PackData.java            (outgoing builders)
    unpack.py        ←  UnPackData.java          (incoming parsers)
    util.py          ←  KVMUtil.java             (frame splitter, OldRLE)
    blade_thread.py  ←  BladeThread.java         (per-blade state machine)
    draw_thread.py   ←  DrawThread.java          (rendering pipeline)
    decoder/         ←  com.kvm.decoder.*        (NewRLE + JPEG codec)
"""
