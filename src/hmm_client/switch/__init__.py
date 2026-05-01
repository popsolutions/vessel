"""CX310 / VRP switch operations.

The E9000 chassis carries up to four switch modules in slots Swi1..4
(this chassis has CX310s in Swi2 and Swi3 — see `docs/discovery.md`).
Each switch runs Huawei's VRP NOS and is reachable over SSH on the
internal mgmt fabric (172.31.1.x) — usually via the HMM as a
jump-box.

This subpackage exposes:
    - `Dispatcher`  → SSH connection wrapper (paramiko)
    - `parse_vlan_table()` / `diff_vlan_tables()`  → VRP `display vlan`
      output parsers and a structural diff for backup-vs-current
      comparison.

Backup-first is non-negotiable for any write op (matches the project
rule from MEMORY.md): every mutating call MUST first invoke
`swiconfexport swi<N>` via the HMM dispatcher and verify the resulting
tarball before pushing VLAN changes.
"""

from .dispatcher import Dispatcher, DispatcherError, SshTarget, open_dispatcher
from .vlan import (
    PortMembership,
    VlanDiff,
    VlanEntry,
    diff_vlan_tables,
    parse_vlan_table,
)

__all__ = [
    "Dispatcher",
    "DispatcherError",
    "PortMembership",
    "SshTarget",
    "VlanDiff",
    "VlanEntry",
    "diff_vlan_tables",
    "open_dispatcher",
    "parse_vlan_table",
]
