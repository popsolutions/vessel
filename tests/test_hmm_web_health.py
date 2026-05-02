"""Tests for hmm_client.hmm_web.health."""

from __future__ import annotations

import pytest

from hmm_client.hmm_web import Alarm, HMMWebError
from hmm_client.hmm_web.health import _parse_alarms, _parse_sel

_ALARMS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result><retcode>0</retcode><alarms>
  <sum>3</sum><critical>1</critical><major>2</major><minor>0</minor>
  <alarm>
    <level>3</level><leveldesp>Critical</leveldesp>
    <source>MM1</source><sensorname>Management Port</sensorname>
    <datetime>Wed Mar 11 06:42:18 2026</datetime>
    <eventdesp>Transition to non-recoverable</eventdesp>
    <eventcode>0X0743FF27</eventcode>
    <cleartag>0</cleartag>
  </alarm>
  <alarm>
    <level>2</level><leveldesp>Major</leveldesp>
    <source>PSU</source><sensorname>PS3 Status</sensorname>
    <datetime>Wed Mar 11 06:43:41 2026</datetime>
    <eventdesp>Power supply failure</eventdesp>
    <eventcode>0X0801FFFF</eventcode>
    <cleartag>0</cleartag>
  </alarm>
  <alarm>
    <level>2</level><leveldesp>Major</leveldesp>
    <source>PSU</source><sensorname>PS4 Status</sensorname>
    <datetime>Wed Mar 11 06:43:41 2026</datetime>
    <eventdesp>Power supply failure</eventdesp>
    <eventcode>0X0801FFFF</eventcode>
    <cleartag>1</cleartag>
  </alarm>
</alarms></result>"""

_SEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result><retcode>0</retcode><sum>169</sum>
  <sel>
    <level>0</level><leveldesp>Normal</leveldesp>
    <datetime>Wed Mar 11 06:42:52 2026</datetime>
    <source>MM</source><sensorname>SMM1 Critical Alarm</sensorname>
    <eventdesp>Transition to idle</eventdesp>
    <assert>Asserted</assert>
  </sel>
  <sel>
    <level>3</level><leveldesp>Critical</leveldesp>
    <datetime>Wed Mar 11 06:42:18 2026</datetime>
    <source>MM</source><sensorname>SMM1 Management Port</sensorname>
    <eventdesp>Transition to non-recoverable</eventdesp>
    <assert>Deasserted</assert>
  </sel>
</result>"""


@pytest.mark.unit
class TestParseAlarms:
    def test_summary_counts(self):
        s = _parse_alarms(_ALARMS_XML)
        assert s.total == 3
        assert s.critical == 1
        assert s.major == 2
        assert s.minor == 0
        assert len(s.alarms) == 3

    def test_alarm_fields(self):
        s = _parse_alarms(_ALARMS_XML)
        a = s.alarms[0]
        assert a.level == 3
        assert a.leveldesp == "Critical"
        assert a.source == "MM1"
        assert a.sensorname == "Management Port"
        assert a.eventcode == "0X0743FF27"
        assert a.cleartag == "0"
        assert a.is_active

    def test_cleared_alarm_not_active(self):
        s = _parse_alarms(_ALARMS_XML)
        cleared = s.alarms[2]
        assert cleared.cleartag == "1"
        assert not cleared.is_active

    def test_no_alarms_block(self):
        s = _parse_alarms("<result><retcode>0</retcode></result>")
        assert s.total == 0
        assert s.alarms == []

    def test_unparseable_raises(self):
        with pytest.raises(HMMWebError):
            _parse_alarms("not valid <<")


@pytest.mark.unit
class TestParseSel:
    def test_total_and_count(self):
        p = _parse_sel(_SEL_XML)
        assert p.total == 169
        assert len(p.events) == 2

    def test_event_fields(self):
        p = _parse_sel(_SEL_XML)
        e = p.events[0]
        assert e.level == 0
        assert e.leveldesp == "Normal"
        assert e.source == "MM"
        assert e.sensorname == "SMM1 Critical Alarm"
        assert e.asserted is True

    def test_deasserted_flag(self):
        p = _parse_sel(_SEL_XML)
        assert p.events[1].asserted is False

    def test_unparseable_raises(self):
        with pytest.raises(HMMWebError):
            _parse_sel("not xml")


@pytest.mark.unit
class TestAlarmDataclass:
    def test_active_when_cleartag_zero(self):
        a = Alarm(
            level=3, leveldesp="Critical", source="MM1", sensorname="x",
            datetime_str="", eventdesp="", eventcode="", cleartag="0",
        )
        assert a.is_active

    def test_inactive_when_cleartag_one(self):
        a = Alarm(
            level=3, leveldesp="Critical", source="MM1", sensorname="x",
            datetime_str="", eventdesp="", eventcode="", cleartag="1",
        )
        assert not a.is_active
