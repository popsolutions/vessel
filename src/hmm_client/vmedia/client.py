"""VirtualMedia TCP client.

Synthesized from re/decompiled/.../com/huawei/vm/console/communication/
ProtocolProcessor.{connectPak,devicesPak,heartBitPak,disconnectPak,vmLinkClosePak}
and com/huawei/vm/console/management/VMConsole.{creatVMLink,sentCertifyCode}.

Per the Java code, the VirtualMedia data plane (TCP to host:vmm_base+slot)
speaks a 12-byte-header protocol. The first frame is CERTIFY_ID (op=1):

    byte 0     = 1   (CERTIFY_ID)
    bytes 4-7  = body length (BE int32) - 29 if 24-byte sessionid + IPv4
    bytes 8-11 = version major.minor.patch.build (4 bytes, default 2.1.0.0)
    bytes 12-35 = 24-byte sessionid
    byte 36    = 0 (IPv4) or 1 (IPv6)
    bytes 37-40 = local IPv4 (4 bytes)

Then DEVICE_TYPE (op=2, byte[1]=device & 0xF, no body).
Server responds with ACK frames; on ACK_DEVICE_CREAT the SCSI/ATAPI command
loop begins (SFF_DATA frames carrying CDB -> respond with data).
"""
from __future__ import annotations

import contextlib
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console

from ..config import Settings
from . import sff8020i
from .cdrom_iso import IsoFileBacking
from .kvm_stream import (
    _byte_swap_4byte_chunks,
    derive_vmedia_session_keys,
    pack_req_vmm_codekey,
    parse_vmm_codekey_report,
)
from .login import Session, login
from .proto import (
    FRAME_HEAD_SIZE,
    AckCode,
    DeviceType,
    Header,
    OpCode,
    pack_header,
    parse_header,
)

console = Console()

DEFAULT_VERSION = (3, 1, 1, 1)  # matches Palemoon's captured CERTIFY_ID byte-for-byte
CONNECT_TIMEOUT = 20.0
RECV_TIMEOUT = 10.0


@dataclass
class ProbeResult:
    """One round-trip result from the data-plane probe."""

    connected: bool
    sent_certify_id_bytes: bytes
    server_response_header: Header | None
    server_response_body: bytes
    error: str | None


def _pack_certify_id(sessionid: bytes, local_ip: bytes,
                     version: tuple[int, int, int, int] = DEFAULT_VERSION) -> bytes:
    """Build the 41-byte CERTIFY_ID frame for a 24-byte sessionid + IPv4."""
    if len(sessionid) != 24:
        raise ValueError(f"sessionid must be 24 bytes, got {len(sessionid)}")
    if len(local_ip) != 4:
        raise ValueError(f"local_ip must be 4 bytes (IPv4), got {len(local_ip)}")

    body_len = 24 + 1 + 4  # sessionid + ip-type + ipv4
    trans = body_len.to_bytes(4, "big") + bytes(version)  # bytes 4-11
    head = pack_header(OpCode.CERTIFY_ID, trans_field=trans)
    body = sessionid + bytes([0]) + local_ip  # ip-type=0 = IPv4
    return head + body


def _pack_device_type(device: DeviceType) -> bytes:
    """12-byte DEVICE_TYPE frame: byte[0]=2, byte[1]=device & 0xF."""
    return pack_header(OpCode.DEVICE_TYPE, flags=int(device) & 0x0F)


def _pack_heartbeat() -> bytes:
    return pack_header(OpCode.HEARTBIT)


def _pack_close_vm(device: DeviceType, reason: int = 0) -> bytes:
    return pack_header(OpCode.CLOSE_VM, flags=int(device) & 0x03, ack_or_sub=reason)


def _pack_shutdown(reason: int = 0) -> bytes:
    return pack_header(OpCode.SHUTDOWN, ack_or_sub=reason)


def _build_sessionid_simple(sess: Session) -> bytes:
    """The simplest 24-byte sessionid candidate: verifyvalueext (16 B) + aes_iv[:8].

    The negotiated path uses PBKDF2-HMAC-SHA1 with codekey_ext / secretiv but
    requires a prior REQ_VMM_CODEKEY round-trip on the KVM channel. The simple
    path tries the embed values directly; if the server rejects with
    ACK_CERTIFY_ID_FAIL we'll iterate.
    """
    return sess.verifyvalueext + sess.aes_iv[:8]


