# Architecture

Vessel is a 4-layer stack. Each layer is independently usable; you can
embed the SDK in your own automation, sit on top of the REST API, or
just use the CLI.

```
┌───────────────────────────────────────────────────────────────┐
│ L4  IaC integrations  │ Ansible collection │ OpenTofu prov   │
│ L3  Declarative engine│ vessel apply -f chassis.yaml         │
│ L2  REST API (FastAPI)│ /api/v1/chassis/...  + OpenAPI spec  │
│ L1  SDK Python        │ vessel.chassis.* (typed Pydantic)    │
│ L0  Drivers           │ HMM web API + Redfish + VRP CLI + SSH│
└───────────────────────────────────────────────────────────────┘
```

## L0 — Drivers

Talk to the actual chassis hardware:

- **HMM web API** (under reverse engineering — see Phase 5 Sprint 1)
- **Redfish 1.0.2** on the SMM and per-blade iBMCs
- **VRP CLI** on the CX310 switches via SSH
- **Custom binary KVM protocol** for the iKVM video stream
  (`hmm_client.kvm.*`)

## L1 — SDK

`hmm_client.*` Python package (will be renamed `vessel.*` once the SDK
crystallises). Typed Pydantic models, sync + async APIs, one module
per HMM subsystem.

## L2 — REST API

FastAPI app (`hmm_client.gui.app`) — currently powers the local web GUI.
Phase 5 Sprint 3 expands it with `/api/v1/...` mirroring the SDK.

## L3 — Declarative engine

`vessel apply -f chassis.yaml` — Terraform-like reconciliation against
desired state. Snapshot before write, watchdog-checked, auto-rollback.

## L4 — IaC integrations

- **Ansible collection** `popsolutions.vessel.*` modules
- **OpenTofu provider** — `provider "vessel"` published to the Registry

## Cross-cutting

- **Audit log** ([details](operations/audit-log.md)) — NDJSON,
  every mutating op gets a record. Required by all layers.
- **Snapshot** before every mutating op — never write without a
  recoverable baseline.
- **Telegram notifications** for long-running ops.
