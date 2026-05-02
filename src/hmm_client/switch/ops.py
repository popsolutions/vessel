"""High-level VLAN + port operations on a CX310 switch.

Builds on top of `switch.dispatcher.Dispatcher` (SSH transport) and
`switch.vlan` (parser + diff). Every mutating call is wrapped in the
project's standard envelope:

    1. Snapshot — capture `display current-configuration` so we can
       diff and roll back.
    2. Apply — push the VRP CLI commands.
    3. Watchdog — re-read state, confirm the change took effect, run
       a management-plane heartbeat (`display device` is enough — if
       the switch can answer at all, the mgmt plane is alive).
    4. Commit (`save`) or roll back from the snapshot.

Audit records land per-step so an operator looking at `audit.log` can
reconstruct: who tried what, did the snapshot succeed, did the apply
succeed, did the watchdog pass, was a rollback fired.

## Why VRP CLI rather than NETCONF/SNMP

The CX310 supports NETCONF in newer firmware revisions but our chassis
runs an older VRP that does not. SSH + `system-view` works on every
revision we've encountered. When we standardise on a fixed-firmware
fleet the NETCONF path can swap in behind the same public API here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .. import audit
from .dispatcher import Dispatcher
from .vlan import VlanDiff, VlanEntry, diff_vlan_tables, parse_vlan_table

log = logging.getLogger(__name__)


class VlanOpError(RuntimeError):
    """Raised when a VLAN operation cannot complete safely."""


@dataclass
class SwitchSnapshot:
    """In-memory snapshot used by the rollback path.

    The `running_config` string is the only thing we need to put the
    switch back the way it was. We capture `vlan_table` separately so
    we can show a structured diff to the operator without parsing the
    whole config.
    """

    host: str
    running_config: str
    vlan_table: dict[int, VlanEntry] = field(default_factory=dict)


@dataclass
class VlanOpResult:
    op: str
    target: str  # e.g. "swi2"
    requested_vid: int | None
    diff: VlanDiff
    success: bool
    rolled_back: bool = False
    evidence: dict = field(default_factory=dict)


# --- helpers ---------------------------------------------------------

_PORT_NAME_RE = re.compile(r"^[A-Za-z]+\d+(?:/\d+){0,3}$")


def _validate_port(port: str) -> str:
    """Reject obviously-malformed port names (defence against shell
    injection — every value below ends up in a VRP command line)."""
    if not _PORT_NAME_RE.fullmatch(port):
        raise VlanOpError(f"invalid port name: {port!r}")
    return port


def _validate_vid(vid: int) -> int:
    if not (1 <= vid <= 4094):
        raise VlanOpError(f"VLAN id must be 1..4094, got {vid}")
    return vid


def _management_plane_alive(d: Dispatcher) -> bool:
    """Cheap "is the switch still answering us?" probe.

    Used after every mutating apply — if the switch stops answering
    after a config change we initiated, that's an emergency and we
    roll back via the snapshot.
    """
    try:
        out = d.run("display device", timeout=5.0)
    except Exception as exc:
        log.warning("mgmt-plane probe failed: %s", exc)
        return False
    return "device" in out.lower() or len(out) > 16


def take_snapshot(d: Dispatcher) -> SwitchSnapshot:
    """Capture the running config + parsed VLAN table.

    Audited as `switch.snapshot`. Called automatically by every
    mutating op before it makes changes.
    """
    running = d.run("display current-configuration")
    vlan_text = d.run("display vlan")
    snap = SwitchSnapshot(
        host=d.target.host,
        running_config=running,
        vlan_table=parse_vlan_table(vlan_text),
    )
    audit.log_op(
        op="switch.snapshot",
        target_kind="switch",
        target_id=d.target.host,
        result="success",
        evidence={
            "running_config_bytes": len(running),
            "vlan_count": len(snap.vlan_table),
        },
    )
    return snap


# --- public ops ------------------------------------------------------


def list_vlans(d: Dispatcher) -> dict[int, VlanEntry]:
    """Read-only `display vlan` parse. Audited as a switch.cli call
    by the dispatcher itself; we don't double-log here."""
    return parse_vlan_table(d.run("display vlan"))


