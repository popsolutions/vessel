# Roadmap

Source of truth: Forgejo issues + milestones at
[`git.pop.coop/noc/huaweie9000`](https://git.pop.coop/noc/huaweie9000/issues).
This file mirrors the plan; the live state lives in the issue tracker.

## Principles

- **Snapshot first, change second.** No write-action ships until backup +
  restore for the affected scope is verified on a non-production target.
- **Validation watchdog on switch changes.** A management-plane heartbeat
  must succeed after every commit on the CX310s; if it doesn't, auto-rollback
  using the pre-change snapshot.
- **Redfish first, SSH fallback.** Standardized API beats custom CLI when
  both can do the job. SSH is for what Redfish 1.0.2 does not cover (KVM,
  HMM-only commands like `swiconfexport`).
- **Issues = memory.** Every non-trivial finding gets an issue. Closed issues
  carry the evidence forward.

## Phase summary

| Phase | Goal | Gate to advance |
|---|---|---|
| 1 — Discovery & Backup | Snapshot + restore for HMM, switches, each iBMC | Round-trip backup → wipe → restore on a staging blade |
| 2 — Proxmox commissioning | Mount ISO, boot-once, install Proxmox unattended | First blade installed end-to-end without human touch |
| 3 — Switch ops & VLAN | VLAN CRUD, port assignment, link diagnostics on CX310 | VLAN add+remove with auto-rollback verified |
| 4 — GUI | Local web app for power, mount, console | All phase-1/2/3 actions usable from the browser |

## Phase 1 — Discovery & Backup

1. Map full HMM `smmget`/`smmset` data-item dictionary (probe + cross-ref the
   Huawei HMM CLI Reference).
2. Determine `swiconfexport` output destination (does it write to a local
   path? push via SFTP? require a remote target?). Test on Swi2.
3. Discover per-blade iBMC IPs in the `172.31.0.0/24` fabric (sweep + map
   slot ↔ IP).
4. Implement `hmm-snapshot` CLI that produces, under
   `snapshots/<UTC-stamp>/`:
   - `hmm/redfish.json` — full Redfish dump (already prototyped in
     `discover.py`)
   - `hmm/smmget.txt` — every known data item
   - `hmm/accounts.json`
   - `switches/swi<N>.cfg` — VRP running-config + startup-config
   - `ibmc/blade<N>.json` — Redfish dump from each iBMC
   - `manifest.yaml` — what was captured, by whom, when, hashes
5. Implement `hmm-restore <snapshot>` for each scope (HMM, switch, iBMC).
6. Verify round-trip on a non-production blade.

## Phase 2 — Proxmox commissioning

1. Reach iBMC Redfish through SSH tunnel via HMM (`ssh -L`).
2. Confirm `VirtualMedia` is exposed by the iBMC; document the
   `InsertMedia`/`EjectMedia` actions.
3. HTTP host the Proxmox ISO from the workstation (`python -m http.server` or
   a small `httpx` server) on a routable interface for the iBMC.
4. Implement `hmm-provision <blade>` that: snapshot → mount ISO → set
   one-shot boot=CD → power on → wait for install → eject → confirm.
5. Build a minimal Proxmox answer file for unattended install.
6. Validate cluster: 10× CH121 compute + 3× CH222 ceph-osd.

## Phase 3 — Switch ops & VLAN

1. Confirm CX310 mgmt IPs and SSH access (probably via HMM jump).
2. Wrapper around VRP CLI: `system-view`, `vlan`, `interface`, `port`,
   `commit`, `save`.
3. `hmm-net snapshot <swi>` (uses `swiconfexport` from Phase 1 + raw
   `display current-configuration`).
4. `hmm-net vlan add <id> --name <n>` etc., always inside the
   *snapshot → apply → watchdog → rollback-or-commit* envelope.
5. Diagnostics: `hmm-net diag <blade>` → ping, ARP, MAC table, LLDP
   neighbours.

## Phase 4 — GUI

1. FastAPI app, single-binary deploy via `uv tool run`.
2. Chassis map view (16-slot grid + 4 switch slots).
3. Per-blade actions: power on/off, mount ISO, open console.
4. Console:
   - Best case: decode the Java applet's KVM handshake (Wireshark + `jadx`
     on the JNLP/JAR), reimplement, embed via noVNC.
   - Fallback: SOL (Serial-over-LAN) over SSH, rendered by xterm.js.
5. Backup browser: list/download/restore snapshots from the GUI.

## Live issue tracker

| # | Title | Phase | Status |
|---|---|---|---|
| [#1](https://git.pop.coop/noc/huaweie9000/issues/1) | Map HMM `smmget`/`smmset` data-item dictionary | 1 | open |
| [#2](https://git.pop.coop/noc/huaweie9000/issues/2) | Determine `swiconfexport` output destination | 1 | open |
| [#3](https://git.pop.coop/noc/huaweie9000/issues/3) | Discover per-blade iBMC IPs in 172.31.0.0/24 | 1 | open |
| [#4](https://git.pop.coop/noc/huaweie9000/issues/4) | Implement `hmm-snapshot` CLI | 1 | open |
| [#5](https://git.pop.coop/noc/huaweie9000/issues/5) | Implement `hmm-restore <snapshot>` CLI | 1 | open |
| [#6](https://git.pop.coop/noc/huaweie9000/issues/6) | Round-trip backup → wipe → restore on staging blade | 1 | open |
| [#7](https://git.pop.coop/noc/huaweie9000/issues/7) | Reach iBMC Redfish via SSH tunnel through HMM | 2 | open |
| [#8](https://git.pop.coop/noc/huaweie9000/issues/8) | Confirm iBMC `VirtualMedia` actions | 2 | open |
| [#9](https://git.pop.coop/noc/huaweie9000/issues/9) | Implement `hmm-provision <blade> --iso <url>` | 2 | open |
| [#10](https://git.pop.coop/noc/huaweie9000/issues/10) | Proxmox unattended-install answer file | 2 | open |
| [#11](https://git.pop.coop/noc/huaweie9000/issues/11) | CX310 mgmt SSH access + VRP CLI wrapper | 3 | open |
| [#12](https://git.pop.coop/noc/huaweie9000/issues/12) | VLAN CRUD with snapshot + watchdog rollback | 3 | open |
| [#13](https://git.pop.coop/noc/huaweie9000/issues/13) | Network diagnostics (ping/ARP/MAC/LLDP) | 3 | open |
| [#14](https://git.pop.coop/noc/huaweie9000/issues/14) | FastAPI GUI scaffolding + chassis map | 4 | open |
| [#15](https://git.pop.coop/noc/huaweie9000/issues/15) | Capture & decode legacy Java KVM applet handshake | 4 | open |
| [#16](https://git.pop.coop/noc/huaweie9000/issues/16) | Console: noVNC fallback to SOL | 4 | open |
