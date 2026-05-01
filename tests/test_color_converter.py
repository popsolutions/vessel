"""Tests for `hmm_client.kvm_core.decoder.color_converter`."""
from __future__ import annotations

import pytest

from hmm_client.kvm_core.decoder.color_converter import (
    bgr233_to_rgb888,
    rgb888_to_bgr233,
    ycbcr2rgb,
    ycbcr2rgb332,
)


@pytest.mark.unit
class TestBgr233Conversion:
    def test_zero_byte_maps_to_black(self):
        assert bgr233_to_rgb888(0x00) == (0, 0, 0)

    def test_all_bits_set_maps_to_white(self):
        assert bgr233_to_rgb888(0xFF) == (255, 255, 255)

    def test_red_only(self):
        # 0x07 = 0b00_000_111 → R=7, G=0, B=0
        r, g, b = bgr233_to_rgb888(0x07)
        assert r == 255
        assert g == 0
        assert b == 0

    def test_green_only(self):
        # 0x38 = 0b00_111_000 → G=7, R=0, B=0
        r, g, b = bgr233_to_rgb888(0x38)
        assert r == 0
        assert g == 255
        assert b == 0

    def test_blue_only(self):
        # 0xC0 = 0b11_000_000 → B=3 (max for 2 bits), G=0, R=0
        r, g, b = bgr233_to_rgb888(0xC0)
        assert r == 0
        assert g == 0
        assert b == 255


@pytest.mark.unit
class TestYcbcr2Rgb:
    def test_solid_white_studio_range(self):
        rgb = ycbcr2rgb(235, 128, 128)
        r = (rgb >> 16) & 0xFF
        g = (rgb >> 8) & 0xFF
        b = rgb & 0xFF
        assert r == 235
        assert g == 235
        assert b == 235

    def test_solid_black(self):
        rgb = ycbcr2rgb(0, 128, 128)
        assert rgb == 0

    def test_clipping_keeps_in_range(self):
        rgb = ycbcr2rgb(0, 255, 255)
        r = (rgb >> 16) & 0xFF
        g = (rgb >> 8) & 0xFF
        b = rgb & 0xFF
        for c in (r, g, b):
            assert 0 <= c <= 255


@pytest.mark.unit
class TestYcbcr2Rgb332:
    def test_returns_byte_value(self):
        result = ycbcr2rgb332(128, 128, 128)
        assert 0 <= result <= 255

    def test_studio_white_packs_to_high_bits(self):
        result = ycbcr2rgb332(235, 128, 128)
        assert result == 0xFF


@pytest.mark.unit
def test_rgb888_to_bgr233_byte_compatible():
    result = rgb888_to_bgr233(0xFF, 0xFF, 0xFF)
    assert 0 <= result <= 255
