<div align="center">

# Vessel

### A free-software operator's helm for the Huawei E9000 chassis

**The first open, browser-native KVM for the E9000 family.**
No Java applet. No Windows VM. No Palemoon. Just a browser.

Eleven years after the chassis shipped with a `vconsole.jar` Java 1.6
applet that only runs on Palemoon-on-Windows, the entire iKVM stack —
video decode, keyboard, mouse, virtual media — is reverse-engineered
and reimplemented in modern Python + HTML/JS.

[![Sponsor this work](https://img.shields.io/badge/💛%20Sponsor%20this%20work-Stripe-635bff?style=for-the-badge)](https://buy.stripe.com/bJe14ne471GheX3bE4dEs00)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=for-the-badge)](https://www.python.org/)

</div>

---

## Why "Vessel"

A vessel is what carries you across an ocean you can't cross on foot.
It's also what holds something precious — water, blood, code — without
leaking.

A blade chassis is both: it carries dozens of compute payloads through
years of production, and it holds operator trust that the controls
will respond when you reach for them. When the helm of that vessel is
welded shut by a 2014-vintage Java applet that no modern browser will
load, the operator becomes a passenger. **Vessel** is what you build
when you refuse that.

## What this is

A complete Linux-native operations toolkit for the **Huawei E9000 blade
chassis** family — CH121 / CH222 compute blades, CX310 switch modules,
SMM (HMM) management modules. Replaces the proprietary Java applet
(`vconsole.jar`, last touched 2018-01) with code you can read, audit,
extend, and run anywhere.

```
┌──────────────────┐                  ┌─────────────┐
│  Your browser    │  ◀── WebSocket ──┤   FastAPI   │
│  (any modern     │                  │  GUI server │
│   Chrome, Brave, │  ── HID input ──▶│  (Python)   │
│   Firefox, …)    │                  │             │
└──────────────────┘                  └──────┬──────┘
                                             │ TCP (custom Huawei
                                             │  iKVM + VirtualMedia)
                                      ┌──────▼──────┐
                                      │ E9000 HMM   │
                                      │ 192.168.1.30│
                                      │ ┌─────────┐ │
                                      │ │ iBMC ×16│ │
                                      │ └─────────┘ │
                                      └─────────────┘
```

## What works today

| Capability | Status | Notes |
|---|---|---|
| **Live KVM video** | ✅ working | OldRLE codec ported from Java, byte-exact |
| **Keyboard input** | ✅ working | Full HID 8-byte report, AES-encrypted |
| **Mouse input** | ✅ working | Absolute coords scaled to chassis 0..3000 |
| **Virtual Media (mount ISO)** | ✅ working | Booted Proxmox 9.1 from a hosted ISO |
| **Power control** | ✅ working | on / off / cycle / reset / nmi per blade |
| **Boot device override** | ✅ working | one-shot or persistent |
| **Snapshot + drift detection** | ✅ working | backup HMM + switch configs, diff vs live |
| **Inventory (via Redfish)** | ✅ working | populates the home page on connect |
| **Server-side ISO browser** | ✅ working | pick `.iso`/`.img` without typing a path |
| **FastAPI web GUI** | ✅ working | `/`, `/kvm/{slot}` |
| **Python CLI** | ✅ working | `hmm list / power / boot / snapshot / drift / vmedia mount` |
| Switch (CX310) VLAN ops | 🟡 next | direct VRP CLI access in progress |
| GUI in applet visual style | 🟡 next | layout tracking the original 32-blade tab strip |
| NewRLE/JPEG codec | 🟡 next | high-resolution graphics modes |

## Quick start

```bash
# 1. Clone
git clone https://git.pop.coop/noc/huaweie9000.git
cd huaweie9000

# 2. Install (Python 3.11+)
uv venv .venv && . .venv/bin/activate
uv pip install -e .

# 3. Configure your chassis
cp .env.example .env
$EDITOR .env       # set HMM_HOST, HMM_USER, HMM_PASSWORD

# 4. Verify CLI works
hmm list

# 5. Open the web UI
hmm gui            # opens http://127.0.0.1:8765
```

That's it. Click any blade, hit `KVM` — full live console in your browser.

## The reverse-engineering story

Huawei shipped the E9000 in 2014. The KVM applet (`vconsole.jar`) was
last touched in **2018-01-12**, requires Java 1.6, embeds two Windows
DLLs for native USB access, and the only browsers that still load
unsigned applets are Palemoon and a few abandoned IE forks. By 2025
that's a death sentence — yet thousands of E9000s are still in
production (telcos, hospitals, universities), and operators are
locked out of their own hardware on a yearly basis.

This project is what came out of trying to refuse that.

### The protocol

The applet talks **two parallel TCP streams** to the HMM:

- `:2198` — handshake + per-frame heartbeats (the "SMM" channel)
- `:2200` — actual video frames (the "blade data plane")
- `:8500 + slot` — a third TCP per-blade channel for VirtualMedia
  (DNAT'd internally to `:8208` on the iBMC)

Every frame is `[FE F6 hi lo][sessionID 4 or 24 B][CRC16-BE 2 B][op 1 B][payload]`.
The session is established via PBKDF2(verifyvalue, secretiv, SHA-1, 5000)
→ 72 bytes split into a 24-byte sessionID and three AES-128 keys
(kvm / kbd / vmm).

Keyboard and mouse payloads are **AES-128-CBC NoPadding** encrypted
with the codekey-derived key (Windows path) or the kbd_secret_key
(Linux path), with a zero IV. Picking the wrong path got us hours
of "typing 'p' produces 'Y'" debugging.

The video codec is a custom RLE called **OldRLE** that the Java code
calls `unZipData` (despite involving no zlib). Each row's byte stream
is preceded by a leading-zero marker that Java's `combine()` builds
in explicitly:

```java
data = byte[packLength + 1];
data[0] = 0;
int index = 1;          // copies start at data[1]
```

Omitting that one zero byte caused a ~260-pixel horizontal offset
that we chased for several days before finding it in the decompiled
source. Each subsequent run was 1 byte off; over ~85 chunks per
keyframe that compounded to a quarter-screen shift.

Pixels are 8-bit `BBGGGRRR` (BGR233) packed into a 1-byte palette.
Diff frames are XOR-deltas in BGR233 space — apply per-byte XOR
against the previous frame to get the new screen.

### The validation method

The single trick that turned weeks of pixel-hunting into hours of
fixing: capture **the actual Java applet's wire traffic** with
tcpdump while it runs against a live blade, then write a Python
test that decodes the same bytes and asserts byte-for-byte parity.

```bash
sudo tcpdump -i any 'host 192.168.1.30 and tcp portrange 2198-2300' \
    -w palemoon.pcap -s 0
```

Each fix went: capture pcap → run our decoder offline → diff against
Palemoon's display → identify the discrepancy → patch → re-run. The
"is our keyboard encryption right" question reduced to a
single-line assertion:

```python
assert keyboard_pack(blade=1, hid=zeros, codekey=0x4dce0605,
                     encrypted=False, is_new=True) \
    == bytes.fromhex(
        "fef600144dce060500000301a71c6f608cec568870eff0e4d651d791")
```

When that passed, every subsequent typed character landed correctly
on the blade.

## Hardware tested

| Component | Model | Slots | Status |
|---|---|---|---|
| Compute blades | **CH121** (half-width, dual Xeon, 8 DIMM) | 1, 2, 3, 4, 8, 9, 10, 11, 12, 16 | 10× ✅ |
| Storage blades | **CH222** (full-width, 15× HDD) | 13, 14, 15 | 3× ✅ |
| Switch modules | **CX310** (10GE) | Swi2, Swi3 | 2× ✅ |
| Management | **SMM** v7.63 (IPMI built 2021-08-23) | HMM1, HMM2 | active/standby ✅ |

Tested against a production chassis. If you have other E9000-family
hardware — `MX510`, `CX317`, `CX610`, `CH140`, `CH225` — issues +
PRs welcome. The protocol is mostly chassis-uniform.

## Service surface (per HMM)

| Protocol | Port | Status | Notes |
|---|---|---|---|
| HTTPS / Redfish | 443 | open | DMTF Redfish 1.0.2 — chassis-level |
| SSH dispatcher | 22 | open | restricted vocabulary (`smmget`, `smmset`, `swiconfexport`, …) |
| HTTP | 80 | open | login UI |
| iKVM data plane | 2198, 2200 | open | the custom Huawei protocol this project speaks |
| VirtualMedia | 8500 + slot | open | per-blade ATAPI/SCSI tunnel |
| IPMI | 623 | filtered | likely chassis-internal only |
| SNMP | 161 | closed | — |

## Layout

```
src/hmm_client/
  cli.py             Typer CLI: `hmm list / power / boot / snapshot …`
  config.py          Settings, env loader
  ops.py             power / boot / inventory ops (Redfish + SSH-jump)
  redfish.py         DMTF Redfish 1.0.2 client
  discover.py        read-only Redfish walker
  snapshot.py        HMM + switch config backup
  restore.py         drift detection (read-only)

  vmedia/            VirtualMedia (mount ISO over the wire)
    login.py         HMM Web auth + per-session embed extraction
    crypto.py        AES + key parsers
    proto.py         12-byte VM frame primitives
    client.py        VM data-plane TCP client
    sff8020i.py      ATAPI/SCSI command responder
    cdrom_iso.py     ISO file backend

  kvm/               OldRLE-only KVM (legacy, still in service)
    client.py        per-blade TCP, frame reassembly, XOR diff
    transport.py     wire framing
    codec_old.py     OldRLE decoder + BGR233 palette

  kvm_core/          Faithful clean-room port of vconsole.jar
    aes.py           ← AESHandler.java
    base.py          ← Base.java
    key_map.py       ← Linux scancode + Web HID mapping
    pack.py          ← PackData.java (every outgoing op)
    decoder/         ← com.kvm.decoder.* (color converter shipped,
                       NewRLE/JPEG in progress)

  gui/               FastAPI + HTMX web GUI
    app.py           routes + WebSocket KVM bridge
    templates/       Jinja2 (index.html, kvm.html, _grid.html)

docs/
  kvm-protocol-re.md   the protocol RE notes (ground truth doc)
  discovery.md         CLI vocabulary discovered via Tab-Tab
  topology.md          chassis network layout
  roadmap.md           phased plan
```

## Safety stance

- **Backup-first.** Any change-making operation is gated by a verified
  snapshot of the affected scope. No `set` without a prior `get` to disk.
- **Watchdog on switch changes.** Auto-rollback on mgmt-loss before
  any commit becomes permanent.
- **No credentials in code.** `.env` only, gitignored. `.env.example`
  documents the required schema.
- **No live writes from the dev branch.** Production access is gated
  through `main`.

## Roadmap

Detailed in [`docs/roadmap.md`](docs/roadmap.md). Five phases:

1. **Discovery & Backup** ✅ shipped — `hmm snapshot / drift`
2. **Boot orchestration** ✅ shipped — `hmm vmedia mount`, KVM
3. **Switch ops & VLAN** 🟡 in progress — direct CX310 access
4. **Applet-style GUI polish** 🟡 next — 32-blade tab strip layout
5. **NewRLE/JPEG codec** ⏭ — high-resolution graphics modes

Open issues + milestones live on
[`git.pop.coop/noc/huaweie9000`](https://git.pop.coop/noc/huaweie9000/issues).

## Contributing

Issues, PRs, and protocol-RE notes welcome. The applet decompilation
itself (`re/vconsole.jar` and the decompiled tree) is **gitignored**
because it's Huawei intellectual property — but the protocol-level
documentation in `docs/kvm-protocol-re.md` is a clean-room
reproduction of *behaviour*, not implementation, and is safe to share.

If you have a chassis variant we don't cover (different SMM firmware,
different blade family), pcaps of the working Java applet against
your hardware are the single most useful contribution. See the
"validation method" section above.

## Why free software, here, now

The four freedoms — to **run**, to **study**, to **modify**, to
**share** — were written for exactly this case: hardware you bought
and own, controlled by software whose vendor stopped distributing
the runtime needed to launch it. When that happens, "the manual
says click Console" becomes a sealed door. The chassis is still
working. The operator is still trying to operate it. The only
thing missing is a piece of code that should be public infrastructure.

Vessel is released under the **MIT license** — short, permissive,
and explicit. You can run it for any purpose, on any hardware you
own, in any context, including commercial. You can read every line
of the protocol implementation. You can change anything that doesn't
work for your environment. You can ship the result back to the
community, fork it, embed it in something larger, sell support around
it. The only thing you can't do is pretend you wrote it from scratch
when you didn't.

The reverse-engineering documentation in `docs/kvm-protocol-re.md`
is a **clean-room behavioural description** of how the chassis
speaks on the wire. It is not a copy of vendor source code; it
documents observable bytes, just as a clean-room driver
re-implementation always has — from the early Linux drivers for
proprietary modems through the CUPS printer drivers through the
libreoffice OOXML parsers. This is the standard mode of bringing
old hardware into a new decade. It is legal, ethical, and the only
way thousands of these chassis stay out of e-waste streams in 2026.

If you operate one of these and Vessel saves you a Windows-XP VM,
the most useful thing you can do — beyond using it — is **send a
pcap of your chassis variant**. The protocol is mostly uniform but
the firmware revisions diverge. Every pcap turns one operator's
problem into everyone's solution.

## Sponsoring

This work was bootstrapped solo to unstick a real production
chassis. If it unsticks yours and you'd like to keep the work
moving — switch CLI, NewRLE codec, applet-style GUI polish — a
sponsorship makes a real difference.

<div align="center">

[![Sponsor via Stripe](https://img.shields.io/badge/💛%20Sponsor%20via%20Stripe-buy.stripe.com-635bff?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/bJe14ne471GheX3bE4dEs00)

</div>

Every contribution funds protocol RE on hardware we don't have access
to (other E9000 firmware revisions, MX510 chassis, CH140 blades),
plus continued maintenance.

## License

MIT — see [`LICENSE`](LICENSE). Use it, fork it, ship it. The only
thing you can't redistribute is `re/vconsole.jar` itself, which is
Huawei IP and is gitignored.

## Acknowledgements

- The original `vconsole.jar` engineers — your code became readable
  enough through `jadx` to clean-room reproduce. Thank you for not
  obfuscating it.
- Every operator who left an old E9000 PXE-boot flow in a forum thread
  somewhere. The chassis-internal `172.31.x.0/24` topology came out
  of a 2017 Spiceworks post that no one had any business remembering.
- Pillow + cryptography + FastAPI — Python on hard mode would not
  have been possible without you.

---

<div align="center">
<sub>If this saved you from spinning up another Windows-XP VM in 2026 —
<a href="https://buy.stripe.com/bJe14ne471GheX3bE4dEs00">consider sponsoring</a>.
The next operator running a 2014-vintage E9000 with no in-warranty support
will thank you too.</sub>
</div>
