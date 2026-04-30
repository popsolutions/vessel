"""Per-blade operations: power, boot device, inventory.

Power and boot device are controlled by the per-blade iBMC (not the HMM CLI -
the HMM's `smmset -d powerstate` exists but rejects all values on this
firmware). We reach the iBMC by SSH-jumping through the HMM:

    workstation --ssh--> HMM --ssh root@172.31.1.<128+slot>--> iBMC
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import paramiko

from .config import Settings
from .redfish import RedfishClient

# iBMC `ipmcset` value mappings (probed from `ipmcset -d <X> -v ?`).
POWER_VALUES = {"off": "0", "on": "1"}
RESET_VALUES = {"reset": "0", "cycle": "2", "nmi": "3"}
BOOT_DEVICES = {
    "none": "0",     # No override (use default order)
    "pxe": "1",
    "hdd": "2",
    "cd": "5",
    "floppy": "0xF",
}


def ibmc_ip_for_slot(slot: int) -> str:
    """Per the chassis-internal mapping (see docs/topology.md)."""
    if not 1 <= slot <= 32:
        raise ValueError(f"slot must be 1..32, got {slot}")
    return f"172.31.1.{128 + slot}"


class IBMCSession:
    """SSH jumphost: workstation -> HMM dispatcher -> iBMC dispatcher."""

    def __init__(self, settings: Settings, slot: int) -> None:
        self.s = settings
        self.slot = slot
        self._client: paramiko.SSHClient | None = None
        self._chan: paramiko.Channel | None = None

    def __enter__(self) -> "IBMCSession":
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.s.hmm_host,
            username=self.s.hmm_user,
            password=self.s.hmm_password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        self._chan = self._client.invoke_shell()
        time.sleep(1.0)
        self._drain()
        ip = ibmc_ip_for_slot(self.slot)
        self._send(f"ssh {ip} NoStricHostKeyChecking", wait=4.0)
        out = self._send(self.s.ibmc_password, wait=4.0)
        if "BMC" not in out and "iMana" not in out and "root@" not in out:
            raise RuntimeError(f"iBMC slot {self.slot} login failed: {out!r}")
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self._send("exit", wait=1.0)
        except Exception:
            pass
        if self._chan is not None:
            self._chan.close()
        if self._client is not None:
            self._client.close()

    def _drain(self) -> str:
        assert self._chan is not None
        buf = b""
        while self._chan.recv_ready():
            buf += self._chan.recv(32768)
        return buf.decode(errors="replace")

    def _send(self, line: str, wait: float = 1.5) -> str:
        assert self._chan is not None
        self._chan.send(line + "\r\n")
        time.sleep(wait)
        return self._drain()

    def run(self, cmd: str, wait: float = 2.0) -> str:
        return self._send(cmd, wait=wait)


def power(settings: Settings, slot: int, action: str) -> str:
    """Apply a power action to a blade. Returns the iBMC response text."""
    if action in POWER_VALUES:
        cmd = f"ipmcset -d powerstate -v {POWER_VALUES[action]}"
    elif action in RESET_VALUES:
        cmd = f"ipmcset -d frucontrol -v {RESET_VALUES[action]}"
    else:
        raise ValueError(
            f"unknown power action: {action!r} "
            f"(want one of {list(POWER_VALUES) + list(RESET_VALUES)})"
        )
    with IBMCSession(settings, slot) as ibmc:
        return ibmc.run(cmd)


def set_boot_device(settings: Settings, slot: int, device: str) -> str:
    if device not in BOOT_DEVICES:
        raise ValueError(
            f"unknown boot device: {device!r} (want one of {list(BOOT_DEVICES)})"
        )
    with IBMCSession(settings, slot) as ibmc:
        return ibmc.run(f"ipmcset -d bootdevice -v {BOOT_DEVICES[device]}")


def get_boot_device(settings: Settings, slot: int) -> str:
    with IBMCSession(settings, slot) as ibmc:
        return ibmc.run("ipmcget -d bootdevice").strip()


def _inventory(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One Redfish session, parallel GETs of every Chassis member."""
    with RedfishClient(
        settings.hmm_host, settings.hmm_user, settings.hmm_password,
        verify=settings.verify_tls,
    ) as rf:
        coll = rf.get("/redfish/v1/Chassis")
        refs = rf.members(coll)
        with ThreadPoolExecutor(max_workers=8) as ex:
            docs = list(ex.map(rf.get, refs))

    blades: list[dict[str, Any]] = []
    switches: list[dict[str, Any]] = []
    for doc in docs:
        id_ = doc.get("Id", "")
        state = (doc.get("Status") or {}).get("State", "?")
        if (m := re.match(r"Blade(\d+)$", id_)):
            slot = int(m.group(1))
            blades.append({
                "slot": slot, "model": doc.get("Model", "-"),
                "state": state, "ibmc_ip": ibmc_ip_for_slot(slot),
            })
        elif (m := re.match(r"Swi(\d+)$", id_)):
            slot = int(m.group(1))
            switches.append({
                "slot": slot, "model": doc.get("Model", "-"),
                "state": state, "mgmt_ip": f"172.31.1.{160 + slot}",
            })
    return (sorted(blades, key=lambda b: b["slot"]),
            sorted(switches, key=lambda s: s["slot"]))


def list_blades(settings: Settings) -> list[dict[str, Any]]:
    return _inventory(settings)[0]


def list_switches(settings: Settings) -> list[dict[str, Any]]:
    return _inventory(settings)[1]


def list_inventory(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Both blades and switches in a single Redfish session (preferred for the CLI)."""
    return _inventory(settings)


def list_sessions(settings: Settings) -> list[dict[str, Any]]:
    """List all active Redfish sessions on the HMM."""
    with RedfishClient(
        settings.hmm_host, settings.hmm_user, settings.hmm_password,
        verify=settings.verify_tls,
    ) as rf:
        coll = rf.get("/redfish/v1/SessionService/Sessions")
        my_url = rf._session_url  # noqa: SLF001
        out: list[dict[str, Any]] = []
        for ref in rf.members(coll):
            d = rf.get(ref)
            out.append({
                "url": ref,
                "id": d.get("Id"),
                "user": d.get("UserName"),
                "is_mine": ref == my_url,
            })
    return out


def cleanup_sessions(settings: Settings) -> int:
    """Delete every Redfish session on the HMM except the one we just opened.
    Returns the number of orphan sessions removed.
    """
    deleted = 0
    with RedfishClient(
        settings.hmm_host, settings.hmm_user, settings.hmm_password,
        verify=settings.verify_tls,
    ) as rf:
        coll = rf.get("/redfish/v1/SessionService/Sessions")
        my_url = rf._session_url  # noqa: SLF001
        for ref in rf.members(coll):
            if ref == my_url:
                continue
            try:
                rf._client.delete(ref)  # noqa: SLF001
                deleted += 1
            except httpx.HTTPError:
                pass
    return deleted
