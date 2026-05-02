"""Tests for hmm_client.hmm_web.manifest."""

from __future__ import annotations

import pytest

from hmm_client.hmm_web import (
    ComponentVersion,
    FieldDiff,
    ManifestError,
    diff,
    parse_manifest,
)


@pytest.mark.unit
class TestParseManifest:
    def test_empty_yaml(self):
        assert parse_manifest("") == {}
        assert parse_manifest("---\n") == {}

    def test_single_component(self):
        m = parse_manifest('smm:\n  "Software Version": "(U54)7.63"\n')
        assert m == {"smm": {"Software Version": "(U54)7.63"}}

    def test_multiple_components(self):
        m = parse_manifest(
            'smm:\n'
            '  "Software Version": "(U54)7.63"\n'
            'Slot1:\n'
            '  "BIOS Version": "(U41)V502"\n'
        )
        assert "smm" in m and "Slot1" in m
        assert m["smm"]["Software Version"] == "(U54)7.63"
        assert m["Slot1"]["BIOS Version"] == "(U41)V502"

    def test_alias_resolves_to_target(self):
        m = parse_manifest(
            'smm:\n'
            '  "Software Version": "(U54)7.63"\n'
            'othersmm: smm\n'
        )
        assert m["smm"] == m["othersmm"] == {"Software Version": "(U54)7.63"}

    def test_alias_chain_resolves(self):
        m = parse_manifest(
            'a:\n'
            '  "F": "v"\n'
            'b: a\n'
            'c: b\n'
        )
        assert m["c"] == {"F": "v"}

    def test_alias_to_unknown_raises(self):
        with pytest.raises(ManifestError):
            parse_manifest('x: nope\n')

    def test_alias_cycle_raises(self):
        with pytest.raises(ManifestError):
            parse_manifest('a: b\nb: a\n')

    def test_top_level_must_be_mapping(self):
        with pytest.raises(ManifestError):
            parse_manifest("- just\n- a\n- list\n")

    def test_invalid_yaml_raises(self):
        with pytest.raises(ManifestError):
            parse_manifest("smm: {unclosed: ")


def _cv(comp: str, **fields: str) -> ComponentVersion:
    return ComponentVersion(component=comp, present=True, raw="", fields=dict(fields))


@pytest.mark.unit
class TestDiff:
    def test_ok_when_current_matches_target(self):
        inv = [_cv("smm", **{"Software Version": "(U54)7.63"})]
        m = {"smm": {"Software Version": "(U54)7.63"}}
        d = diff(inv, m)
        assert len(d.fields) == 1
        assert d.fields[0].status == "ok"
        assert d.all_ok
        assert d.needs_action == []

    def test_mismatch_flagged(self):
        inv = [_cv("smm", **{"Software Version": "(U54)7.62"})]
        m = {"smm": {"Software Version": "(U54)7.63"}}
        d = diff(inv, m)
        assert d.fields[0].status == "mismatch"
        assert d.fields[0].current == "(U54)7.62"
        assert d.fields[0].target == "(U54)7.63"
        assert not d.all_ok
        assert d.needs_action == [d.fields[0]]

    def test_missing_field_flagged(self):
        inv = [_cv("smm", **{"Software Version": "(U54)7.63"})]
        m = {"smm": {"CPLD Version": "(U1082)108"}}
        d = diff(inv, m)
        assert d.fields[0].status == "missing"
        assert d.fields[0].current == ""

    def test_unknown_component(self):
        inv = [_cv("smm", **{"Software Version": "(U54)7.63"})]
        m = {"Slot99": {"BIOS Version": "anything"}}
        d = diff(inv, m)
        assert d.fields[0].status == "unknown"
        assert d.fields[0].component == "Slot99"

    def test_absent_component(self):
        inv = [ComponentVersion(component="Slot7", present=False)]
        m = {"Slot7": {"BIOS Version": "(U41)V502"}}
        d = diff(inv, m)
        assert d.fields[0].status == "unknown"

    def test_strip_whitespace_when_comparing(self):
        inv = [_cv("smm", **{"Software Version": "  (U54)7.63  "})]
        m = {"smm": {"Software Version": "(U54)7.63"}}
        assert diff(inv, m).fields[0].status == "ok"

    def test_by_component_groups_diffs(self):
        inv = [
            _cv("smm", **{"Software Version": "(U54)7.63", "CPLD Version": "(U1082)108"}),
            _cv("Slot1", **{"BIOS Version": "(U41)V501"}),
        ]
        m = {
            "smm": {"Software Version": "(U54)7.63", "CPLD Version": "(U1082)108"},
            "Slot1": {"BIOS Version": "(U41)V502"},
        }
        d = diff(inv, m)
        grouped = d.by_component()
        assert set(grouped) == {"smm", "Slot1"}
        assert len(grouped["smm"]) == 2
        assert len(grouped["Slot1"]) == 1
        assert grouped["Slot1"][0].status == "mismatch"

    def test_field_diff_helpers(self):
        f = FieldDiff(component="smm", field="x", current="a", target="a", status="ok")
        assert f.is_ok and not f.needs_action
        f2 = FieldDiff(component="smm", field="x", current="a", target="b", status="mismatch")
        assert not f2.is_ok and f2.needs_action
