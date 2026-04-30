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

from .login import Session
from .proto import (
    FRAME_HEAD_SIZE,
    AckCode,
    DeviceType,
    Header,
    OpCode,
    pack_header,
    parse_header,
)

DEFAULT_VERSION = (2, 1, 0, 0)
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
