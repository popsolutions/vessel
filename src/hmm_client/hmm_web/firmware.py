"""Firmware upload + apply + status via the HMM web API.

Wraps ``smmupgradehandler.php`` — the same dispatcher that the HMM
GUI's "System Mgmt → Upgrade" panel drives. The captured flow is:

    1. multipart POST  (uploadfile=<.hpm bytes>)            -> upload
    2. POST actiontype=update&bladelist=<targets>           -> apply
    3. POST actiontype=get                                  -> poll
    4. POST actiontype=delete                               -> cleanup
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .client import HMMWebClient, HMMWebError, HMMWebResult

log = logging.getLogger(__name__)

UPGRADE_HANDLER = "smmupgradehandler.php"
UPLOAD_FIELD = "uploadfile"
DEFAULT_REFERER = "/system_manage_smm.html?chassisid=0"


@dataclass(frozen=True)
class UpgradeTarget:
    """One element of HMM's ``bladelist`` parameter."""

    bladename: str
    fruid: str | None = None

    def to_token(self) -> str:
        if self.fruid is None:
            return self.bladename
        return f"{self.bladename}:fru{self.fruid}"

    @classmethod
    def smm_pair(cls) -> "UpgradeTarget":
        return cls(bladename="bothsmm")

    @classmethod
    def switch(cls, slot: str, fruid: str = "0") -> "UpgradeTarget":
        return cls(bladename=slot, fruid=fruid)

    @classmethod
    def blade(cls, slot: int, fruid: str = "0") -> "UpgradeTarget":
        return cls(bladename=f"Slot{slot}", fruid=fruid)


def bladelist(targets: list[UpgradeTarget]) -> str:
    """Encode targets as HMM's semi-colon-terminated list."""
    if len(targets) == 1 and targets[0].fruid is None:
        return targets[0].to_token()
    return "".join(f"{t.to_token()};" for t in targets)


@dataclass(frozen=True)
class TargetProgress:
    name: str
    retcode: int
    desp: str
    progress: int
    progress_desp: str

    @property
    def is_terminal(self) -> bool:
        return self.progress >= 100 or self.retcode != 0


@dataclass(frozen=True)
class UpgradeStatus:
    targets: list[TargetProgress]
    raw: str

    @property
    def all_done(self) -> bool:
        return bool(self.targets) and all(t.is_terminal for t in self.targets)

    @property
    def any_failed(self) -> bool:
        return any(t.retcode != 0 for t in self.targets)


class FirmwareModule:
    """Web-API firmware verbs."""

    def __init__(self, client: HMMWebClient) -> None:
        self._c = client

    def upload(self, filename: str, image_bytes: bytes) -> HMMWebResult:
        if not image_bytes:
            raise HMMWebError("upload: empty image bytes")
        result = self._c.post_multipart(
            UPGRADE_HANDLER,
            files={UPLOAD_FIELD: (filename, image_bytes, "application/octet-stream")},
            referer_path=DEFAULT_REFERER,
        )
        if result.retcode not in (0, None):
            result.raise_for_retcode()
        log.info("hmm-web firmware uploaded: %s (%d bytes)", filename, len(image_bytes))
        return result

    def apply(self, targets: list[UpgradeTarget]) -> HMMWebResult:
        if not targets:
            raise HMMWebError("apply: no targets given")
        token = bladelist(targets)
        result = self._c.post(
            UPGRADE_HANDLER,
            actiontype="update",
            bladelist=token,
            referer_path=DEFAULT_REFERER,
        )
        result.raise_for_retcode()
        log.info("hmm-web firmware apply triggered: bladelist=%s", token)
        return result

    def status(self) -> UpgradeStatus:
        result = self._c.post(
            UPGRADE_HANDLER,
            actiontype="get",
            referer_path=DEFAULT_REFERER,
        )
        return _parse_status(result.body)

    def cancel(self) -> HMMWebResult:
        result = self._c.post(
            UPGRADE_HANDLER,
            actiontype="delete",
            referer_path=DEFAULT_REFERER,
        )
        result.raise_for_retcode()
        log.info("hmm-web firmware cancel/cleanup issued")
        return result


def _parse_status(xml_body: str) -> UpgradeStatus:
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise HMMWebError(f"status: unparseable XML: {exc}") from exc
    targets: list[TargetProgress] = []
    for blade in root.findall("blade"):
        name = blade.attrib.get("name", "")
        rc_el = blade.find("retcode")
        prog_el = blade.find("progress")
        rc = (
            int(rc_el.text)
            if rc_el is not None and rc_el.text and rc_el.text.lstrip("-").isdigit()
            else -1
        )
        progress = (
            int(prog_el.text)
            if prog_el is not None and prog_el.text and prog_el.text.isdigit()
            else 0
        )
        desp_el = blade.find("desp")
        pdesp_el = blade.find("progressdesp")
        desp = desp_el.text or "" if desp_el is not None else ""
        pdesp = pdesp_el.text or "" if pdesp_el is not None else ""
        targets.append(
            TargetProgress(
                name=name, retcode=rc, desp=desp, progress=progress, progress_desp=pdesp
            )
        )
    return UpgradeStatus(targets=targets, raw=xml_body)
