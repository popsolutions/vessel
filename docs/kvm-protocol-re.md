# Reverse engineering of `vconsole.jar` (KVM + VirtualMedia)

Status: **protocol mapped, implementation pending.** Source artifacts in
`re/` (gitignored - Huawei IP, do not redistribute).

## How the legacy applet is embedded

When the user clicks "Console" on a blade in the HMM Web UI, the page emits
an `<embed>` of `application/x-java-applet`. The applet's session-scoped
parameters are passed as embed attributes:

```
code="com.kvm.KVMApplet.class"
codebase="jar"
archive="vconsole.jar"

ipa, ipb              both = HMM external IP (192.168.1.30)
port                  = 2198      (handshake port)
vmmserverport         = 8500      (VM data plane base; per-blade port = 8500 + slot)
shelftype             = "E9000"
local                 = "en"
securekvm             = "1"       (encrypted mode)

verifyvalue           = <decimal int>     (= int.from_bytes(secretkey[0:4], 'big'))
mmverifyvalue         = <decimal int>     (same as above on this firmware)
secretkey             = <40 hex chars>    (20 bytes; bytes [4:20] = AES-128 key)
secretiv              = <32 hex chars>    (16 bytes; AES IV; also reused as salt)
codekey_ext           = <32 hex chars>    (16 bytes; second AES key - VirtualMedia)
verifyvalueext        = <32 hex chars>    (= codekey_ext on this firmware)
typedata              = <decimal int>     (epoch-ish session id)
```

The applet downloads itself from `https://<HMM>/jar/vconsole.jar`
(unauthenticated path). The `.jar` is **generic across blades** - the embed
parameters are what targets a specific blade.

## JAR structure (~1 MB, built 2018-01-12)

```
com/kvm/                          145 .java   - KVM applet logic
com/kvm/decoder/                    8 .java   - KVM video frame decoder
com/huawei/vm/console/communication/  6        - VM protocol layer
com/huawei/vm/console/management/     8        - VM controller + UI
com/huawei/vm/console/process/        4        - SFF-8020i / UFI / USB processors
com/huawei/vm/console/storage/impl/   7        - CDROMDevice, FloppyDevice, etc.
com/library/                          4        - net + logging helpers
de/tu_darmstadt/.../udflib/          58        - UDF (CD/DVD filesystem) library
de/tu_darmstadt/.../sabre/           28        - generic stream library (BSD-licensed)

VMConsoleLib.dll       (Windows x86-32 native)
VMConsoleLib_x64.dll   (Windows x86-64 native)
```

The native DLLs do USB device access **on the operator's Windows host** -
relevant only if you want to expose a real local optical drive as the
virtual media. For server-side ISO/IMG hosting we don't need them.

## Crypto (`com/kvm/AESHandler.java`)

```
algorithm:   AES-128-CBC, NoPadding (length must be multiple of 16)
two modes:
  encry(src, codekey_int, len)      - uses constant key {1..8,1..8} with first
                                      4 bytes overwritten by codekey int;
                                      IV = 16 zero bytes. Used for handshake.
  encry_bytes(src, key, iv, len)    - full caller-supplied key + IV.
                                      Used for the session stream.
```

Key extraction from the embed (parsed by `KVMApplet.init()`):

- `secretkey[0:8]`  (4 bytes, big-endian int)  ->  `verifyvalue` / codekey
- `secretkey[8:40]` (16 bytes)                 ->  AES key
- `secretiv[0:32]`  (16 bytes)                 ->  AES IV  (also stored as salt)
- `codekey_ext`     (16 bytes hex)             ->  AES key for VirtualMedia stream

So the crypto is **fully derivable from a single session's embed values** - no
key-exchange step on top of TLS. The protocol itself is over plain TCP, with
the per-message body AES-encrypted.

## VirtualMedia protocol (`com/huawei/vm/console/communication/ProtocolCode.java`)

### Header

