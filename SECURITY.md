# Security Policy

Vessel manages enterprise blade chassis — power, firmware, networking,
and remote console for Huawei E9000 hardware. A vulnerability here can
mean unintended power-cycle of production workloads, leaked credentials,
or unauthorised access to the chassis fabric. We take reports
seriously.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use one of the private channels below:

- **GitHub Security Advisories** (preferred):
  <https://github.com/popsolutions/vessel/security/advisories/new>
- **Email**: `security@popsolutions.co`
- **Encrypted email**: same address, GPG key fingerprint published at
  <https://popsolutions.co/.well-known/security.asc>
- **Telegram**: `@PopVesselBot` accepts security reports forwarded by
  the maintainers; do **not** send credentials over this channel.

Include in your report:

- Affected component (CLI command, GUI route, SDK call, etc.) and
  version/commit SHA.
- A clear description of the issue and its impact.
- Reproduction steps. Minimal proof-of-concept is appreciated.
- Whether you would like public credit when the advisory is published.

## What to Expect

| Timeline | Action |
|---|---|
| 48 hours | Acknowledgement of your report. |
| 7 days | Initial triage: confirmed / not-applicable / needs more info. |
| 30 days | Fix shipped (or a written timeline if a longer fix is needed). |
| Public disclosure | Coordinated with reporter. We follow a 90-day default disclosure window. |

We publish fixed advisories at
<https://github.com/popsolutions/vessel/security/advisories>.

## Scope

**In scope** — anything that could:

- Allow unauthenticated chassis access through the GUI, CLI, or SDK.
- Bypass the snapshot-before-write rule on mutating operations.
- Leak credentials (HMM password, AES keys, Telegram tokens, SSH keys).
- Send commands to unintended targets (wrong blade, wrong switch, wrong
  VLAN).
- Skip auto-rollback after a failed health check on switch ops.
- Inject code through a captured chassis response (deserialisation,
  XSS in the GUI canvas/status bar, SQL/command injection).
- Compromise the supply chain (CI workflow, release artifact,
  container image).

**Out of scope** — please don't report:

- Vulnerabilities in the Huawei HMM, iBMC, or CX310 firmware
  themselves. Report those to Huawei directly.
- Issues that require physical access to the chassis serial console.
- Issues that require an already-compromised operator workstation.
- Findings from automated scanners with no demonstrated impact.

## Safe Harbour

We will not pursue legal action against researchers who:

- Make a good-faith effort to follow this policy.
- Avoid intentionally degrading service for other users of the targeted
  chassis.
- Do not access, modify, or destroy data beyond what is necessary to
  demonstrate the vulnerability.
- Give us reasonable time to fix the issue before public disclosure.

We will publicly credit researchers who help us improve Vessel
(unless they request anonymity) in the relevant advisory and release
notes.

## Hardening Checklist for Operators

Even with no known vulnerabilities, deployments should:

- [ ] Run the GUI behind authentication (issue #22). Default
  `127.0.0.1` bind has no auth.
- [ ] Set `VESSEL_TG_BOT_TOKEN` and other credentials via env vars or a
  secret manager, never in committed files.
- [ ] Verify backups round-trip on a non-production target before
  enabling write operations (issue #6).
- [ ] Pin chassis-internal SSH host keys in `~/.ssh/known_hosts`.
- [ ] Keep the audit log (issue #21) on append-only / WORM storage.
- [ ] Review the `re/captures/` directory for accidentally committed
  pcaps before pushing.

## Acknowledgements

To be added as advisories close.
