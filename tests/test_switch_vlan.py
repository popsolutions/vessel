"""Tests for `hmm_client.switch.vlan` — VRP `display vlan` parser + diff."""
from __future__ import annotations

import pytest

from hmm_client.switch.vlan import (
    PortMembership,
    VlanEntry,
    diff_vlan_tables,
    parse_vlan_table,
)


SAMPLE_VRP = """\
The total number of vlans is : 3

U: Up;         D: Down;         TG: Tagged;         UT: Untagged;
MP: Vlan-mapping; ST: Vlan-stacking;

VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)    GE0/0/2(U)
10   common  TG:GE0/0/3(D)    GE0/0/4(U)
20   common  TG:XGE0/0/3(D)
"""


@pytest.mark.unit
class TestParseVlanTable:
    def test_parses_three_vlans(self):
        parsed = parse_vlan_table(SAMPLE_VRP)
        assert set(parsed) == {1, 10, 20}

    def test_sticky_mode_prefix_captures_continuation_ports(self):
        # Crucial bug we fixed during the session: VRP only prints the
        # mode prefix (UT:/TG:) before the FIRST port of a contiguous
        # mode group. The parser must inherit it for subsequent ports.
        parsed = parse_vlan_table(SAMPLE_VRP)
        assert parsed[1].untagged_ports == ["GE0/0/1", "GE0/0/2"]
        assert parsed[10].tagged_ports == ["GE0/0/3", "GE0/0/4"]

    def test_handles_xge_interface_naming(self):
        parsed = parse_vlan_table(SAMPLE_VRP)
        assert parsed[20].tagged_ports == ["XGE0/0/3"]

    def test_returns_empty_dict_on_no_header(self):
        assert parse_vlan_table("") == {}
        assert parse_vlan_table("not a vlan table at all") == {}

    def test_link_state_captured(self):
        parsed = parse_vlan_table(SAMPLE_VRP)
        port_1 = next(p for p in parsed[1].ports if p.iface == "GE0/0/1")
        assert port_1.link_state == "U"
        port_3 = next(p for p in parsed[10].ports if p.iface == "GE0/0/3")
        assert port_3.link_state == "D"


@pytest.mark.unit
class TestDiffVlanTables:
    def test_no_diff_when_identical(self):
        a = parse_vlan_table(SAMPLE_VRP)
        b = parse_vlan_table(SAMPLE_VRP)
        diff = diff_vlan_tables(a, b)
        assert diff.is_empty
        assert diff.added_vids == []
        assert diff.removed_vids == []

    def test_detects_added_vlan(self):
        baseline = parse_vlan_table(SAMPLE_VRP)
        modified = parse_vlan_table("""\
VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)    GE0/0/2(U)
10   common  TG:GE0/0/3(D)    GE0/0/4(U)
20   common  TG:XGE0/0/3(D)
100  common  UT:GE0/0/5(U)
""")
        diff = diff_vlan_tables(baseline, modified)
        assert diff.added_vids == [100]
        assert diff.removed_vids == []

    def test_detects_removed_vlan(self):
        baseline = parse_vlan_table(SAMPLE_VRP)
        modified = parse_vlan_table("""\
VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)    GE0/0/2(U)
10   common  TG:GE0/0/3(D)    GE0/0/4(U)
""")
        diff = diff_vlan_tables(baseline, modified)
        assert diff.removed_vids == [20]
        assert diff.added_vids == []

    def test_detects_membership_change(self):
        baseline = parse_vlan_table(SAMPLE_VRP)
        modified = parse_vlan_table("""\
VID  Type    Ports
-------------------------------------------------------------
1    common  UT:GE0/0/1(U)
10   common  TG:GE0/0/3(D)    GE0/0/4(U)
20   common  TG:XGE0/0/3(D)
""")
        diff = diff_vlan_tables(baseline, modified)
        assert diff.added_vids == []
        assert diff.removed_vids == []
        assert 1 in diff.membership_changes
        assert diff.membership_changes[1]["untagged_removed"] == ["GE0/0/2"]


@pytest.mark.unit
class TestVlanEntry:
    def test_separates_tagged_and_untagged(self):
        entry = VlanEntry(
            vid=42,
            ports=[
                PortMembership(iface="GE0/0/1", mode="UT", link_state="U"),
                PortMembership(iface="GE0/0/2", mode="TG", link_state="U"),
                PortMembership(iface="GE0/0/3", mode="TG", link_state="D"),
            ],
        )
        assert entry.untagged_ports == ["GE0/0/1"]
        assert entry.tagged_ports == ["GE0/0/2", "GE0/0/3"]
