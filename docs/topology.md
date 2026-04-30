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

## Reaching iBMCs - SSH jump (works)

The HMM's `sshd` has `AllowTcpForwarding no`, so `ssh -L` direct tunneling
returns *Administratively prohibited*. **But** the HMM's restricted
dispatcher exposes its own `ssh` command, and the iBMCs accept the same
`root` password as the HMM. So the working pattern is:

```
ssh root@192.168.1.30                 # workstation -> HMM
> ssh 172.31.1.129 NoStricHostKeyChecking
> <Huawei12#$ password>
root@BMC:/#                           # we are now inside the iBMC
```

Inside the iBMC: a different restricted dispatcher with `ipmcget` and
`ipmcset` (see `docs/cli-vocabulary.md` once expanded). Vocabulary on this
firmware (iMana v6.05, Jan-2015 build):

```
TMOUT  df  dmesg  exit  free  ifconfig  ipmcget  ipmcset
maintenance_debug  netstat  ping  ps  route  top
```

`ipmcset -d bootdevice -v PXE` and `ipmcset -d powerstate -v reset` (etc.)
let us drive boot/power. **No virtual-media command** on this firmware -
Phase 2 must use PXE rather than Redfish `VirtualMedia`.

## What's NOT reachable directly

- iBMC Redfish HTTP from the workstation - blocked by HMM's
  `AllowTcpForwarding no`. Workaround: paramiko `direct-tcpip` is also
  prohibited, so we have to either (a) put the iBMC on the external mgmt
  VLAN via `ipmcset -t eth0 ...`, or (b) drive iBMC entirely from inside
  the SSH jump shell.
- The chassis Redfish at the SMM does **not** proxy to iBMCs:
  `/redfish/v1/Systems/Blade1` returns 403; `/Chassis/Blade1/Power` 403.
  Per-blade compute info is iBMC-only.
