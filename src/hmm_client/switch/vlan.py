"""VRP `display vlan` parser + structural diff.

Huawei VRP's `display vlan` produces a fixed-width text table along
the lines of::

    The total number of vlans is : 4

    U: Up;         D: Down;         TG: Tagged;         UT: Untagged;
    MP: Vlan-mapping; ST: Vlan-stacking;
    #: ProtocolTransparent-vlan;  *: Management-vlan;

    VID  Type    Ports
    -------------------------------------------------------------
    1    common  UT:GE0/0/1(U)    GE0/0/2(U)
    10   common  TG:GE0/0/3(D)
    20   common  TG:GE0/0/3(D)    GE0/0/4(U)
    100  common  UT:GE0/0/5(U)

This module exposes a regex-based parser (`parse_vlan_table`) and a
structural diff (`diff_vlan_tables`) so we can compare a current
snapshot against a stored baseline before pushing VLAN changes.

Parser is intentionally permissive — VRP versions vary the column
spacing, and CX310 firmware revisions add extra trailing columns.
We only require a header row containing literal "VID" and "Type",
plus rows starting with a numeric VID.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Match VRP port spec like "UT:GE0/0/1(U)" — mode prefix is OPTIONAL
# because VRP only prints "UT:" / "TG:" before the FIRST port of a
# contiguous mode group; subsequent ports inherit the prefix.
_PORT_TOKEN_RE = re.compile(
    r"(?:(?P<prefix>UT|TG|MP|ST):)?(?P<iface>[A-Za-z]+\d+(?:/\d+)*)\((?P<state>[UD])\)"
)
_HEADER_RE = re.compile(r"^\s*VID\s+Type\s+Ports?\b", re.IGNORECASE)
_ROW_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.*)$")


@dataclass(frozen=True)
class PortMembership:
    iface: str        # e.g. "GE0/0/1"
    mode: str         # "UT" (untagged), "TG" (tagged), etc.
    link_state: str   # "U" up, "D" down


@dataclass
class VlanEntry:
    vid: int
    vlan_type: str = "common"
    ports: list[PortMembership] = field(default_factory=list)
    raw: str = ""

    @property
    def tagged_ports(self) -> list[str]:
        return [p.iface for p in self.ports if p.mode == "TG"]

    @property
    def untagged_ports(self) -> list[str]:
        return [p.iface for p in self.ports if p.mode == "UT"]


def parse_vlan_table(output: str) -> dict[int, VlanEntry]:
    """Parse VRP `display vlan` text into a {vid: VlanEntry} mapping.

    Robust to row-wrapped port lists: if a line has no VID prefix, we
    treat its tokens as a continuation of the previous VID's port list.
    """
    entries: dict[int, VlanEntry] = {}
    current: VlanEntry | None = None
    seen_header = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            current = None
            continue
        if _HEADER_RE.match(line):
            seen_header = True
            continue
        if not seen_header:
            continue
        row = _ROW_RE.match(line)
        if row:
            vid = int(row.group(1))
            entry = VlanEntry(vid=vid, vlan_type=row.group(2), raw=line)
            entry.ports.extend(_extract_ports(row.group(3)))
            entries[vid] = entry
            current = entry
        elif current is not None:
            # continuation line — more ports for the previous VID
            current.ports.extend(_extract_ports(line))
            current.raw = current.raw + "\n" + line

    return entries


def _extract_ports(text: str, initial_mode: str = "UT") -> list[PortMembership]:
    """Parse port tokens. Mode prefix is sticky within a row.

    VRP only prints the mode (UT:/TG:) before the first port of a
    contiguous mode group; later ports in the same group omit the
    prefix. We carry the most recently seen mode forward so they all
    get classified correctly.
    """
    out: list[PortMembership] = []
    current_mode = initial_mode
    for m in _PORT_TOKEN_RE.finditer(text):
        if m.group("prefix"):
            current_mode = m.group("prefix")
        out.append(PortMembership(
            iface=m.group("iface"),
            mode=current_mode,
            link_state=m.group("state"),
        ))
    return out


@dataclass
class VlanDiff:
    added_vids: list[int] = field(default_factory=list)
    removed_vids: list[int] = field(default_factory=list)
    membership_changes: dict[int, dict[str, list[str]]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return (
            not self.added_vids
            and not self.removed_vids
            and not self.membership_changes
        )


def diff_vlan_tables(
    baseline: dict[int, VlanEntry],
    current: dict[int, VlanEntry],
) -> VlanDiff:
    """Return a structural diff: added/removed VLANs + per-VID port changes.

    Membership changes are reported per-mode (tagged / untagged) as
    `{"tagged_added": [...], "tagged_removed": [...], ...}`.
    """
    diff = VlanDiff(
        added_vids=sorted(set(current) - set(baseline)),
        removed_vids=sorted(set(baseline) - set(current)),
    )
    for vid in sorted(set(baseline) & set(current)):
        b = baseline[vid]
        c = current[vid]
        b_tag, c_tag = set(b.tagged_ports), set(c.tagged_ports)
        b_unt, c_unt = set(b.untagged_ports), set(c.untagged_ports)
        change: dict[str, list[str]] = {}
        if b_tag != c_tag:
            change["tagged_added"]   = sorted(c_tag - b_tag)
            change["tagged_removed"] = sorted(b_tag - c_tag)
        if b_unt != c_unt:
            change["untagged_added"]   = sorted(c_unt - b_unt)
            change["untagged_removed"] = sorted(b_unt - c_unt)
        if change:
            diff.membership_changes[vid] = change
    return diff
