# Vessel

**Infrastructure-as-code for the Huawei E9000 blade chassis.**

Where existing tooling forces operators through the Java/Palemoon HMM
web UI to do each operation by hand, Vessel treats the whole chassis
as a programmable resource: typed Python SDK, REST API, declarative
state engine, Ansible collection, and OpenTofu provider — all built on
top of the same chassis primitives.

## Why

A Huawei E9000 chassis carries up to 32 blades, 4 switch modules,
multiple power and fan modules, and a management complex. The
out-of-the-box tooling is:

- a 16-year-old Java applet that requires Palemoon to render the KVM
  console
- a per-action web UI for every change (no bulk operations)
- one-by-one firmware upgrades
- no programmable interface for VLAN, vNIC, MAC pool, or
  stateless-computing profiles

Vessel makes the chassis behave like every other piece of modern
infrastructure: declare the desired state in YAML/HCL, the platform
reconciles.

## Status

Today: working OldRLE KVM client, chassis discovery, snapshot/restore
scaffolding, GUI with power/boot/ISO mount, audit log, GUI auth.

Next: reverse-engineer the HMM web API, build the typed SDK on top,
expose REST + Ansible + OpenTofu.

See the [roadmap](https://github.com/popsolutions/vessel/blob/main/docs/roadmap.md)
for the full plan and [issues](https://github.com/popsolutions/vessel/issues)
for the live work.

## Quick links

- [Getting started](getting-started.md)
- [Architecture](architecture.md)
- [Audit log](operations/audit-log.md) — every chassis-mutating op gets a record
- [Health checks](operations/health-checks.md) — `/healthz` + `/readyz`
- [GUI auth](operations/gui-auth.md) — bcrypt Basic Auth
- [Security policy](https://github.com/popsolutions/vessel/blob/main/SECURITY.md)

## License

Apache-2.0.
