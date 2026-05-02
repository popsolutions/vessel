"""Tests for the /firmware-web routes in the FastAPI GUI."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hmm_client.gui.app import _parse_bladelist_form
from hmm_client.hmm_web import UpgradeTarget


@pytest.mark.unit
class TestParseBladelistForm:
    def test_bothsmm_alias(self):
        out = _parse_bladelist_form("bothsmm")
        assert len(out) == 1
        assert out[0].bladename == "bothsmm"
        assert out[0].fruid is None

    def test_two_switches(self):
        out = _parse_bladelist_form("Swi2:fru0;Swi3:fru0;")
        assert len(out) == 2
        assert out[0].bladename == "Swi2"
        assert out[0].fruid == "0"
        assert out[1].bladename == "Swi3"

    def test_blade_token(self):
        out = _parse_bladelist_form("Slot7:fru0;")
        assert out == [UpgradeTarget(bladename="Slot7", fruid="0")]

    def test_empty_returns_empty(self):
        assert _parse_bladelist_form("") == []
        assert _parse_bladelist_form("   ") == []


def _gui_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HMM_HOST", "127.0.0.99")
    monkeypatch.setenv("HMM_USER", "root")
    monkeypatch.setenv("HMM_PASSWORD", "x")
    monkeypatch.setenv("HMM_VERIFY_TLS", "false")
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.delenv("VESSEL_GUI_PASSWORD_HASH", raising=False)
    from hmm_client.gui.app import app

    return TestClient(app)


@pytest.mark.unit
class TestFirmwareWebRoutes:
    def test_page_renders(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        r = client.get("/firmware-web")
        assert r.status_code == 200
        assert "Firmware (web API)" in r.text
        assert "fw-bladelist" in r.text
        assert "snapshot-first" in r.text

    def test_apply_rejects_empty_bladelist(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        r = client.post("/api/firmware-web/apply", data={"bladelist": ""})
        assert r.status_code == 400
        assert "no valid targets" in r.text

    def test_apply_calls_snapshot_then_firmware(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        snap_dir = tmp_path / "snapshots" / "20260502T230000Z"
        snap_dir.mkdir(parents=True)
        with patch("hmm_client.gui.app.run_snapshot", return_value=snap_dir) as mock_snap, \
             patch("hmm_client.gui.app.HMMWebClient") as mock_cls:
            inst = MagicMock()
            mock_cls.from_settings.return_value = inst
            inst.__enter__.return_value = inst
            inst.__exit__.return_value = None
            r = client.post(
                "/api/firmware-web/apply",
                data={"bladelist": "bothsmm", "snapshot_first": "true"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["bladelist"] == "bothsmm"
        assert body["snapshot_id"] == "20260502T230000Z"
        mock_snap.assert_called_once()

    def test_apply_skips_snapshot_when_unchecked(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        with patch("hmm_client.gui.app.run_snapshot") as mock_snap, \
             patch("hmm_client.gui.app.HMMWebClient") as mock_cls:
            inst = MagicMock()
            mock_cls.from_settings.return_value = inst
            inst.__enter__.return_value = inst
            inst.__exit__.return_value = None
            r = client.post(
                "/api/firmware-web/apply",
                data={"bladelist": "bothsmm", "snapshot_first": "false"},
            )
        assert r.status_code == 200
        assert r.json()["snapshot_id"] is None
        mock_snap.assert_not_called()

    def test_audit_tail_renders_empty_state(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        r = client.get("/api/firmware-web/audit-tail")
        assert r.status_code == 200
        assert "No firmware-web actions logged yet" in r.text

    def test_audit_tail_filters_to_firmware_web_records(self, tmp_path, monkeypatch):
        client = _gui_client(tmp_path, monkeypatch)
        from hmm_client import audit as _audit
        _audit.log_op(op="snapshot.create", target_kind="hmm", target_id="x", result="success")
        _audit.log_op(
            op="firmware-web.upload",
            target_kind="hmm",
            target_id="x",
            result="success",
            evidence={"filename": "test.hpm", "bytes": 1024},
        )
        r = client.get("/api/firmware-web/audit-tail")
        assert r.status_code == 200
        assert "firmware-web.upload" in r.text
        assert "snapshot.create" not in r.text
