"""Snapshot-then-change foundation: produce a backup of HMM + switches.

Writes ``snapshots/<UTC-stamp>/`` with:
  hmm/redfish/...           DMTF Redfish payloads (same as discover.py)
  hmm/smmget.txt            output of probed smmget data items
  hmm/diagnostics.txt       routedis + ifconfig
  switches/swi<N>.tar.gz    swiconfexport result, fetched via SFTP
  manifest.json             content hashes + checksum manifest

iBMC per-blade dumps are deferred (issue #7 - tunnel path blocked).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko
from rich.console import Console

from .config import Settings
from .discover import ROOT_ENTRIES, SUB_COLLECTIONS, _path_to_filename, _safe_get
from .redfish import RedfishClient

console = Console()

SMMGET_PROBES: list[tuple[str, list[str]]] = [
    ("version", []),
    ("gateway", []),
    ("timezone", []),
    ("presence", []),
]
SMMGET_PER_BLADE = ["presence", "health", "biosbootmode", "macaddress"]
SMMGET_PER_SWITCH = ["presence", "health", "version"]

VALID_SLOT_RE = re.compile(r"^(blade|swi)\d+$")


@dataclass(frozen=True)
class Item:
    path: str
    sha256: str
    bytes: int
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes, "kind": self.kind}


def _sha256(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _add(items: list[Item], root: Path, rel: str, kind: str) -> None:
    full = root / rel
    digest, size = _sha256(full)
    items.append(Item(path=rel, sha256=digest, bytes=size, kind=kind))


def _drain(chan) -> str:
    buf = b""
    while chan.recv_ready():
        buf += chan.recv(32768)
    return buf.decode(errors="replace")


def _shell_send(chan, line: str, wait: float = 1.5) -> str:
    chan.send(line + "\r\n")
    time.sleep(wait)
    return _drain(chan)


def _snap_redfish(s: Settings, out: Path, items: list[Item]) -> None:
    target = out / "hmm" / "redfish"
    target.mkdir(parents=True, exist_ok=True)
    seen: dict[str, dict[str, Any]] = {}

    with RedfishClient(s.hmm_host, s.hmm_user, s.hmm_password, verify=s.verify_tls) as rf:

        def fetch(path: str) -> dict[str, Any]:
            if path in seen:
                return seen[path]
            d = _safe_get(rf, path)
            seen[path] = d
            (target / _path_to_filename(path)).write_text(json.dumps(d, indent=2, sort_keys=True))
            return d

        root = fetch("/redfish/v1/")
        for key in ROOT_ENTRIES:
            entry = root.get(key)
            ref = entry.get("@odata.id") if isinstance(entry, dict) else None
            if not ref:
                continue
            coll = fetch(ref)
            for member in rf.members(coll):
                m = fetch(member)
                for sub in SUB_COLLECTIONS:
                    sub_entry = m.get(sub)
                    sref = sub_entry.get("@odata.id") if isinstance(sub_entry, dict) else None
                    if not sref:
                        continue
                    sc = fetch(sref)
                    for s_m in rf.members(sc):
                        fetch(s_m)

    for path in seen:
        rel = f"hmm/redfish/{_path_to_filename(path)}"
        _add(items, out, rel, kind="redfish")
    console.print(f"  redfish    [green]{len(seen)} resources[/]")


def _redfish_chassis_slots(s: Settings) -> tuple[list[str], list[str]]:
    blades, switches = [], []
    with RedfishClient(s.hmm_host, s.hmm_user, s.hmm_password, verify=s.verify_tls) as rf:
        coll = rf.get("/redfish/v1/Chassis")
        for ref in rf.members(coll):
            doc = rf.get(ref)
            id_ = doc.get("Id", "")
            state = (doc.get("Status") or {}).get("State")
            if state != "Enabled":
                continue
            m = re.match(r"Blade(\d+)$", id_)
            if m:
                blades.append(f"blade{int(m.group(1))}")
                continue
            m = re.match(r"Swi(\d+)$", id_)
            if m:
                switches.append(f"swi{int(m.group(1))}")
    return blades, switches


def _snap_hmm_cli(
    s: Settings,
    out: Path,
    items: list[Item],
    blades: list[str],
    switches: list[str],
) -> None:
    target = out / "hmm"
    target.mkdir(parents=True, exist_ok=True)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        s.hmm_host,
        username=s.hmm_user,
        password=s.hmm_password,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    chan = cli.invoke_shell()
    time.sleep(1.0)
    _drain(chan)

    smmget_lines: list[str] = []

    def probe(cmd: str) -> None:
        smmget_lines.append(f"$ {cmd}")
        smmget_lines.append(_shell_send(chan, cmd, wait=1.5).rstrip())
        smmget_lines.append("")

    for d, _ in SMMGET_PROBES:
        probe(f"smmget -d {d}")
    for slot in blades:
        if VALID_SLOT_RE.match(slot):
            for d in SMMGET_PER_BLADE:
                probe(f"smmget -l {slot} -d {d}")
    for slot in switches:
        if VALID_SLOT_RE.match(slot):
            for d in SMMGET_PER_SWITCH:
                probe(f"smmget -l {slot} -d {d}")

    (target / "smmget.txt").write_text("\n".join(smmget_lines) + "\n")
    _add(items, out, "hmm/smmget.txt", kind="smmget")

    diag_lines: list[str] = ["$ ifconfig", _shell_send(chan, "ifconfig", wait=1.5).rstrip()]

    _shell_send(chan, "cmdext -l", wait=1.0)
    _shell_send(chan, s.hmm_password, wait=2.0)
    for cmd in ("routedis", "arp", "df", "date"):
        diag_lines.append(f"\n$ {cmd}")
        diag_lines.append(_shell_send(chan, cmd, wait=1.5).rstrip())
    _shell_send(chan, "cmdext -u", wait=1.0)
    _shell_send(chan, s.hmm_password, wait=1.5)

    (target / "diagnostics.txt").write_text("\n".join(diag_lines) + "\n")
    _add(items, out, "hmm/diagnostics.txt", kind="diagnostics")

    chan.close()
    cli.close()
    console.print(f"  hmm-cli    [green]smmget x {len(smmget_lines) // 3 - 1}, diagnostics ok[/]")


def _snap_switches(s: Settings, out: Path, items: list[Item], switches: list[str]) -> None:
    target = out / "switches"
    target.mkdir(parents=True, exist_ok=True)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        s.hmm_host,
        username=s.hmm_user,
        password=s.hmm_password,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    chan = cli.invoke_shell()
    time.sleep(1.0)
    _drain(chan)

    _shell_send(chan, "cmdext -l", wait=1.0)
    _shell_send(chan, s.hmm_password, wait=2.0)

    for sw in switches:
        out_text = _shell_send(chan, f"swiconfexport {sw}", wait=6.0)
        if "Successed" not in out_text:
            console.print(f"  switches   [yellow]{sw}: skipped ({out_text.strip()[:80]})[/]")
            continue

        sftp_cli = paramiko.SSHClient()
        sftp_cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sftp_cli.connect(
            s.hmm_host,
            username=s.hmm_user,
            password=s.hmm_password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = sftp_cli.open_sftp()
        try:
            src = f"/tmp/exchange/{sw}/{sw}.tar.gz"
            dst = target / f"{sw}.tar.gz"
            sftp.get(src, str(dst))
            _add(items, out, f"switches/{sw}.tar.gz", kind="switch")
            console.print(f"  switches   [green]{sw}.tar.gz ({dst.stat().st_size} bytes)[/]")
        finally:
            sftp.close()
            sftp_cli.close()

    _shell_send(chan, "cmdext -u", wait=1.0)
    _shell_send(chan, s.hmm_password, wait=1.5)
    chan.close()
    cli.close()


def _hmm_software_version(s: Settings) -> str:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        s.hmm_host,
        username=s.hmm_user,
        password=s.hmm_password,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    chan = cli.invoke_shell()
    time.sleep(1.0)
    _drain(chan)
    out = _shell_send(chan, "smmget -d version", wait=1.5)
    chan.close()
    cli.close()
    m = re.search(r"Software Version :\(?[^)]*\)?(\S+)", out)
    return m.group(1) if m else "unknown"


def run_snapshot() -> Path:
    s = Settings.load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = s.snapshots_dir / stamp
    out.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold]HMM snapshot[/] {s.hmm_host} -> {out}")

    items: list[Item] = []
    blades, switches = _redfish_chassis_slots(s)
    console.print(f"  inventory  {len(blades)} active blades, {len(switches)} active switches")

    _snap_redfish(s, out, items)
    _snap_hmm_cli(s, out, items, blades, switches)
    _snap_switches(s, out, items, switches)

    manifest = {
        "timestamp": stamp,
        "host": s.hmm_host,
        "hmm_software_version": _hmm_software_version(s),
        "blades_present": blades,
        "switches_present": switches,
        "items": [it.to_dict() for it in items],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    console.print(f"\n[bold green]Snapshot complete[/] -> {out / 'manifest.json'}")
    console.print(f"  {len(items)} files, {sum(it.bytes for it in items):,} bytes total")
    return out


if __name__ == "__main__":
    run_snapshot()
