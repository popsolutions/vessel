# HMM SSH CLI - vocabulary reference

The SMM's SSH login is a Wind River Linux 4.2 shell with a custom command
dispatcher: `/smm/smmcli`. Most Unix commands return `unknown command`. This
file documents what is reachable.

> Probed 2026-04-30 against firmware **v7.63** (IPMI module built 2021-08-23).
> Vocabulary may differ on other firmware revisions - re-verify before
> relying on it.

## How to discover

1. SSH in as `root`.
2. Press **Tab Tab** at the empty prompt to dump the base vocabulary.
3. To unlock diagnostic commands, run `cmdext -l`, enter the password again
   when prompted, then Tab Tab again. Run `cmdext -u` to unload.

## Base vocabulary (always available)

| Command | Form | What it does |
|---|---|---|
| `smmget` | `smmget [-l <loc>] [-t <target>] -d <dataitem>` | Read SMM/blade/switch data items. **`-d` is required.** |
| `smmset` | `smmset [-l <loc>] [-t <target>] -d <dataitem> -v <value>` | Write data items. |
| `swiconfexport` | `swiconfexport swi<N>` (only inside `cmdext -l`) | Export switch config to `/tmp/exchange/swi<N>/swi<N>.tar.gz`. |
| `cmdext` | `cmdext -l` / `cmdext -u` | Load / unload extended diagnostic commands (re-prompts for password). |
| `reads` | `reads <faillog\|secure\|smmlog\|oplog> [-t]` | Read SMM logs. `-t` reads the tail. |
| `ifconfig` | standard busybox | NIC info. |
| `ssh` | `ssh <host> [NoStricHostKeyChecking]` | **Custom syntax** - only positional args. |
| `telnet` | standard | Telnet client. |
| `ping` / `ping6` | busybox | ICMP. |
| `ntpq` | standard | NTP query. |
| `date` / `top` / `history` / `clear` / `exit` / `reboot` | standard | - |
| `TMOUT` | env-style | Idle-timeout knob. |

## Extended vocabulary (after `cmdext -l <password>`)

13 commands loaded (Linux/busybox diagnostics):
`cpuinfo`, `meminfo`, `netstat`, `iptabledis`, `export`, `df`, `ps`,
`fpgainfo`, `cpldinfo`, `routedis`, `arp`, `dmsg`, `route6`.

These are mostly diagnostic - none of them is a richer HMM CLI. The real HMM
ops surface stays `smmget` / `smmset` / `swiconfexport` / `reads`.

## `smmget` data items confirmed working

Probed against this chassis. Nothing here is an exhaustive Huawei reference -
expect more items than are listed.

| dataitem | Requires | Returns |
|---|---|---|
| `version` | nothing | SMM Uboot/CPLD/PCB/FPGA/Software versions + IPMI build date. |
| `version` | `-l swi<N>` | Switch IPMC/IPMI/iMana/Driver/Uboot versions + IPMB address. |
| `gateway` | nothing | `Configuration gateway: <ip>` + `Active gateway: <ip>`. |
| `timezone` | nothing | `Time Zone: GMT+0`. |
| `presence` | nothing | `SMM is present.` |
| `presence` | `-l blade<N>` | `Blade<N> is present.` |
| `presence` | `-l swi<N>` | `Swi<N> is present.` |
| `health` | `-l blade<N>` | `blade<N> has no problem.` (or alarm summary) |
| `health` | `-l swi<N>` | `swi<N> has no problem.` |
| `biosbootmode` | `-l blade<N>` | `enable` / `disable`. |
| `macaddress` | `-l blade<N>` | MAC if known, else `NO MAC Address!`. |
| `powerstate` | `-l blade<N>` or `-l swi<N>` | (likely on/off - re-verify with a real call). |

## `smmget` error semantics

- *"The data item does not exist."* - the dataitem string is wrong.
- *"Target is null,please input target."* - dataitem requires `-t`.
- *"The target does not exist, or the target does not support the dataitem."*
  - `-t` value is wrong (target names look like sensor labels, e.g.
  `BaseboardTemp` per the help).
- *"smmget:cli invalid data item or location error"* - `-l` value is wrong.
  Valid `-l` values include `blade1`..`blade32`, `swi1`..`swi4`. **`hmm1`,
  `hmm2` are NOT valid `-l` values** (returns this error).
- *"Please Operate: blade1 ~ blade32 swi1 ~ swi4"* - dataitem is valid but
  needs `-l`.

## `swiconfexport` - switch backup

```
ssh root@192.168.1.30
> cmdext -l
> <password>
> swiconfexport swi2
Successed
Please check "/tmp/exchange/swi2/swi2.tar.gz"
```

The tar.gz contains:
```
swi<N>/2_1_1.cfg          # current running config (binary, encoded)
swi<N>/entity_0.cfg
swi<N>/entity_1.cfg       # contains the FRU code (e.g. "65794")
swi<N>/entity_2.cfg
swi<N>/swcfg/             # (empty in observed dump)
swi<N>/bakcfg/<YYYY-MM-DD>/2_1_1.cfg    # historical local backups
```

The `.cfg` files are Huawei-proprietary binary format. They round-trip via
the companion `swiconfimport` (TBD - verify in the restore implementation).

Retrieve via SFTP - the SMM has SFTP enabled for the same `root` account.

## `reads` - SMM logs

```
reads faillog       # security/audit fails
reads secure        # security log
reads smmlog        # SMM operational log
reads oplog         # operation log
reads <log> -t      # tail
```

## What's NOT available

- No `ipmcget`/`ipmctool`/`ipmitool`.
- No `displayblade`, `displayfru`, `displayalarm`, `displayswitch`,
  `displayversion` style commands (those are Huawei OceanStor / VRP idioms,
  not HMM).
- No `save`/`backup`/`exportconfig` global command. Use `swiconfexport` per
  switch and Redfish/SFTP per scope.
- No `bash`, `sh`, `cat`, `ls`, `grep`, `cd`. The shell is `/smm/smmcli` and
  enforces the dispatcher.

## Routing table (for context)

```
192.168.1.0/24   eth0    (mgmt)
172.31.0.0/24   eth1    (HMM internal, only HMM-to-HMM)
172.31.1.0/24   eth2    (chassis fabric: blades + switches mgmt)
default ->      192.168.1.1 via eth0
```

`AllowTcpForwarding` is **disabled** - `ssh -L` cannot reach `172.31.1.x`
from the workstation through the HMM. See `docs/topology.md` for fallbacks.