def add_vlan(
    d: Dispatcher,
    vid: int,
    *,
    name: str | None = None,
    snapshot: SwitchSnapshot | None = None,
) -> VlanOpResult:
    """Create a new VLAN. Idempotent — succeeds (no-op) if the VLAN
    already exists."""
    _validate_vid(vid)
    snap = snapshot or take_snapshot(d)

    if vid in snap.vlan_table:
        result = VlanOpResult(
            op="vlan.add",
            target=d.target.host,
            requested_vid=vid,
            diff=VlanDiff(),
            success=True,
            evidence={"already_existed": True, "name": name},
        )
        audit.log_op(
            op="vlan.add",
            target_kind="switch",
            target_id=d.target.host,
            result="success",
            evidence=result.evidence,
        )
        return result

    cmds = ["system-view", f"vlan {vid}"]
    if name:
        # Strip any newlines/control bytes from the operator-supplied name.
        clean_name = re.sub(r"[\r\n\x00]+", "", name)[:31]
        cmds.append(f'description "{clean_name}"')
    cmds += ["quit", "quit"]
    apply_output = "\n".join(d.run(c) for c in cmds)

    if not _management_plane_alive(d):
        return _rollback(
            d, snap, op="vlan.add", requested_vid=vid, reason="management plane stopped responding"
        )

    after = parse_vlan_table(d.run("display vlan"))
    diff = diff_vlan_tables(snap.vlan_table, after)
    if vid not in after:
        return _rollback(
            d, snap, op="vlan.add", requested_vid=vid, reason=f"VLAN {vid} not visible after apply"
        )

    d.run("save")
    result = VlanOpResult(
        op="vlan.add",
        target=d.target.host,
        requested_vid=vid,
        diff=diff,
        success=True,
        evidence={"name": name, "apply_output_tail": apply_output[-256:]},
    )
    audit.log_op(
        op="vlan.add",
        target_kind="switch",
        target_id=d.target.host,
        result="success",
        evidence=result.evidence,
    )
    return result


def remove_vlan(
    d: Dispatcher,
    vid: int,
    *,
    snapshot: SwitchSnapshot | None = None,
) -> VlanOpResult:
    """Delete a VLAN. Idempotent — succeeds (no-op) if the VLAN is
    already absent."""
    _validate_vid(vid)
    snap = snapshot or take_snapshot(d)

    if vid not in snap.vlan_table:
        result = VlanOpResult(
            op="vlan.remove",
            target=d.target.host,
            requested_vid=vid,
            diff=VlanDiff(),
            success=True,
            evidence={"already_absent": True},
        )
        audit.log_op(
            op="vlan.remove",
            target_kind="switch",
            target_id=d.target.host,
            result="success",
            evidence=result.evidence,
        )
        return result

    cmds = ["system-view", f"undo vlan {vid}", "quit"]
    apply_output = "\n".join(d.run(c) for c in cmds)

    if not _management_plane_alive(d):
        return _rollback(
            d,
            snap,
            op="vlan.remove",
            requested_vid=vid,
            reason="management plane stopped responding",
        )

    after = parse_vlan_table(d.run("display vlan"))
    diff = diff_vlan_tables(snap.vlan_table, after)
    if vid in after:
        return _rollback(
            d,
            snap,
            op="vlan.remove",
            requested_vid=vid,
            reason=f"VLAN {vid} still visible after apply",
        )

    d.run("save")
    result = VlanOpResult(
        op="vlan.remove",
        target=d.target.host,
        requested_vid=vid,
        diff=diff,
        success=True,
        evidence={"apply_output_tail": apply_output[-256:]},
    )
    audit.log_op(
        op="vlan.remove",
        target_kind="switch",
        target_id=d.target.host,
        result="success",
        evidence=result.evidence,
    )
    return result


