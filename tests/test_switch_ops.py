"""Tests for `hmm_client.switch.ops` — VLAN CRUD with snapshot/watchdog."""

from __future__ import annotations

import pytest

from hmm_client.switch import ops as switch_ops
from hmm_client.switch.dispatcher import SshTarget


SAMPLE_VLAN_TABLE_BEFORE = """\
VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)    GE0/0/2(U)
10   common  TG:GE0/0/3(D)
"""

SAMPLE_VLAN_TABLE_AFTER_ADD_100 = """\
VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)    GE0/0/2(U)
10   common  TG:GE0/0/3(D)
100  common  UT:GE0/0/4(U)
"""

SAMPLE_RUNNING_CONFIG = "vlan batch 1 10\ninterface GE0/0/1\n port default vlan 1\n"


class MockDispatcher:
    """Test double for `Dispatcher` — records every command and replays
    canned responses by command-prefix lookup."""

    def __init__(self, responses: dict[str, str], host: str = "swi2.test"):
        self.target = SshTarget(host=host, username="ops")
        self._responses = responses
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: float = 30.0) -> str:
        self.commands.append(command)
        for prefix, body in self._responses.items():
            if command.startswith(prefix):
                return body
        return ""

    def update_response(self, prefix: str, body: str) -> None:
        self._responses[prefix] = body


@pytest.fixture(autouse=True)
def _audit_isolation(tmp_path, monkeypatch):
    """Each test writes its audit records to a tmp file, never the
    project-level audit.log."""
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "audit.log"))
    yield


def _baseline_responses() -> dict[str, str]:
    return {
        "display current-configuration": SAMPLE_RUNNING_CONFIG,
        "display vlan": SAMPLE_VLAN_TABLE_BEFORE,
        "display device": "device CX310 healthy",
        "system-view": "Enter system view, return user view with Ctrl+Z.",
        "save": "Saved to flash:.",
    }


@pytest.mark.unit
class TestTakeSnapshot:
    def test_captures_config_and_vlan_table(self):
        d = MockDispatcher(_baseline_responses())
        snap = switch_ops.take_snapshot(d)
        assert snap.host == "swi2.test"
        assert snap.running_config == SAMPLE_RUNNING_CONFIG
        assert set(snap.vlan_table) == {1, 10}


@pytest.mark.unit
class TestAddVlan:
    def test_adds_new_vlan(self):
        d = MockDispatcher(_baseline_responses())
        original_run = d.run

        def patched_run(cmd, *, timeout=30.0):
            out = original_run(cmd, timeout=timeout)
            if cmd == "vlan 100":
                d.update_response("display vlan", SAMPLE_VLAN_TABLE_AFTER_ADD_100)
            return out

        d.run = patched_run
        result = switch_ops.add_vlan(d, 100, name="prod-100")
        assert result.success is True
        assert result.rolled_back is False
        assert result.diff.added_vids == [100]

    def test_idempotent_when_already_exists(self):
        d = MockDispatcher(_baseline_responses())
        result = switch_ops.add_vlan(d, 10)  # 10 is already in baseline
        assert result.success is True
        assert result.evidence["already_existed"] is True
        # No system-view should have been issued
        assert "system-view" not in d.commands

    def test_rejects_invalid_vid(self):
        d = MockDispatcher(_baseline_responses())
        with pytest.raises(switch_ops.VlanOpError):
            switch_ops.add_vlan(d, 5000)
        with pytest.raises(switch_ops.VlanOpError):
            switch_ops.add_vlan(d, 0)

    def test_rolls_back_when_vlan_not_visible_after_apply(self):
        # Simulate a switch that swallows our `vlan 100` command but
        # never shows the VLAN — watchdog should fire rollback.
        d = MockDispatcher(_baseline_responses())
        result = switch_ops.add_vlan(d, 100)
        assert result.success is False
        assert result.rolled_back is True
        assert "not visible after apply" in result.evidence["reason"]

    def test_rolls_back_when_mgmt_plane_dies(self):
        responses = _baseline_responses()
        responses["display device"] = ""  # empty → mgmt plane dead
        d = MockDispatcher(responses)
        result = switch_ops.add_vlan(d, 100)
        assert result.success is False
        assert result.rolled_back is True
        assert "management plane" in result.evidence["reason"]


@pytest.mark.unit
class TestRemoveVlan:
    def test_removes_existing_vlan(self):
        d = MockDispatcher(_baseline_responses())
        original_run = d.run

        def patched_run(cmd, *, timeout=30.0):
            out = original_run(cmd, timeout=timeout)
            if cmd == "undo vlan 10":
                d.update_response(
                    "display vlan",
                    "VID  Type    Ports\n"
                    "-------------------------------------------------------------\n"
                    "1    common  UT:GE0/0/1(U)\n",
                )
            return out

        d.run = patched_run
        result = switch_ops.remove_vlan(d, 10)
        assert result.success is True
        assert result.rolled_back is False
        assert result.diff.removed_vids == [10]

    def test_idempotent_when_already_absent(self):
        d = MockDispatcher(_baseline_responses())
        result = switch_ops.remove_vlan(d, 999)
        assert result.success is True
        assert result.evidence["already_absent"] is True


@pytest.mark.unit
class TestPortConfig:
    def test_rejects_malformed_port_name(self):
        d = MockDispatcher(_baseline_responses())
        with pytest.raises(switch_ops.VlanOpError):
            switch_ops.set_access_port(d, "; rm -rf /", 1)
        with pytest.raises(switch_ops.VlanOpError):
            switch_ops.set_trunk_port(d, "GE 0/0/1 with space", allowed_vids=[1])

    def test_set_trunk_emits_correct_vrp_commands(self):
        d = MockDispatcher(_baseline_responses())
        switch_ops.set_trunk_port(d, "GE0/0/5", allowed_vids=[10, 20, 30], pvid=10)
        assert "interface GE0/0/5" in d.commands
        assert "port link-type trunk" in d.commands
        assert "port trunk allow-pass vlan 10 20 30" in d.commands
        assert "port trunk pvid vlan 10" in d.commands

    def test_set_access_emits_correct_vrp_commands(self):
        d = MockDispatcher(_baseline_responses())
        switch_ops.set_access_port(d, "GE0/0/7", 100)
        assert "interface GE0/0/7" in d.commands
        assert "port link-type access" in d.commands
        assert "port default vlan 100" in d.commands


@pytest.mark.unit
class TestNameSanitisation:
    def test_strips_newlines_from_vlan_name(self):
        d = MockDispatcher(_baseline_responses())
        original_run = d.run

        def patched_run(cmd, *, timeout=30.0):
            out = original_run(cmd, timeout=timeout)
            if cmd == "vlan 200":
                d.update_response(
                    "display vlan",
                    SAMPLE_VLAN_TABLE_BEFORE + "200  common  UT:GE0/0/1(U)\n",
                )
            return out

        d.run = patched_run
        switch_ops.add_vlan(d, 200, name="prod\nshell-injection")
        desc_cmds = [c for c in d.commands if c.startswith("description ")]
        assert desc_cmds
        for c in desc_cmds:
            assert "\n" not in c
            assert "shell-injection" in c
