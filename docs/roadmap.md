# Roadmap

**Source of truth**: GitHub issues + milestones at
[`popsolutions/vessel`](https://github.com/popsolutions/vessel/issues).
This file mirrors the plan; the live state lives in the issue tracker.

> The legacy Forgejo issue tracker at `git.pop.coop/noc/huaweie9000/issues`
> is now read-only — all open work has been migrated to GitHub.

## Vision

Vessel is **infrastructure-as-code for the Huawei E9000 chassis**. Where
existing tooling forces you through the Java/Palemoon HMM web UI to do
each operation by hand, Vessel treats the whole chassis as a programmable
resource:

- A **Python SDK** that exposes every HMM/iBMC/CX310 operation as a typed call.
- A **REST API** (FastAPI) on top of the SDK, with auto-generated OpenAPI spec.
- A **declarative engine** that reads desired state from YAML/HCL and
  reconciles it against the chassis (Terraform-style).
- **Ansible collection** + **OpenTofu provider** built on the SDK so existing
  IaC pipelines can manage E9000 chassis the same way they manage VMware,
  Junos, or AWS.

> Concrete example — what Vessel makes possible:
>
> ```hcl
> resource "vessel_compute_profile" "k8s_worker" {
>   name             = "k8s-worker-template"
>   bios_boot_order  = ["pxe", "disk"]
>   vnic_profiles    = [vessel_vnic_profile.mgmt.id, vessel_vnic_profile.data.id]
>   mac_pool         = vessel_mac_pool.workers.id
>   power_policy     = "always-on"
> }
>
> resource "vessel_blade" "worker" {
>   for_each = toset(["1","2","3","4","5","6","7","8","9","10"])
>   slot     = tonumber(each.key)
>   profile  = vessel_compute_profile.k8s_worker.id
> }
> ```

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│ L4  IaC integrations  │ Ansible collection │ OpenTofu prov   │
│ L3  Declarative engine│ vessel apply -f chassis.yaml         │
│ L2  REST API (FastAPI)│ /api/v1/chassis/...  + OpenAPI spec  │
│ L1  SDK Python        │ vessel.chassis.* (typed Pydantic)    │
│ L0  Drivers           │ HMM web API + Redfish + VRP CLI + SSH│
└───────────────────────────────────────────────────────────────┘
```

## Principles

- **Snapshot first, change second.** No write-action ships until backup +
  restore for the affected scope is verified on a non-production target.
- **Validation watchdog on switch changes.** A management-plane heartbeat
  must succeed after every commit on the CX310s; if it doesn't, auto-rollback
  using the pre-change snapshot.
- **Audit log is a feature, not telemetry.** Every mutating call writes a
  structured record (actor, target, operation, snapshot id, evidence).
- **Redfish first, HMM web API second, SSH last.** Standardised API beats
  custom CLI. Reach for SSH only when neither covers the operation
  (KVM, `swiconfexport`, VRP-only commands).
- **Issues = memory.** Every non-trivial finding gets an issue. Closed
  issues carry the evidence forward.

## HMM menu → SDK module mapping

This is the surface area Vessel must cover. Each row maps a section of the
Huawei HMM web UI to a Python SDK module and the IaC resources we expose.

| HMM menu | SDK module | IaC resources |
|---|---|---|
| Chassis Settings → Basic / BIOS Boot / Black Box / FC Ports / Reminder / Restore | `vessel.chassis.basic` | `vessel_chassis_settings` |
| Network Settings → MMs / Blades / Switch Modules | `vessel.chassis.network` | `vessel_mgmt_network` |
| Stateless Computing → Profile / MAC Pool / UUID / Node | `vessel.chassis.stateless` | `vessel_compute_profile`, `vessel_mac_pool`, `vessel_uuid_pool` |
| easyLink → Setting / Wizard / Switch Profile / NIC Profile | `vessel.chassis.easylink` | `vessel_switch_profile`, `vessel_vnic_profile` |
| easyLink Network Info → Overview / Ethernet / FC / MAC | `vessel.chassis.network_info` | data sources (read-only) |
| PSUs & Fans → Power Meter / Hibernation / Capping / Records / Fan Meter | `vessel.chassis.power`, `vessel.chassis.fan` | `vessel_power_policy` |
| Alarm Monitoring → Settings / Simulation | `vessel.chassis.alarms` | `vessel_alarm_subscription` |
| System Mgmt → Logs / Account / Security / NTP / SSL / Upgrade | `vessel.chassis.system`, `vessel.chassis.upgrade` | `vessel_user`, `vessel_firmware`, `vessel_ldap_domain`, `vessel_ntp_server` |
| Compute Nodes → Slot 1..16 | `vessel.chassis.blade` | `vessel_blade` |
| Switch Modules → Swi2 / Swi3 (CX310 VRP) | `vessel.chassis.switch` | `vessel_vlan`, `vessel_switch_port`, `vessel_lag` |

## Phases

| Phase | Goal | Gate to advance |
|---|---|---|
| 1 — Discovery & Backup | Snapshot + restore for HMM, switches, each iBMC | Round-trip backup → wipe → restore on a staging blade |
| 2 — Proxmox commissioning | Mount ISO, boot-once, install Proxmox unattended | First blade installed end-to-end without human touch |
| 3 — Switch ops & VLAN | VLAN CRUD, port assignment, link diagnostics on CX310 | VLAN add+remove with auto-rollback verified |
| 4 — GUI / KVM | Local web app for power, mount, console | All phase-1/2/3 actions usable from the browser |
| **5 — Vessel Platform** | **API-first SDK + REST + Ansible + OpenTofu provider** | **End-to-end IaC: VLAN + vNIC profile + bulk firmware via Ansible/OpenTofu** |

## Phase 5 — Vessel Platform (the headline)

Sprints inside Phase 5:

| Sprint | Deliverable | Blocker |
|---|---|---|
| 1 — RE | Capture HMM web API endpoints with mitmproxy/DevTools; document in `docs/hmm-api/*.md` | None — unblocks everything else |
| 2 — SDK Layer | `vessel.chassis.*` typed Python (Pydantic models per HMM subsystem) | Sprint 1 |
| 3 — Bulk firmware | `vessel firmware plan/apply` CLI + Ansible module (rolling waves + auto-rollback) | Sprint 2 |
| 4 — VLAN/vNIC IaC | OpenTofu provider — VLAN, vNIC profile, MAC pool resources | Sprint 2 |
| 5 — Stateless profiles | Compute profile + blade assignment in OpenTofu + Ansible | Sprint 4 |

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
   `commit`, `save`. Interactive shell flow with paging-prompt handling
   (`screen-length 0 temporary`).
3. `hmm-net snapshot <swi>` (uses `swiconfexport` from Phase 1 + raw
   `display current-configuration`).
4. `hmm-net vlan add <id> --name <n>` etc., always inside the
   *snapshot → apply → watchdog → rollback-or-commit* envelope.
5. Diagnostics: `hmm-net diag <blade>` → ping, ARP, MAC table, LLDP
   neighbours.

## Phase 4 — GUI / KVM

1. FastAPI app, single-binary deploy via `uv tool run`. ✅ scaffolded
2. Chassis map view (16-slot grid + 4 switch slots). ✅ done
3. Per-blade actions: power on/off, mount ISO, open console. ✅ done
4. KVM console — Java applet replacement:
   - **OldRLE** path (BGR233 + XOR diff) — ✅ working live against this chassis
   - **NewRLE/JPEG** path — 🟡 ported but produces single-tile output;
     needs pcap capture for byte-diff validation (priority: low)
5. Backup browser: list/download/restore snapshots from the GUI.

## Phase 5 — Vessel Platform

1. **Reverse engineer the HMM web API** (Sprint 1, blocking)
   - Use `mitmproxy` or Chrome DevTools while operator drives the GUI
   - Document each endpoint (path, method, auth, request/response schema)
   - One markdown doc per HMM menu under `docs/hmm-api/`
   - Output: enough to generate a typed SDK
2. **SDK layer** (Sprint 2)
   - `vessel.chassis` package with one module per HMM subsystem
   - Pydantic models for all responses
   - Async + sync APIs
   - Typed errors per failure category
3. **REST API layer** (Sprint 3 in parallel with bulk firmware)
   - Extend existing FastAPI (`gui/app.py`) with `/api/v1/...`
   - All read operations exposed today
   - Mutation endpoints with mandatory backup-first
   - OpenAPI spec at `/api/openapi.json`
4. **Bulk firmware upgrade** (Sprint 3)
   - `vessel firmware inventory` — list all upgradable components and
     current versions (iBMC, BIOS, switches, fans, SMMs)
   - `vessel firmware plan --image <bin> --targets <selector>` — dry-run
   - `vessel firmware apply --plan <file>` — rolling waves with healthcheck
     + auto-rollback on failure
5. **Declarative state engine** (Sprint 4-5)
   - `vessel apply -f chassis.yaml` (plan/apply/destroy semantics)
   - YAML schema for VLANs, vNIC profiles, blade profiles
   - Drift detection
6. **Ansible Collection** (Sprint 3-5)
   - `popsolutions.vessel.chassis_vlan`
   - `popsolutions.vessel.firmware_upgrade`
   - `popsolutions.vessel.blade_provision`
   - `popsolutions.vessel.alarm_query`
7. **OpenTofu Provider** (Sprint 4-5)
   - `provider "vessel"` published to OpenTofu Registry
   - Resources: `vessel_vlan`, `vessel_vnic_profile`, `vessel_switch_profile`,
     `vessel_blade`, `vessel_firmware`, `vessel_compute_profile`,
     `vessel_mac_pool`, `vessel_uuid_pool`

## Live issue tracker

Always check the GitHub board for the freshest list — this table is a
human-friendly snapshot.

| # | Title | Phase | Priority |
|---|---|---|---|
| [#1](https://github.com/popsolutions/vessel/issues/1) | Map HMM smmget/smmset data-item dictionary | 1 | medium |
| [#2](https://github.com/popsolutions/vessel/issues/2) | Determine `swiconfexport` output destination | 1 | medium |
| [#3](https://github.com/popsolutions/vessel/issues/3) | Discover per-blade iBMC IPs in 172.31.0.0/24 | 1 | high |
| [#4](https://github.com/popsolutions/vessel/issues/4) | Implement `hmm-snapshot` CLI | 1 | high |
| [#5](https://github.com/popsolutions/vessel/issues/5) | Implement `hmm-restore <snapshot>` CLI | 1 | high |
| [#6](https://github.com/popsolutions/vessel/issues/6) | Verify backup roundtrip on non-production blade | 1 | high |
| [#7](https://github.com/popsolutions/vessel/issues/7) | Reach iBMC Redfish through SSH tunnel via HMM | 2 | medium |
| [#8](https://github.com/popsolutions/vessel/issues/8) | Confirm iBMC `VirtualMedia` actions | 2 | high |
| [#9](https://github.com/popsolutions/vessel/issues/9) | Implement `hmm-provision <blade> --iso <url>` | 2 | medium |
| [#10](https://github.com/popsolutions/vessel/issues/10) | Proxmox unattended-install answer file | 2 | medium |
| [#11](https://github.com/popsolutions/vessel/issues/11) | CX310 mgmt SSH access + VRP CLI wrapper | 3 | high |
| [#12](https://github.com/popsolutions/vessel/issues/12) | VLAN CRUD with snapshot + watchdog rollback | 3 | high |
| [#13](https://github.com/popsolutions/vessel/issues/13) | Network diagnostics (ping/ARP/MAC/LLDP) | 3 | medium |
| [#14](https://github.com/popsolutions/vessel/issues/14) | FastAPI GUI scaffolding + chassis map | 4 | medium |
| [#15](https://github.com/popsolutions/vessel/issues/15) | Capture & decode legacy Java KVM applet handshake | 4 | medium |
| [#16](https://github.com/popsolutions/vessel/issues/16) | Console: noVNC fallback to SOL | 4 | low |
| [#17](https://github.com/popsolutions/vessel/issues/17) | NewRLE/JPEG decoder: top-row tile copy fails (red square) | 4 | low |
| [#18](https://github.com/popsolutions/vessel/issues/18) | Capture NewRLE pcap from Palemoon for byte-diff | 4 | low |
| [#19](https://github.com/popsolutions/vessel/issues/19) | Port DQT tables for JPEG quality 20/30/40/60-100% | 4 | low |
| [#20](https://github.com/popsolutions/vessel/issues/20) | CX310 dispatcher: interactive shell flow | 3 | medium |
| [#21](https://github.com/popsolutions/vessel/issues/21) | Audit log for all chassis-mutating operations | 5 | high |
| [#22](https://github.com/popsolutions/vessel/issues/22) | GUI authentication (currently zero auth) | 4 | high |