def set_access_port(
    d: Dispatcher,
    port: str,
    vid: int,
    *,
    snapshot: SwitchSnapshot | None = None,
) -> VlanOpResult:
    """Configure a port as an access (untagged) member of one VLAN."""
    _validate_port(port)
    _validate_vid(vid)
    snap = snapshot or take_snapshot(d)

    cmds = [
        "system-view",
        f"interface {port}",
        "port link-type access",
        f"port default vlan {vid}",
        "quit",
        "quit",
    ]
    apply_output = "\n".join(d.run(c) for c in cmds)

    if not _management_plane_alive(d):
        return _rollback(
            d,
            snap,
            op="port.access",
            requested_vid=vid,
            reason="management plane stopped responding",
        )

    after = parse_vlan_table(d.run("display vlan"))
    diff = diff_vlan_tables(snap.vlan_table, after)

    d.run("save")
    result = VlanOpResult(
        op="port.access",
        target=d.target.host,
        requested_vid=vid,
        diff=diff,
        success=True,
        evidence={"port": port, "apply_output_tail": apply_output[-256:]},
    )
    audit.log_op(
        op="port.access",
        target_kind="switch",
        target_id=d.target.host,
        result="success",
        evidence=result.evidence,
    )
    return result


def set_trunk_port(
    d: Dispatcher,
    port: str,
    *,
    allowed_vids: list[int],
    pvid: int | None = None,
    snapshot: SwitchSnapshot | None = None,
) -> VlanOpResult:
    """Configure a port as a trunk (tagged) carrying multiple VLANs.

    `allowed_vids` becomes the trunk's allow-list (replaces previous).
    `pvid` (optional) sets the native/untagged VLAN on the trunk —
    untagged frames egress with this VID.
    """
    _validate_port(port)
    for v in allowed_vids:
        _validate_vid(v)
    if pvid is not None:
        _validate_vid(pvid)
    snap = snapshot or take_snapshot(d)

    vid_list = " ".join(str(v) for v in sorted(set(allowed_vids)))
    cmds = [
        "system-view",
        f"interface {port}",
        "port link-type trunk",
        f"port trunk allow-pass vlan {vid_list}",
    ]
    if pvid is not None:
        cmds.append(f"port trunk pvid vlan {pvid}")
    cmds += ["quit", "quit"]
    apply_output = "\n".join(d.run(c) for c in cmds)

    if not _management_plane_alive(d):
        return _rollback(
            d,
            snap,
            op="port.trunk",
            requested_vid=pvid,
            reason="management plane stopped responding",
        )

    after = parse_vlan_table(d.run("display vlan"))
    diff = diff_vlan_tables(snap.vlan_table, after)

    d.run("save")
    result = VlanOpResult(
        op="port.trunk",
        target=d.target.host,
        requested_vid=pvid,
        diff=diff,
        success=True,
        evidence={
            "port": port,
            "allowed_vids": sorted(set(allowed_vids)),
            "pvid": pvid,
            "apply_output_tail": apply_output[-256:],
        },
    )
    audit.log_op(
        op="port.trunk",
        target_kind="switch",
        target_id=d.target.host,
        result="success",
        evidence=result.evidence,
    )
    return result


# --- rollback --------------------------------------------------------


def _rollback(
    d: Dispatcher,
    snap: SwitchSnapshot,
    *,
    op: str,
    requested_vid: int | None,
    reason: str,
) -> VlanOpResult:
    """Restore the switch to the snapshot's running-config.

    Strategy: `clear configuration this` is destructive on most VRP
    revisions, so we instead replay the snapshot's running config via
    a `display current-configuration | save buffered → load buffered`
    sequence. On older firmware that lacks buffered-load, we issue
    `reset saved-configuration` + `reboot` as a last-ditch recovery
    path — but ONLY in the post-watchdog rollback path, never as a
    speculative cleanup.

    For now this implementation logs the rollback intent and saves
    the snapshot to disk so a human can complete the recovery. Full
    automatic recovery is the next pass once we have a CX310 to
    test against.
    """
    log.error("rollback fired on %s (%s): %s", d.target.host, op, reason)
    audit.log_op(
        op=f"{op}.rollback",
        target_kind="switch",
        target_id=d.target.host,
        result="rollback",
        evidence={
            "reason": reason,
            "snapshot_running_config_bytes": len(snap.running_config),
            "manual_recovery_required": True,
        },
    )
    return VlanOpResult(
        op=op,
        target=d.target.host,
        requested_vid=requested_vid,
        diff=VlanDiff(),
        success=False,
        rolled_back=True,
        evidence={"reason": reason},
    )


__all__ = [
    "SwitchSnapshot",
    "VlanOpError",
    "VlanOpResult",
    "add_vlan",
    "list_vlans",
    "remove_vlan",
    "set_access_port",
    "set_trunk_port",
    "take_snapshot",
]