```
PACKET_HEAD_SIZE = 12 bytes
field name positions (per the const offsets):
  CERTIFY_ID_POSITION = 5
  VERTION_POSITION    = 9     (sic - Huawei spelling)
  IPV4_SIZE = 4
  IPV6_SIZE = 16
  IP_TYPE_SIZE = 1
  SECRET_CERTIFYID_SIZE = 24
```

### Op codes (1 byte, position TBD - likely byte 0 of body)

| Op  | Constant                | Direction      | Body                       |
|-----|-------------------------|----------------|----------------------------|
|  0  | `ACK`                   | server -> client | sub-code (see below)     |
|  1  | `CERTIFY_ID`            | client -> server | 24-byte secret certify ID |
|  2  | `DEVICE_TYPE`           | client -> server | 1=Floppy, 2=CDROM, 3=Multi |
|  3  | `UFI_DATA`              | both           | USB-Floppy command/data     |
|  4  | `SFF_DATA`              | both           | SFF-8020i (ATAPI) CDROM CDB |
|  5  | `CLOSE_VM`              | client -> server | 0=Link, 1=Floppy, 2=CDROM |
|  6  | `HEARTBIT`              | both           | (empty)                    |
|  7  | `SHUTDOWN`              | client -> server | (empty)                  |
| -2  | `UFI_COMMAND_COMPLETE`  | server -> client | result                   |
| -1  | `SFF_COMMAND_COMPLETE`  | server -> client | result                   |
| -16 | `CONSOLE_PRINT_CONTROLLER` | server -> client | log line              |
| -4  | `MIC_FILE_CMD`          | both           | virtual mic file           |

### ACK sub-codes

| Sub | Meaning                              |
|-----|--------------------------------------|
|  0  | `ACK_CERTIFY_PASS` (auth ok)         |
|  1  | `ACK_CERTIFY_ID_FAIL`                |
|  2  | `ACK_CERTIFY_VER_NOTSUP`             |
| 16  | `ACK_DEVICE_CREAT` (mount succeeded) |
| 17  | `ACK_DEVICE_FAIL_ENUM`               |
| 33  | `ACK_CLOSE_DEVICE_RM`                |
| 34  | `ACK_CLOSE_UPDATA`                   |
| 35  | `ACK_CLOSE_IPCONFIG`                 |
| 36  | `ACK_MIC_SENT`                       |

### UFI/SFF data sub-types

| Value | Meaning                       |
|-------|-------------------------------|
|  0    | `UFI_SFF_DATA_COMMAND` (CDB)  |
|  1    | `UFI_SFF_DATA_DATA`           |
|  1    | `UFI_SFF_DATA_CONTINUE`       |
|  3    | `UFI_SFF_DATA_END`            |
|  0    | `UFI_SFF_CMD_OK`              |
|  1    | `UFI_SFF_CMD_FAIL`            |

### Storage processors (per the decompiled classes)

- `SFF8020iProcessor.java` (~12 KB) - full ATAPI/SFF-8020i CDROM command set:
  INQUIRY, READ_CAPACITY, READ(10), READ_TOC, MODE_SENSE, TEST_UNIT_READY, etc.
- `UFIProcessor.java` (~11 KB) - USB-Floppy
- `USBProcessor.java` (~5 KB) - USB device emulation top-level
- `CDROMImage.java` - reads from a backing `.iso` file
- `CDROMLocalDir.java` - wraps a local directory and presents it as UDF
  (this is the cool thing: drag-a-folder-as-CD)
- `FloppyImage.java` - same idea for `.img`

## KVM protocol (port 2198 + per-blade data plane)

Less explored so far (priority is VM). High-level from class names:

- `Client.java` opens both **TCP** (`Socket`) and **UDP** (`DatagramSocket`)
  - TCP for keyboard/mouse + control, UDP for video frames.