def _recv_exact(sock: socket.socket, n: int, timeout: float = RECV_TIMEOUT) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"server closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def probe_certify_id(sess: Session, slot: int,
                     sessionid: bytes | None = None) -> ProbeResult:
    """Open the data-plane socket, send CERTIFY_ID, capture server response.

    Returns the raw server frame so callers (or interactive RE) can decode it.
    """
    if sessionid is None:
        sessionid = _build_sessionid_simple(sess)

    port = sess.vmedia_port(slot)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((sess.host, port))
        local_ip = socket.inet_aton(s.getsockname()[0])
        frame = _pack_certify_id(sessionid, local_ip)
        s.sendall(frame)

        header_bytes = _recv_exact(s, FRAME_HEAD_SIZE, timeout=RECV_TIMEOUT)
        header = parse_header(header_bytes)
        body = b""
        if header.op == OpCode.ACK:
            body_len = 0
        else:
            body_len = int.from_bytes(header.trans_field[:4], "big")
        if body_len > 0:
            body = _recv_exact(s, body_len, timeout=RECV_TIMEOUT)

        return ProbeResult(
            connected=True,
            sent_certify_id_bytes=frame,
            server_response_header=header,
            server_response_body=body,
            error=None,
        )
    except Exception as e:
        return ProbeResult(
            connected=True,
            sent_certify_id_bytes=b"",
            server_response_header=None,
            server_response_body=b"",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        with contextlib.suppress(Exception):
            s.shutdown(socket.SHUT_RDWR)
        s.close()


@dataclass
class Connection:
    """Live VM connection with helpers to send/recv frames."""

    sess: Session
    slot: int
    sock: socket.socket

    def send_frame(self, frame: bytes) -> None:
        self.sock.sendall(frame)

    def recv_frame(self, timeout: float = RECV_TIMEOUT) -> tuple[Header, bytes]:
        header_bytes = _recv_exact(self.sock, FRAME_HEAD_SIZE, timeout=timeout)
        header = parse_header(header_bytes)
        body_len = 0 if header.op == OpCode.ACK else int.from_bytes(
            header.trans_field[:4], "big"
        )
        body = _recv_exact(self.sock, body_len, timeout=timeout) if body_len else b""
        return header, body

    def heartbeat(self) -> None:
        self.send_frame(_pack_heartbeat())

    def close(self, device: DeviceType = DeviceType.CDROM) -> None:
        with contextlib.suppress(Exception):
            self.send_frame(_pack_close_vm(device))
            self.send_frame(_pack_shutdown())
            time.sleep(0.2)
        with contextlib.suppress(Exception):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def open_vmedia(sess: Session, slot: int,
                device: DeviceType = DeviceType.CDROM,
                sessionid: bytes | None = None) -> Connection:
    """Open + authenticate + declare device. Caller owns the returned Connection."""
    if sessionid is None:
        sessionid = _build_sessionid_simple(sess)

    port = sess.vmedia_port(slot)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect((sess.host, port))
    local_ip = socket.inet_aton(s.getsockname()[0])

    s.sendall(_pack_certify_id(sessionid, local_ip))
    header_bytes = _recv_exact(s, FRAME_HEAD_SIZE, timeout=RECV_TIMEOUT)
    header = parse_header(header_bytes)
    if header.op != OpCode.ACK or header.ack_or_sub != AckCode.CERTIFY_PASS:
        s.close()
        raise RuntimeError(
            f"CERTIFY_ID rejected: op={header.op} sub={header.ack_or_sub}"
        )

    s.sendall(_pack_device_type(device))
    header_bytes = _recv_exact(s, FRAME_HEAD_SIZE, timeout=RECV_TIMEOUT)
    header = parse_header(header_bytes)
    if header.op != OpCode.ACK or header.ack_or_sub != AckCode.DEVICE_CREAT:
        s.close()
        raise RuntimeError(
            f"DEVICE_TYPE rejected: op={header.op} sub={header.ack_or_sub}"
        )

    return Connection(sess=sess, slot=slot, sock=s)


# ---------------------------------------------------------------------------
# Per-blade KVM stream — codekey negotiation
# ---------------------------------------------------------------------------

PER_BLADE_KVM_PORT = 2200  # for blade 1 per BLADE_STATE; if other blades differ,
                           # we'll need to query BLADE_STATE per-slot (issue #25 fix)


def negotiate_codekey(sess: Session, slot: int,
                      kvm_port: int = PER_BLADE_KVM_PORT) -> dict[str, bytes]:
    """Phase 1: open per-blade KVM stream, send REQ_VMM_CODEKEY, derive VM keys.

    Returns the dict from `derive_vmedia_session_keys`:
      {sessionid (24B), secret_key (16B), secret_iv (16B)}
    """
    sid4 = _byte_swap_4byte_chunks(sess.verifyvalue.to_bytes(4, "big"))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((sess.host, kvm_port))
        s.sendall(pack_req_vmm_codekey(blade_no=slot, sessionid=sid4, secure=False))
        time.sleep(0.5)
        s.settimeout(RECV_TIMEOUT)
        raw = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if len(raw) >= 256:
                    break
        except socket.timeout:
            pass
    finally:
        with contextlib.suppress(Exception):
            s.shutdown(socket.SHUT_RDWR)
        s.close()

    if len(raw) < 7 or raw[:2] != b"\xfe\xf6":
        raise RuntimeError(f"REQ_VMM_CODEKEY: no valid response ({len(raw)} bytes)")
    blen = (raw[2] << 8) | raw[3]
    body = raw[4:4 + blen]
    op = body[2]
    if op != 0x32:
        raise RuntimeError(f"expected VMM_CODEENCRYPT_REPORT (op=0x32), got 0x{op:02x}")
    nego_codekey, nego_salt = parse_vmm_codekey_report(body[3:])
    return derive_vmedia_session_keys(nego_codekey, nego_salt)


# ---------------------------------------------------------------------------
# SCSI loop — handle iBMC's SFF_DATA commands
# ---------------------------------------------------------------------------

SFF_FLAGS_END_DATA = 0x31     # response: END (3) + DATA (1)
SFF_STATUS_OK = 0
SFF_STATUS_FAIL = 1


def _send_sff_data(conn: Connection, body: bytes, seq_id: int) -> None:
    """Send an SFF_DATA response frame (body length encoded in trans field BE32)."""
    trans = len(body).to_bytes(4, "big") + b"\x00" * 4
    head = pack_header(OpCode.SFF_DATA, flags=SFF_FLAGS_END_DATA,
                       seq_id=seq_id, trans_field=trans)
    conn.send_frame(head + body)


def _send_sff_complete(conn: Connection, seq_id: int, status: int = SFF_STATUS_OK) -> None:
    """Send the SFF_COMMAND_COMPLETE frame (op 0xFF) closing a CDB transaction."""
    conn.send_frame(pack_header(OpCode.SFF_COMMAND_COMPLETE,
                                ack_or_sub=status, seq_id=seq_id))


def run_session(conn: Connection, backing: sff8020i.IsoBacking,
                on_idle: Callable[[], bool] | None = None) -> None:
    """SCSI loop: read CDBs, build responses, until server closes or `on_idle` says stop.

    `on_idle()` is called when no frame arrives within RECV_TIMEOUT; return True to stop.
    """
    cdrom_block = sff8020i.CDROM_BLOCK_SIZE
    console.print(f"[bold green]vmedia session live[/] — backing {backing.lba_count} LBAs "
                  f"({backing.lba_count * cdrom_block / 1024 / 1024:.1f} MiB)")

    while True:
        try:
            hdr, body = conn.recv_frame(timeout=RECV_TIMEOUT)
        except (socket.timeout, TimeoutError):
            if on_idle and on_idle():
                break
            with contextlib.suppress(Exception):
                conn.heartbeat()
            continue
        except (ConnectionError, OSError) as e:
            console.print(f"[red]connection: {e}[/]")
            break

        if hdr.op in (OpCode.SHUTDOWN, OpCode.CLOSE_VM):
            console.print(f"[yellow]server requested {OpCode(hdr.op).name}[/]")
            break

        if hdr.op == OpCode.HEARTBIT:
            conn.heartbeat()
            continue

        if hdr.op == OpCode.ACK:
            continue  # mid-transaction acks; ignore for now

        if hdr.op != OpCode.SFF_DATA:
            console.print(f"[yellow]unexpected op 0x{hdr.op:02x} ({len(body)} B body)[/]")
            continue

        # SFF_DATA from server: flags lower-nibble 0 = COMMAND (CDB)
        if (hdr.flags & 0x0F) != 0:
            continue  # data continuation we don't expect

        cdb = body
        scsi_op, fields = sff8020i.parse_cdb(cdb)
        seq = hdr.seq_id

        try:
            resp: bytes = b""
            if scsi_op == sff8020i.INQUIRY:
                resp = sff8020i.make_inquiry_response(alloc_len=fields.get("alloc_len", 36) or 36)
            elif scsi_op == sff8020i.READ_CAPACITY:
                resp = sff8020i.make_read_capacity_response(backing.lba_count)
            elif scsi_op == sff8020i.READ_10:
                resp = sff8020i.make_read_response(
                    backing, fields["lba"], fields["transfer_len"]
                )
            elif scsi_op == sff8020i.READ_TOC:
                resp = sff8020i.make_read_toc_response(
                    backing.lba_count,
                    msf=bool(fields.get("msf", 0)),
                    format_=fields.get("format", 0),
                )
            elif scsi_op in (sff8020i.MODE_SENSE_6, sff8020i.MODE_SENSE_10):
                resp = sff8020i.make_mode_sense_response(
                    fields.get("page_code", 0), fields.get("alloc_len", 8)
                )
            elif scsi_op in (
                sff8020i.TEST_UNIT_READY,
                sff8020i.PREVENT_ALLOW_MEDIUM_REMOVAL,
                sff8020i.START_STOP_UNIT,
            ):
                resp = b""
            else:
                console.print(f"[yellow]unsupported SCSI op {sff8020i.opcode_name(scsi_op)} "
                              f"(seq={seq}) — replying COMMAND_COMPLETE FAIL[/]")
                _send_sff_complete(conn, seq, status=SFF_STATUS_FAIL)
                continue

            console.print(f"  scsi: {sff8020i.opcode_name(scsi_op):<24s} "
                          f"seq={seq:>3}  resp={len(resp)}B  fields={fields}")

            if resp:
                _send_sff_data(conn, resp, seq)
            _send_sff_complete(conn, seq, status=SFF_STATUS_OK)

        except Exception as e:
            console.print(f"[red]error handling {sff8020i.opcode_name(scsi_op)}: {e}[/]")
            _send_sff_complete(conn, seq, status=SFF_STATUS_FAIL)


# ---------------------------------------------------------------------------
# Top-level entry: mount an ISO end-to-end
# ---------------------------------------------------------------------------

def mount_iso(settings: Settings, slot: int, iso_path: str | Path,
              kvm_port: int = PER_BLADE_KVM_PORT,
              on_idle: Callable[[], bool] | None = None) -> None:
    """Full vmedia mount flow.

    1. HMM Web login + extract per-session keys
    2. KVM stream (port 2200 default for blade 1) -> REQ_VMM_CODEKEY -> derive
    3. VM data plane (port 8500 + slot) -> CERTIFY_ID -> DEVICE_TYPE=CDROM
    4. SCSI loop responding to iBMC's INQUIRY/READ/etc with bytes from the .iso

    Blocks until the server tears down or `on_idle()` returns True.
    """
    console.rule(f"[bold]vmedia mount[/] slot={slot} iso={iso_path}")

    sess = login(settings.hmm_host, settings.hmm_user, settings.hmm_password,
                 verify_tls=settings.verify_tls)
    console.print(f"  HMM login ok  verifyvalue=0x{sess.verifyvalue:08x}")

    keys = negotiate_codekey(sess, slot, kvm_port=kvm_port)
    console.print(f"  codekey nego ok  sessionID derived")

    vm_port = sess.vmedia_port(slot)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect((sess.host, vm_port))
    local_ip = socket.inet_aton(s.getsockname()[0])

    s.sendall(_pack_certify_id(sessionid=keys["sessionid"], local_ip=local_ip))
    head = parse_header(_recv_exact(s, FRAME_HEAD_SIZE, timeout=RECV_TIMEOUT))
    if head.op != OpCode.ACK or head.ack_or_sub != AckCode.CERTIFY_PASS:
        s.close()
        raise RuntimeError(f"CERTIFY_ID rejected: op={head.op} sub={head.ack_or_sub}")
    console.print("  CERTIFY_PASS ✓")

    s.sendall(_pack_device_type(DeviceType.CDROM))
    head = parse_header(_recv_exact(s, FRAME_HEAD_SIZE, timeout=RECV_TIMEOUT))
    if head.op != OpCode.ACK or head.ack_or_sub != AckCode.DEVICE_CREAT:
        s.close()
        raise RuntimeError(f"DEVICE_TYPE rejected: op={head.op} sub={head.ack_or_sub}")
    console.print("  DEVICE_CREAT ✓")

    conn = Connection(sess=sess, slot=slot, sock=s)
    try:
        with IsoFileBacking(iso_path) as backing:
            run_session(conn, backing, on_idle=on_idle)
    finally:
        conn.close(DeviceType.CDROM)
        console.print("[dim]vmedia session closed[/]")
