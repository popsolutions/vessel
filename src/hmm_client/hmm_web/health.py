"""Chassis health: active alarms + recent SEL events.

Wraps ``selhandler.php`` — the dispatcher behind both the alarm panel
and the System Event Log on the HMM web GUI.

Captured shapes (verified 2026-05-02 against 192.168.1.30):

    POST /selhandler.php
    actiontype=alarm&page=1&perpage=10&chassisid=0&isstep=1&bladename=all
    -> <result><alarms>
         <sum>4</sum><critical>2</critical><major>2</major><minor>0</minor>
         <alarm>
           <level>3</level><leveldesp>Critical</leveldesp>
           <source>MM1</source><sensorname>Management Port</sensorname>
           <datetime>Wed Mar 11 06:42:18 2026</datetime>
           <eventdesp>...</eventdesp><eventcode>0X0743FF27</eventcode>
           <cleartag>0</cleartag>
         </alarm>
       </alarms></result>

    POST /selhandler.php
    actiontype=sel&page=1&perpage=10&chassisid=0&bladename=smm
    -> <result><sum>169</sum>
         <sel>
           <level>0</level><leveldesp>Normal</leveldesp>
           <datetime>...</datetime><source>MM</source>
           <sensorname>SMM1 ...</sensorname>
           <eventdesp>...</eventdesp><assert>Asserted</assert>
         </sel>
       </result>
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .client import HMMWebClient, HMMWebError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alarm:
    level: int
    leveldesp: str
    source: str
    sensorname: str
    datetime_str: str
    eventdesp: str
    eventcode: str
    cleartag: str

    @property
    def is_active(self) -> bool:
        return self.cleartag == "0"


@dataclass(frozen=True)
class AlarmsSummary:
    total: int
    critical: int
    major: int
    minor: int
    alarms: list[Alarm] = field(default_factory=list)


@dataclass(frozen=True)
class SelEvent:
    level: int
    leveldesp: str
    datetime_str: str
    source: str
    sensorname: str
    eventdesp: str
    asserted: bool


@dataclass(frozen=True)
class SelPage:
    total: int
    events: list[SelEvent]


def _txt(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text


def _int(el: ET.Element | None, default: int = 0) -> int:
    t = _txt(el).strip()
    return int(t) if t.lstrip("-").isdigit() else default


def _parse_alarms(xml_body: str) -> AlarmsSummary:
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise HMMWebError(f"alarms: unparseable XML: {exc}") from exc
    block = root.find("alarms")
    if block is None:
        return AlarmsSummary(total=0, critical=0, major=0, minor=0, alarms=[])
    alarms: list[Alarm] = []
    for a in block.findall("alarm"):
        alarms.append(
            Alarm(
                level=_int(a.find("level")),
                leveldesp=_txt(a.find("leveldesp")),
                source=_txt(a.find("source")),
                sensorname=_txt(a.find("sensorname")),
                datetime_str=_txt(a.find("datetime")),
                eventdesp=_txt(a.find("eventdesp")),
                eventcode=_txt(a.find("eventcode")),
                cleartag=_txt(a.find("cleartag")),
            )
        )
    return AlarmsSummary(
        total=_int(block.find("sum")),
        critical=_int(block.find("critical")),
        major=_int(block.find("major")),
        minor=_int(block.find("minor")),
        alarms=alarms,
    )


def _parse_sel(xml_body: str) -> SelPage:
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise HMMWebError(f"sel: unparseable XML: {exc}") from exc
    events: list[SelEvent] = []
    for s in root.findall("sel"):
        events.append(
            SelEvent(
                level=_int(s.find("level")),
                leveldesp=_txt(s.find("leveldesp")),
                datetime_str=_txt(s.find("datetime")),
                source=_txt(s.find("source")),
                sensorname=_txt(s.find("sensorname")),
                eventdesp=_txt(s.find("eventdesp")),
                asserted=_txt(s.find("assert")).strip().lower() == "asserted",
            )
        )
    return SelPage(total=_int(root.find("sum")), events=events)


class HealthModule:
    """Read-only chassis health view."""

    def __init__(self, client: HMMWebClient) -> None:
        self._c = client

    def list_alarms(self, *, page: int = 1, perpage: int = 50) -> AlarmsSummary:
        """Return active alarms across the chassis (bladename=all)."""
        r = self._c.post(
            "selhandler.php",
            actiontype="alarm",
            page=str(page),
            perpage=str(perpage),
            chassisid="0",
            isstep="1",
            bladename="all",
            referer_path="/alarm_config.html?chassisid=0",
        )
        return _parse_alarms(r.body)

    def list_sel(
        self,
        *,
        bladename: str = "smm",
        page: int = 1,
        perpage: int = 50,
    ) -> SelPage:
        """Return a page of System Event Log entries for one component."""
        r = self._c.post(
            "selhandler.php",
            actiontype="sel",
            page=str(page),
            perpage=str(perpage),
            chassisid="0",
            bladename=bladename,
            referer_path=f"/{bladename}.html?chassisid=0",
        )
        return _parse_sel(r.body)
