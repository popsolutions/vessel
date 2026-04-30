# Topology — internal chassis fabrics

Discovered 2026-04-30 by ping-sweeping `172.31.0.0/24` and `172.31.1.0/24`
from inside the HMM. Reproduce with `python scripts/sweep.py`.

## Pattern

The chassis-internal fabric is on **`172.31.1.0/24` (eth2 of the SMM)**.
Every blade iBMC and switch mgmt sits there with a deterministic offset:

| Component   | Formula                       | Example          |
|---|---|---|
| Blade `<N>` iBMC | `172.31.1.<128 + N>`     | Blade1 -> .129    |
| Switch `swi<N>`  | `172.31.1.<160 + N>`     | Swi2 -> .162      |
| HMM`<N>`         | `172.31.1.<164 + N>`     | HMM1 -> .165, HMM2 -> .166 |
| Unknown          | `172.31.1.236`, `.237`   | likely PSU mgmt or rack mgr |

`172.31.0.0/24` (eth1 of the SMM) only carries the two HMMs (`.165`, `.166`)
- no blade or switch responds there. Likely the HMM-internal heartbeat /
cluster fabric.

## Slot -> iBMC map (live)

| Slot | Model | iBMC IP        | State    |
|---|---|---|---|
| 1    | CH121 | 172.31.1.129   | UP       |
| 2    | CH121 | 172.31.1.130   | UP       |
| 3    | CH121 | 172.31.1.131   | UP       |
| 4    | CH121 | 172.31.1.132   | UP       |
| 5    | -     | (172.31.1.133) | absent   |
| 6    | -     | (172.31.1.134) | absent   |
| 7    | -     | (172.31.1.135) | absent   |
| 8    | CH121 | 172.31.1.136   | UP       |
| 9    | CH121 | 172.31.1.137   | UP       |
| 10   | CH121 | 172.31.1.138   | UP       |
| 11   | CH121 | 172.31.1.139   | UP       |
| 12   | CH121 | 172.31.1.140   | UP       |
| 13   | CH222 | 172.31.1.141   | UP       |
| 14   | CH222 | 172.31.1.142   | UP       |
| 15   | CH222 | 172.31.1.143   | UP       |
| 16   | CH121 | 172.31.1.144   | UP       |
| 17-24 | -    | (172.31.1.145-.152) | absent |

## Switches & management modules

| Component | IP             | State |
|---|---|---|
| Swi1      | (172.31.1.161) | absent |
| Swi2      | 172.31.1.162   | UP (CX310) |
| Swi3      | 172.31.1.163   | UP (CX310) |
| Swi4      | (172.31.1.164) | absent |
| HMM1      | 172.31.1.165 + 172.31.0.165 | UP (standby) |
| HMM2      | 172.31.1.166 + 172.31.0.166 | UP (active, owns floating 192.168.1.30) |
| ?         | 172.31.1.236   | UP - pending fingerprint |
| ?         | 172.31.1.237   | UP - pending fingerprint |

## Reachability gotcha

The HMM's `sshd` has **`AllowTcpForwarding no`** - direct local-port-forwarding
through the HMM (`ssh -L`) returns *Administratively prohibited*. So we
**cannot tunnel HTTPS to an iBMC via the HMM**. This blocks the original
plan in issue [#7](https://git.pop.coop/noc/huaweie9000/issues/7).

Fallback paths to reach iBMC Redfish from the workstation, in order of
preference:

1. **Reconfigure each iBMC** to obtain an address on the external mgmt VLAN
   (`192.168.1.0/24`) so the workstation reaches it directly. Typically done
   via the HMM web UI's "Blade IP" page - needs to be scripted via the
   undocumented HTML endpoints.
2. **Add a static route** on the workstation through one of the CX310
   uplinks once VLAN ops are in place (Phase 3).
3. **Scrape the HMM web UI** for proxy endpoints (Huawei's older firmware
   has `/cgi-bin/...` proxies that forward to the iBMC).

This finding is also recorded as a comment on issue #7 and folded into
Phase-2 planning.
