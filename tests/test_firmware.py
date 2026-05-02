"""Tests for `hmm_client.firmware` — bulk firmware via Redfish UpdateService.

Focus: the pure planning logic (strategies, batching, blockers,
audit). The `apply()` path against real Redfish is exercised in an
integration test (deferred — needs the chassis-internal SSH tunnel
landed in #7).
"""

from __future__ import annotations

import json

import pytest

from hmm_client import firmware


def _t(id_: str, ready: bool = True, version: str = "1.0.0") -> firmware.FirmwareTarget:
    return firmware.FirmwareTarget(
        kind="blade",
        id=id_,
        redfish_host=f"{id_}.test",
        component="BMC",
        current_version=version,
        update_uri="/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
        is_ready=ready,
    )


@pytest.fixture(autouse=True)
def _audit_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
    yield


@pytest.mark.unit
class TestPlanRolling:
    def test_default_rolling_batches_by_4(self):
        targets = [_t(f"slot{i}") for i in range(1, 11)]
        p = firmware.plan("http://x/img.bin", targets)
        assert p.strategy == "rolling"
        assert p.target_count == 10
        # 10 / 4 = 3 waves: [1-4], [5-8], [9-10]
        assert len(p.waves) == 3
        assert [len(w.targets) for w in p.waves] == [4, 4, 2]
        assert p.is_safe

    def test_custom_batch_size(self):
        targets = [_t(f"slot{i}") for i in range(1, 7)]
        p = firmware.plan("http://x/img.bin", targets, batch_size=2)
        assert len(p.waves) == 3
        assert all(len(w.targets) == 2 for w in p.waves)


@pytest.mark.unit
class TestPlanCanary:
    def test_canary_isolates_first_target(self):
        targets = [_t(f"slot{i}") for i in range(1, 6)]
        p = firmware.plan("http://x/img.bin", targets, strategy="canary", batch_size=2)
        # Wave 1 = canary (1 target), then rolling on the rest
        assert len(p.waves[0].targets) == 1
        assert p.waves[0].targets[0].id == "slot1"
        # Remaining 4 split into batch_size=2 → 2 more waves
        assert len(p.waves) == 3

    def test_canary_with_one_target_blocks(self):
        p = firmware.plan("http://x/img.bin", [_t("slot1")], strategy="canary")
        assert any("canary needs >=2" in b for b in p.blockers)
        assert not p.is_safe


@pytest.mark.unit
class TestPlanAllAtOnce:
    def test_one_wave(self):
        targets = [_t(f"slot{i}") for i in range(1, 6)]
        p = firmware.plan("http://x/img.bin", targets, strategy="all-at-once")
        assert len(p.waves) == 1
        assert len(p.waves[0].targets) == 5


@pytest.mark.unit
class TestPlanBlockers:
    def test_not_ready_targets_block(self):
        targets = [_t("slot1"), _t("slot2", ready=False), _t("slot3", ready=False)]
        p = firmware.plan("http://x/img.bin", targets)
        assert not p.is_safe
        assert any("not ready" in b for b in p.blockers)
        assert "slot2" in p.blockers[0] or "slot3" in p.blockers[0]

    def test_apply_refuses_blocked_plan(self):
        p = firmware.plan(
            "http://x/img.bin",
            [_t("slot1", ready=False)],
        )
        with pytest.raises(firmware.FirmwareError):
            firmware.apply(p, user="root", password="pw")


@pytest.mark.unit
class TestPlanValidation:
    def test_empty_image_uri_raises(self):
        with pytest.raises(firmware.FirmwareError):
            firmware.plan("", [_t("slot1")])

    def test_empty_targets_raises(self):
        with pytest.raises(firmware.FirmwareError):
            firmware.plan("http://x/img.bin", [])

    def test_unknown_strategy_raises(self):
        with pytest.raises(firmware.FirmwareError):
            firmware.plan("http://x/img.bin", [_t("slot1")], strategy="hyperspeed")  # type: ignore[arg-type]


@pytest.mark.unit
class TestPlanSerialisation:
    def test_to_dict_round_trip_via_json(self):
        targets = [_t("slot1"), _t("slot2")]
        p = firmware.plan("http://x/img.bin", targets)
        # Must be JSON-serialisable so an operator can save/share it
        encoded = json.dumps(p.to_dict())
        decoded = json.loads(encoded)
        assert decoded["image_uri"] == "http://x/img.bin"
        assert decoded["strategy"] == "rolling"
        assert decoded["waves"][0]["targets"][0]["id"] == "slot1"


@pytest.mark.unit
class TestPlanAuditing:
    def test_plan_writes_audit_record(self, tmp_path):
        targets = [_t("slot1"), _t("slot2")]
        firmware.plan("http://x/img.bin", targets)
        audit_path = tmp_path / "audit.log"
        assert audit_path.exists()
        rec = json.loads(audit_path.read_text().strip().splitlines()[0])
        assert rec["op"] == "firmware.plan"
        assert rec["result"] == "dry-run"
        assert rec["evidence"]["target_count"] == 2
        assert rec["evidence"]["wave_count"] == 1
