# Huawei E9000 — Linux-native client

Linux-native tooling to operate a Huawei E9000 blade chassis without the legacy
Java applet (Palemoon-on-Windows). Goal: backup-then-change workflow for
**chassis ops, switch (VLAN) ops, and Proxmox commissioning** of the blades.

> Roadmap and progress are tracked as issues on
> [`git.pop.coop/noc/huaweie9000`](https://git.pop.coop/noc/huaweie9000/issues)
> — git is the system of record for code *and* memory.

## Hardware (discovered 2026-04-30)

| Component | Model | Slots populated | Status |
|---|---|---|---|
| Compute blades | CH121 (half-width) | 1, 2, 3, 4, 8, 9, 10, 11, 12, 16 | 10× Enabled |
| Storage blades | CH222 (full-width) | 13, 14, 15 | 3× Enabled |
| Switch modules | CX310 (10GE) | Swi2, Swi3 | 2× Enabled (Swi1, Swi4 absent) |
| Management modules | SMM (HMM) | HMM1, HMM2 | active/standby |
| SMM software | v7.63 (IPMI module built 2021-08-23) | — | — |

**IP layout**
- `192.168.1.30` — floating (active SMM) — entry point
- `192.168.1.31` — HMM1 static
- `192.168.1.32` — HMM2 static (currently the active one — owns the floating)
- `172.31.0.0/24` (eth1 on SMM) — internal blade iBMC fabric
- `172.31.1.0/24` (eth2 on SMM) — internal switch mgmt fabric

## Service surface

| Protocol | Port | Status | Notes |
|---|---|---|---|
| HTTPS / Redfish | 443 | open | DMTF Redfish 1.0.2 — chassis-level only; `Systems` collection is **empty** |
| SSH | 22 | open | restricted dispatcher with custom vocabulary (`smmget`, `smmset`, `swiconfexport`, …) |
| HTTP | 80 | open | login UI |
| IPMI | 623 | filtered | likely chassis-internal only |
| SNMP | 161 | closed | — |
| VNC / KVM | 5900 | filtered | session-allocated; legacy Java applet target |

See [`docs/discovery.md`](docs/discovery.md) for raw evidence and vocabulary.

## Status (2026-04-30)

### What works today

The `hmm` CLI replaces the legacy applet for chassis ops:

```bash
hmm list                         # populated blades + switches (12s)
hmm power <slot> on|off|reset|cycle|nmi
hmm boot  <slot> none|pxe|hdd|cd|floppy [--reboot]
hmm bootdev <slot>
hmm sessions list|clean          # manage Redfish session slots
hmm discover                     # full Redfish walk
hmm snapshot                     # backup HMM + switches (140 KB)
hmm drift <snapshot-dir>         # compare snapshot vs live
```

Tested live against the production HMM. Power/boot reach the iBMC via the
HMM SSH dispatcher (no virtual media needed for those ops).

### What's still in progress

| Capability | State | Tracking |
|---|---|---|
| VirtualMedia mount (boot ISO) | foundation done; protocol RE in progress | [#20](https://git.pop.coop/noc/huaweie9000/issues/20) |
| KVM console (video + input) | applet decompiled; protocol partly mapped | [#15](https://git.pop.coop/noc/huaweie9000/issues/15), [#16](https://git.pop.coop/noc/huaweie9000/issues/16) |
| FastAPI GUI | not started | [#14](https://git.pop.coop/noc/huaweie9000/issues/14) |
| Switch VRP CLI | not started | [#11](https://git.pop.coop/noc/huaweie9000/issues/11) |

The protocol is genuinely deep — the iBMC has aggressive auth-failure
lockouts (~30-120s) and the KVM stream uses a custom `[FE F6 lenH lenL]`
framing with CRC16-LE bodies that may need AES-CBC encryption when
`securekvm=1`. Each unknown takes empirical iteration.

## Quick start

```bash
uv venv -p 3.13 .venv && . .venv/bin/activate
uv pip install -e .

cp .env.example .env  # then edit with real credentials

hmm list              # confirm CLI works against your chassis
```

## Roadmap

Tracked as Forgejo milestones + issues. Detail in
[`docs/roadmap.md`](docs/roadmap.md). Four phases:

1. **Discovery & Backup** ✅ shipped (`hmm snapshot`, `hmm drift`)
2. **Proxmox commissioning** 🟡 in progress — `hmm vmedia mount` on issue [#20](https://git.pop.coop/noc/huaweie9000/issues/20)
3. **Switch ops & VLAN** ⏭️ blocked on direct CX310 access (issue [#11](https://git.pop.coop/noc/huaweie9000/issues/11))
4. **GUI** ⏭️ depends on Phase 2/3

## Layout

```
src/hmm_client/
  config.py          # Settings, env loader
  redfish.py         # DMTF Redfish 1.0.2 client
  ops.py             # power/boot/inventory via SSH-jump + Redfish
  cli.py             # Typer CLI (hmm <subcommand>)
  discover.py        # read-only Redfish walker
  snapshot.py        # backup HMM + switches
  restore.py         # drift detection (read-only)
  vmedia/            # VirtualMedia client (in progress)
    proto.py         # 12-byte VM frame primitives
    crypto.py        # AES-128-CBC + key parsers
    login.py         # HMM Web auth + per-session embed extraction
    client.py        # VM data-plane TCP
    kvm_stream.py    # port-2198 KVM framer (FE F6 + CRC16)
docs/
  discovery.md       cli-vocabulary.md   topology.md
  roadmap.md         kvm-protocol-re.md
re/                  # gitignored: vconsole.jar + decompiled (Huawei IP)
discovery/           # gitignored: raw API dumps
snapshots/           # gitignored: backup payloads
```

## Safety stance

- Change-making code is gated by a snapshot precondition: no write without a
  prior backup of the affected scope.
- Switch changes use a watchdog (auto-rollback on mgmt-loss) before commit.
- Credentials only in `.env` (gitignored). `.env.example` documents the schema.
