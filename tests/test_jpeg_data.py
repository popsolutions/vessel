"""Tests for `hmm_client.kvm_core.decoder.jpeg_data` synthetic JPEG header."""

from __future__ import annotations

import pytest

from hmm_client.kvm_core.decoder.jpeg_data import (
    HEAD_SYN_DQT_U_50,
    HEAD_SYN_DQT_V_50,
    HEAD_SYN_DQT_Y_50,
    HEAD_SYN_SOF_444,
    HEAD_SYN_SOI_APPO,
    TAIL,
    create_syn_head_data,
    get_dqt_tags,
    set_dqt_tags,
)


@pytest.mark.unit
class TestJpegMarkerLengths:
    """JPEG markers carry a self-declared length field. Each DQT marker
    must have total bytes = 2 (FF DB) + length_value. We had a bug where
    DQT_U_50 / DQT_V_50 were 70 bytes but the length value declared 67
    (= 69 total) — caused PIL to reject our reconstructed JPEGs."""

    def test_dqt_y_50_length_matches_declaration(self):
        declared = (HEAD_SYN_DQT_Y_50[2] << 8) | HEAD_SYN_DQT_Y_50[3]
        assert len(HEAD_SYN_DQT_Y_50) - 2 == declared
        assert declared == 67  # 2 (len bytes) + 1 (Pq:Tq) + 64 (quant)

    def test_dqt_u_50_length_matches_declaration(self):
        declared = (HEAD_SYN_DQT_U_50[2] << 8) | HEAD_SYN_DQT_U_50[3]
        assert len(HEAD_SYN_DQT_U_50) - 2 == declared
        assert declared == 67

    def test_dqt_v_50_length_matches_declaration(self):
        declared = (HEAD_SYN_DQT_V_50[2] << 8) | HEAD_SYN_DQT_V_50[3]
        assert len(HEAD_SYN_DQT_V_50) - 2 == declared
        assert declared == 67

    def test_sof_marker_length_matches_declaration(self):
        declared = (HEAD_SYN_SOF_444[2] << 8) | HEAD_SYN_SOF_444[3]
        assert len(HEAD_SYN_SOF_444) - 2 == declared

    def test_soi_appo_starts_with_soi_marker(self):
        assert HEAD_SYN_SOI_APPO[0] == 0xFF
        assert HEAD_SYN_SOI_APPO[1] == 0xD8  # SOI

    def test_tail_is_eoi_marker(self):
        assert TAIL == bytes([0xFF, 0xD9])  # EOI


@pytest.mark.unit
class TestSynHeadData:
    def test_create_syn_head_data_starts_with_soi(self):
        head = create_syn_head_data()
        assert head[0:2] == bytes([0xFF, 0xD8])

    def test_create_syn_head_data_total_length(self):
        # SOI_APPO (20) + 3 * DQT (each 69) + SOF (19) + SOS (452) = 698
        head = create_syn_head_data()
        assert len(head) == 698

    def test_dqt_tags_default_is_4(self):
        assert get_dqt_tags() == 4

    def test_set_dqt_tags_clamps_range(self):
        original = get_dqt_tags()
        try:
            set_dqt_tags(-5)
            assert get_dqt_tags() == 0
            set_dqt_tags(99)
            assert get_dqt_tags() == 9
            set_dqt_tags(4)
            assert get_dqt_tags() == 4
        finally:
            set_dqt_tags(original)

    def test_unported_quality_falls_back_to_50pct(self):
        """Quality 20/30/40/60-100 are stubs (None) — must not crash."""
        original = get_dqt_tags()
        try:
            set_dqt_tags(2)  # 30% — currently None stub
            head = create_syn_head_data()
            # Should fall back to quality 50% header
            assert len(head) == 698
        finally:
            set_dqt_tags(original)