- `BladeCommu.java` - per-blade TCP wrapper.
- `ClientSocketCommunity.java` - multiplexer for the chassis-wide view (the
  applet can show all 16 blades' thumbnails simultaneously).
- `com.kvm.decoder` - 8 classes; the proprietary frame codec (probably
  Huawei iKVM video, a JPEG-differential variant).
- `KeyboardEvent`, `MouseEvent` etc. for input encoding.

## Connection flow (synthesized from the code)

### VirtualMedia (port `8500 + slot`)

```
1. TCP connect to <HMM>:<8500+slot>  -- DNAT to <iBMC>:8208
2. Build CERTIFY_ID frame:
     header (12 bytes, fields per ProtocolCode constants)
     body  = AES_CBC_128(secret_certify_id_bytes, codekey_ext, secretiv)
   Send.
3. Read ACK frame:
     if sub == ACK_CERTIFY_PASS: continue
     else: bail.
4. Send DEVICE_TYPE = 2 (CDROM).
5. Read ACK_DEVICE_CREAT (sub=16).
6. Loop:
     read SFF_DATA from server (with sub=UFI_SFF_DATA_COMMAND, body=CDB)
     parse SCSI CDB (INQUIRY / READ_CAPACITY / READ(10) / etc.)
     respond SFF_DATA with sub=UFI_SFF_DATA_DATA carrying the result
     end with SFF_COMMAND_COMPLETE
7. Send HEARTBIT every <HEARTBIT_INTERVAL> ms.
8. To eject: send CLOSE_VM with body=2 (CDROM).
9. To shutdown: send SHUTDOWN.
```

Each frame body is AES-encrypted in `securekvm=1` mode.

### KVM (port 2198 handshake -> ?)

To be detailed once VirtualMedia client is running and we can capture parallel
behaviour. Likely:

- TCP handshake on 2198 negotiates a session; receives a token.
- UDP video frames flow on a port returned in the handshake.
- Keyboard/mouse on a TCP control port.

## Python implementation plan

**Module layout** (will land under `src/hmm_client/kvm/` and
`src/hmm_client/vmedia/`):

```
src/hmm_client/
  kvm/
    __init__.py
    crypto.py          # AESHandler equivalent (cryptography.Cipher.AES + CBC)
    framing.py         # 12-byte header pack/unpack
    client.py          # TCP/UDP transport
    decoder.py         # video frame decoder (port from com.kvm.decoder)
  vmedia/
    __init__.py
    proto.py           # op codes, ACK codes, sub-codes (ProtocolCode.java port)
    client.py          # connect, certify, device-create, command loop
    sff8020i.py        # ATAPI/SFF-8020i command parser & responder
    ufi.py             # USB Floppy
    cdrom_iso.py       # backing for an .iso file
    cdrom_dir.py       # backing for a directory (UDF) - uses pycdlib / pyudf
```

**Frontend (Phase 4):** noVNC for KVM, simple file picker for VM. Wired
into the FastAPI GUI (issue #14).

**Estimated effort:** ~3-4 days for VirtualMedia (the deliverable that
replaces #19 PXE), +2-3 days for KVM video. Can ship VM first standalone.

## Outstanding questions

1. Exact byte layout of the 12-byte header (field offsets between the
   constants `CERTIFY_ID_POSITION=5` and `VERTION_POSITION=9` only partially
   specify it). Will be answered by reading `PackData.java` /
   `UnPackData.java`.
2. Endianness of multi-byte fields (the `intToByte` helper in
   `ProtocolCode` is **big-endian**; assume the same throughout).
3. Whether the heartbeat carries any payload other than the op code.
4. How `verifyvalueext` / `codekey_ext` differ from `verifyvalue` /
   `secretkey` - likely VirtualMedia uses ext, KVM uses non-ext, but verify
   by reading `Base.java` and the call sites.
5. KVM video codec details - reserved for after VM is working.

## Legal / IP note

`re/vconsole.jar` and the decompiled Java sources are Huawei intellectual
property and are gitignored. **Do not commit, redistribute, or publish** the
JAR or its decompilation. The protocol-level documentation in this file is a
clean-room description and safe to share - it documents *behaviour*, not
copyrighted *implementation*.

The Python re-implementation should be written from this document, not by
porting the Java line-for-line, to keep it cleanly licensable (e.g. MIT) for
the open-source community of E9000 owners stuck with the legacy applet.
