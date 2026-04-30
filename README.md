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

## Quick start

```bash
uv venv -p 3.13 .venv && . .venv/bin/activate
uv pip install -e .

cp .env.example .env  # then edit with real credentials

# read-only Redfish discovery → writes ./discovery/<UTC-stamp>/
python -m hmm_client.discover
```

## Roadmap

Tracked as Forgejo milestones + issues. Detail in
[`docs/roadmap.md`](docs/roadmap.md). Four phases, in order:

1. **Discovery & Backup** — non-destructive. Snapshot HMM + each switch + each
   iBMC config. Restore command. Gate: full round-trip backup → restore
   verified on a staging blade.
2. **Proxmox commissioning** — Redfish `VirtualMedia` on each blade's iBMC
   (not the SMM Redfish, which lacks Systems/VirtualMedia). HTTP-served ISO,
   boot-once, answer file.
3. **Switch ops & VLAN** — SSH wrapper for CX310 (Huawei VRP CLI). Pattern:
   *snapshot → apply → validate (ping/LLDP) → auto-rollback if mgmt drops*.
4. **GUI** — FastAPI + HTMX local web app. Chassis map, power/mount/console
   buttons. KVM strategy decided after applet protocol analysis (likely
   embedded noVNC, with SOL fallback).

## Layout

```
src/hmm_client/
  __init__.py
  config.py        # Settings, env loader
  redfish.py       # DMTF Redfish 1.0.2 client (session auth)
  discover.py      # read-only inventory walker
docs/
  discovery.md     # findings, vocabulary, gaps
  roadmap.md       # phases + issue links
discovery/         # gitignored: raw API dumps
snapshots/         # gitignored: backup payloads
```

## Safety stance

- Change-making code is gated by a snapshot precondition: no write without a
  prior backup of the affected scope.
- Switch changes use a watchdog (auto-rollback on mgmt-loss) before commit.
- Credentials only in `.env` (gitignored). `.env.example` documents the schema.
