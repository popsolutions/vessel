"""Smoke tests for the HMM-style top-nav pages and their endpoints.

Each public page must render with the shared nav partial. API
endpoints that talk to the chassis are mocked at the HMMWebClient
boundary so the suite stays offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("HMM_HOST", "127.0.0.99")
    monkeypatch.setenv("HMM_USER", "root")
    monkeypatch.setenv("HMM_PASSWORD", "x")
    monkeypatch.setenv("HMM_VERIFY_TLS", "false")
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.delenv("VESSEL_GUI_PASSWORD_HASH", raising=False)
    from hmm_client.gui.app import app
    return TestClient(app)


@pytest.mark.unit
class TestMenuPagesRender:
    @pytest.mark.parametrize(
        "url,expected_marker",
        [
            ("/chassis-settings", "Basic identification"),
            ("/chassis-management", "Chassis Management"),
            ("/stateless-computing", "Compute Profiles"),
            ("/psus-fans", "PSUs &amp; Fans"),
            ("/alarm-monitoring", "Alarm Monitoring"),
            ("/system-management", "System Management"),
        ],
    )
    def test_page_loads_with_nav(self, tmp_path, monkeypatch, url, expected_marker):
        client = _client(tmp_path, monkeypatch)
        r = client.get(url)
        assert r.status_code == 200
        assert "/static/hmm.css" in r.text
        assert 'class="hmm-header"' in r.text
        assert "Chassis Settings" in r.text
        assert "Chassis Management" in r.text
        assert expected_marker in r.text


@pytest.mark.unit
class TestNewApiEndpoints:
    def test_audit_endpoint_renders_empty_state(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        r = client.get("/api/system-management/audit")
        assert r.status_code == 200

    def test_audit_endpoint_renders_records(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        from hmm_client import audit as _audit
        _audit.log_op(op="snapshot.create", target_kind="hmm", target_id="x", result="success")
        r = client.get("/api/system-management/audit")
        assert r.status_code == 200
        assert "snapshot.create" in r.text

    def test_alarms_summary_returns_json(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        from hmm_client.hmm_web.health import AlarmsSummary
        with patch("hmm_client.gui.app.HMMWebClient") as cls, \
             patch("hmm_client.gui.app.HealthModule") as hm:
            inst = MagicMock()
            cls.from_settings.return_value = inst
            inst.__enter__.return_value = inst
            inst.__exit__.return_value = None
            hm.return_value.list_alarms.return_value = AlarmsSummary(
                total=4, critical=2, major=2, minor=0, alarms=[]
            )
            r = client.get("/api/health/alarms-summary")
        assert r.status_code == 200
        assert r.json() == {"total": 4, "critical": 2, "major": 2, "minor": 0}

    def test_alarms_summary_returns_502_on_failure(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        # No mocks → real HMMWebClient tries to reach 127.0.0.99 and fails.
        r = client.get("/api/health/alarms-summary")
        assert r.status_code == 502
        assert "error" in r.json()
