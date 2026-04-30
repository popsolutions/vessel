# Discovery — 2026-04-30

Read-only enumeration of the Huawei E9000 chassis at `192.168.1.30` (active
HMM floating IP). Raw API dumps live in `./discovery/<UTC-stamp>/` (gitignored).

## 1. Network reachability

| Protocol | Port | State | Notes |
|---|---|---|---|
| SSH | 22 | open | banner `SSH-2.0-` (truncated identifier) |
| Telnet | 23 | closed | — |
| HTTP | 80 | open | login UI |
| SNMP | 161 | closed | disabled |
| HTTPS | 443 | open | HMM Web + Redfish |
| IPMI | 623 | filtered | chassis-internal only |
| VNC | 5900/5901 | filtered | session-allocated for KVM |

ICMP RTT ~15 ms.

## 2. SMM (HMM) version

```
SMM Version Information:
Uboot    Version :(U54)3.03
CPLD     Version :(U1082)108  161206
PCB      Version :SMMA REV B
BOM      Version :001
FPGA     Version :(U1049)008  130605
Software Version :(U54)7.63
IPMI Module Built:Aug 23 2021 15:10:44
```

Retrieved via `smmget -d version` over SSH.

## 3. SMM network interfaces

```
eth0    192.168.1.32/24      (HMM2 static IP, currently active module)
eth0:0  192.168.1.30/24      (floating IP — moves with active HMM)
eth1    172.31.0.166/24      (internal: blade iBMC fabric)
eth2    172.31.1.166/24      (internal: switch mgmt fabric)
lo      127.0.0.1
```

The user's documented entry point `192.168.1.30` is the **floating IP**; SSH
to it lands on whichever module is currently active. Per
`/redfish/v1/Managers/HMM1/EthernetInterfaces/StaticEth0`, HMM1's static is
`192.168.1.31`.

## 4. Redfish surface (DMTF 1.0.2)

Discovery enumerated **65 resources** under `/redfish/v1/`. Full dump:
`discovery/20260430T070459Z/`.

### What is exposed

- `/redfish/v1/Chassis/1` — top-level chassis (`E9000`)
- `/redfish/v1/Chassis/Blade1..24` — per-slot Chassis entries (compute/storage)
- `/redfish/v1/Chassis/Swi1..4` — per-slot switch Chassis entries
- `/redfish/v1/Chassis/HMM1`, `/HMM2` — management modules
- `/redfish/v1/Managers/HMM1`, `/HMM2` — manager objects with
  `EthernetInterfaces`, `SerialInterfaces`, `NetworkProtocol`, `LogServices`
- `/redfish/v1/AccountService/Accounts` — 1 account (`root`, RoleId
  `Administrator`, `UserInterfaces` includes WEB/SNMP/SSH/SFTP/KVM/REDFISH)
- `/redfish/v1/UpdateService`, `EventService`, `TaskService`, `SessionService`
- `/redfish/v1/Registries` — message registries (Base, HMM, HMMEvents)

### Critical gaps

- **`/redfish/v1/Systems` is empty** (`Members@odata.count: 0`). The HMM
  Redfish surface does **not** expose ComputerSystem objects. Therefore:
  - No `Bios`, `Boot`, `Storage`, `Processors`, `Memory` per blade
  - No `VirtualMedia` (the entry point for ISO mount → Proxmox install)
  - No `Boot.BootSourceOverrideTarget` for one-shot CD/PXE boot
- These capabilities exist on **each blade's own iBMC**, reachable from the
  SMM via the internal `172.31.0.0/24` fabric. The Linux client must talk
  directly to each iBMC's Redfish, not to the HMM's.

## 5. Hardware inventory

### Switch modules

| Slot | Model | State |
|---|---|---|
| Swi1 | — | Absent |
| Swi2 | CX310 | Enabled |
| Swi3 | CX310 | Enabled |
| Swi4 | — | Absent |

CX310 = 10GE switch; 16× 10GE downlinks (one per blade) + 8× 10GE uplinks.
Two switches → A-fabric + B-fabric redundancy.

### Blades

| Slot | Model | State | | Slot | Model | State |
|---|---|---|---|---|---|---|
| 1  | CH121 | Enabled |    | 13 | CH222 | Enabled |
| 2  | CH121 | Enabled |    | 14 | CH222 | Enabled |
| 3  | CH121 | Enabled |    | 15 | CH222 | Enabled |
| 4  | CH121 | Enabled |    | 16 | CH121 | Enabled |
| 5  | —     | Absent  |    | 17 | —     | Absent  |
| 6  | —     | Absent  |    | 18 | —     | Absent  |
| 7  | —     | Absent  |    | 19 | —     | Absent  |
| 8  | CH121 | Enabled |    | 20 | —     | Absent  |
| 9  | CH121 | Enabled |    | 21 | —     | Absent  |
| 10 | CH121 | Enabled |    | 22 | —     | Absent  |
| 11 | CH121 | Enabled |    | 23 | —     | Absent  |
| 12 | CH121 | Enabled |    | 24 | —     | Absent  |

