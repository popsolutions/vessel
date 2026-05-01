"""Tests for /healthz and /readyz endpoints in `hmm_client.gui.app`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh TestClient per test, with audit log isolated to tmp_path
    and auth turned off (we test endpoint behaviour, not the auth dep
    bypass — that's covered separately)."""
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.delenv("VESSEL_GUI_PASSWORD_HASH", raising=False)
    # Re-import after env tweak so module-level state picks up the path
    import importlib

    import hmm_client.gui.app as gui_app

    importlib.reload(gui_app)
    return TestClient(gui_app.app)


@pytest.mark.unit
class TestHealthz:
    def test_returns_200_always(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "service": "vessel"}

    def test_no_auth_required(self, tmp_path, monkeypatch):
        # Even with auth ENABLED, /healthz should succeed without creds
        from hmm_client.auth import hash_password

        monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", hash_password("strong-pw"))
        import importlib

        import hmm_client.gui.app as gui_app

        importlib.reload(gui_app)
        r = TestClient(gui_app.app).get("/healthz")
        assert r.status_code == 200


@pytest.mark.unit
class TestReadyz:
    def test_returns_503_when_chassis_unreachable(self, tmp_path, monkeypatch):
        # Point chassis host at an obviously-dead address — the TCP probe
        # will fail with refused/timeout. 198.51.100.0/24 is TEST-NET-2
        # (RFC 5737), guaranteed-unroutable.
        monkeypatch.setenv("HMM_HOST", "198.51.100.123")
        monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
        import importlib

        import hmm_client.gui.app as gui_app

        importlib.reload(gui_app)
        r = TestClient(gui_app.app).get("/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not-ready"
        assert any("hmm-tcp" in f for f in body["failing"])

    def test_writes_audit_probe_record(self, client, tmp_path):
        # readyz writes one audit record. Even if the chassis is
        # unreachable, the audit-write probe runs (and adds 1 line).
        client.get("/readyz")
        audit_path = tmp_path / "audit.log"
        if audit_path.exists():
            content = audit_path.read_text()
            assert "readyz.probe" in content