**Totals:** 10× CH121 (compute, half-width), 3× CH222 (storage, full-width),
13 blades active.

### Management modules

| Slot | Static IP | Floating | Role |
|---|---|---|---|
| HMM1 | 192.168.1.31 | — | (standby at discovery time) |
| HMM2 | 192.168.1.32 | 192.168.1.30 | active |

## 6. HMM SSH CLI vocabulary

Login: `ssh root@192.168.1.30`. Lands in a Wind River Linux 4.2 shell with a
**restricted command dispatcher** (most Unix commands return
`unknown command : <cmd>`). Vocabulary discovered via Tab-Tab on the empty
prompt:

```
/smm/smmget   /smm/smmset   TMOUT     clear
cmdext        date          exit      history
ifconfig      ntpq          ping      ping6
reads         reboot        smmget    smmset
ssh           swiconfexport telnet    top
```

### Command reference (so far)

| Command | Syntax | Effect / status |
|---|---|---|
| `smmget` | `smmget [-l <location>] [-t <target>] -d <dataitem>` | reads SMM data items; `-d` is required |
| `smmset` | `smmset [-l <location>] [-t <target>] -d <dataitem> -v <value>` | writes SMM data items |
| `swiconfexport` | `swiconfexport swi1\|swi2\|swi3\|swi4` | exports switch config; output destination still TBD (bare invocation says "Invalid parameter") |
| `cmdext` | `cmdext ?` returns "Invalid parameter" — sub-vocab unknown | extended commands; needs reverse-engineering |
| `reads` | `reads <faillog\|secure\|smmlog\|oplog> [-t]` | reads SMM logs |
| `ifconfig` | standard | network info |
| `ssh` / `telnet` | standard | jump-box to internal `172.31.x.x` (iBMCs, switches) |
| `ping` / `ping6` / `ntpq` / `date` / `top` / `history` / `clear` / `exit` / `reboot` | standard | — |

### Known data items

- `smmget -d version` → SMM firmware versions (working)
- `smmget -d sysinfo`, `-d ip`, `-d ver`, `-d cmm`, `-d frame` → "data item does not exist"
- `smmget -l blade1 -d powerstate`, `-d ipinfo`, `-d ip` → "data item does not exist"
- `smmget -l hmm1 -d ip` → "invalid data item or location" (different error class — may indicate `hmm1` is invalid; correct location string TBD)

**Open question:** what is the full list of valid data items? Likely
documented in the Huawei HMM CLI Reference. Tracked as an issue.

## 7. Accounts

Single Redfish-managed account:

```json
{
  "Id": "2",
  "UserName": "root",
  "RoleId": "Administrator",
  "Enabled": true,
  "Oem.Huawei.UserInterfaces": ["WEB","SNMP","SSH/TELNET","SFTP","KVM","REDFISH"],
  "Oem.Huawei.UserDomain": "superdomain"
}
```

`/redfish/v1/AccountService/Accounts/1` returns 404 — only `Accounts/2` exists.

## 8. Implications for each phase

### Backup (Phase 1)

- **HMM config:** `smmget -d <items>` for each known data item + capture of
  `Accounts`, `EthernetInterfaces`, `NetworkProtocol`, `EventService`
  subscriptions over Redfish.
- **Switch config:** `swiconfexport swi2` — destination flag still unknown;
  may be SFTP-pushed or written to a local path. To confirm.
- **iBMC config per blade:** ssh into `172.31.0.x` iBMCs and pull Redfish
  inventory + BIOS settings.
- **What can change unexpectedly:** any firmware update, KVM session, or
  config change on the active HMM may trigger failover to the standby — so
  snapshot must capture both modules.

### Provisioning (Phase 2)

- Redfish `VirtualMedia` is **not on the HMM**; must reach each blade's iBMC.
- Strategy: from the workstation, SSH-tunnel through the HMM to
  `172.31.0.x:443` to reach iBMC Redfish.
- Or: configure routes on the workstation and reach iBMCs directly through
  one of the CX310 uplinks.

### Switch ops (Phase 3)

- CX310 is Huawei VRP-based. CLI access likely via SSH to `172.31.1.x`
  (per-switch IP TBD). Or via `ssh` from inside the HMM (jump-box).
- VLAN ops via VRP commands: `system-view`, `vlan batch <id>`,
  `interface XGigabitEthernet0/0/<n>`, `port link-type access`,
  `port default vlan <id>`, `commit`.

### GUI (Phase 4)

- KVM port (5900) is filtered until a session is allocated by the HMM.
  Reverse-engineering the allocation handshake requires capturing the legacy
  applet's traffic in Wireshark *or* finding a Redfish/SMM action that
  triggers it.
- Fallback: SOL (Serial-over-LAN) via SSH for text console.
