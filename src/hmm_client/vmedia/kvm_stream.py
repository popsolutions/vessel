"""KVM stream framer (port 2198).

Wire format (per com/kvm/PackData.java + UnPackData.java):

    [0xFE 0xF6][hi-byte len/flag][lo-byte len][sessionID N=4|24][CRC16 BE][op][payload...]

  byte 0    PACKHEAD1 = 0xFE
  byte 1    PACKHEAD2 = 0xF6
  byte 2    high-bit (0x80) set => secure (sessionID 24 B); lower 7 bits = high
            byte of payload-length (= bytes from CRC onwards: 2 + 1 + payload)
  byte 3    low byte of that length
  bytes 4.. sessionID: 4 bytes (plain) or 24 bytes (secure)
  next 2    CRC16-CCITT (poly 0x1021, init 0) over [op + payload], BE
  next 1    op code (REQ_BLADE_PRESENT=11, REQ_VMM_CODEKEY=49, etc.)
  rest      op-specific payload

When secure=True the body (CRC + op + payload) is AES-128-CBC-NoPadding
encrypted with secretkey/secretiv from the embed.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

# CRC-16/CCITT-FALSE: poly=0x1021, init=0x0000, refin=False, refout=False, xorout=0x0000
# Java's "CRC_16_H" matches this with init seed=0 per the wPoly=4129 branch.
_CRC16_TABLE: list[int] = []
def _build_crc16_table() -> None:
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
        _CRC16_TABLE.append(crc)
_build_crc16_table()


def crc16_ccitt(data: bytes, init: int = 0x0000) -> int:
    crc = init & 0xFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc & 0xFFFF


# --- KVM op codes (subset; full list in PackData/UnPackData) ----------------
PACKHEAD1 = 0xFE
PACKHEAD2 = 0xF6
LEN_HIGHBIT_SECURE = 0x80

# Outgoing (PackData)
KVM_OP_KEY_PACK = 3
KVM_OP_MOUSE_PACK = 5
KVM_OP_CONNECT_BLADE = 6
KVM_OP_INTERRUPT_BLADE = 7
KVM_OP_HEART_BEAT = 9
KVM_OP_REQ_BLADE_PRESENT = 11
KVM_OP_REQ_BLADE_STATE = 20
KVM_OP_REQ_VMM_CODEKEY = 49

# Incoming (UnPackData)
KVM_OP_PRESENT_BLADE = 1
KVM_OP_IMAGE_DATA = 2
KVM_OP_KEY_STATE = 4
KVM_OP_CONNECT_STATE = 8
KVM_OP_VMM_CODEENCRYPT_REPORT = 50
KVM_OP_SECRET_NEGO = 64

VMM_CODEENCRYPT_REPORT_ENCRYPT_LEN = 20
VMM_CODEENCRYPT_REPORT_SALT_LEN = 16


@dataclass(frozen=True)
class KvmFrame:
    sessionid: bytes  # 4 or 24 bytes
    secure: bool
    op: int
    payload: bytes
    crc_ok: bool


def per_int_to_byte_con(value_int: int) -> bytes:
    """Mirror KVMUtil.perIntToByteCon: byte-swap a 4-byte int (LE↔BE chunks)."""
    le = value_int.to_bytes(4, "little", signed=False)
    return bytes(reversed(le))


def pack_kvm_frame(op: int, payload: bytes, sessionid: bytes,
                   secure: bool = False) -> bytes:
    """Build a KVM frame for the wire (client -> server: includes sessionID).

    Wire CRC is little-endian per KVMUtil.diviStream parser logic. The length
    field counts (CRC + op + payload) bytes, i.e. all bytes after sessionID.
    """
    if len(sessionid) not in (4, 24):
        raise ValueError(f"sessionid must be 4 or 24 bytes, got {len(sessionid)}")
    body = bytes([op & 0xFF]) + payload
    crc = crc16_ccitt(body)
    body_with_crc = struct.pack("<H", crc) + body  # CRC LITTLE-endian (low byte first)
    body_len = len(body_with_crc)  # = 2 + 1 + len(payload)
    if body_len > 0x7FFF:
        raise ValueError(f"body too large for 15-bit length: {body_len}")
    hi = ((body_len >> 8) & 0x7F) | (LEN_HIGHBIT_SECURE if secure else 0)
    lo = body_len & 0xFF
    return bytes([PACKHEAD1, PACKHEAD2, hi, lo]) + sessionid + body_with_crc


def recv_kvm_response(sock: socket.socket, timeout: float = 5.0) -> KvmFrame:
    """Read one server-sent KVM frame.

    Server responses do NOT echo the sessionID. Layout:
        [FE F6 lenH lenL] [CRC LE 2] [op 1] [payload]
    """
    sock.settimeout(timeout)
    head = _recv_exact(sock, 4)
    if head[0] != PACKHEAD1 or head[1] != PACKHEAD2:
        raise ValueError(f"bad magic: {head[:2].hex()} (want fef6)")
    body_len = (head[2] << 8) | head[3]  # response: full byte for length high
    body_with_crc = _recv_exact(sock, body_len)
    if len(body_with_crc) < 3:
        raise ValueError(f"body too small ({len(body_with_crc)} bytes)")
    crc_le = body_with_crc[:2]
    body = body_with_crc[2:]
    expected = struct.unpack("<H", crc_le)[0]  # LE on the wire
    actual = crc16_ccitt(body)
    op = body[0]
    payload = body[1:]
    return KvmFrame(
        sessionid=b"",  # server doesn't echo
        secure=False,
        op=op, payload=payload, crc_ok=(expected == actual),
    )


# Backward-compat alias (still used by older callers; will switch to *_response)
def recv_kvm_frame(sock: socket.socket, timeout: float = 5.0) -> KvmFrame:
    return recv_kvm_response(sock, timeout=timeout)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"server closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


# --- High-level builders matching specific PackData methods ------------------

def pack_req_vmm_codekey(blade_no: int, sessionid: bytes, secure: bool = False) -> bytes:
    """REQ_VMM_CODEKEY (op 49). Payload: 1 byte = bladeNO."""
    return pack_kvm_frame(KVM_OP_REQ_VMM_CODEKEY, bytes([blade_no & 0xFF]),
                          sessionid=sessionid, secure=secure)


def pack_req_blade_present(sessionid: bytes, secure: bool = False) -> bytes:
    """REQ_BLADE_PRESENT (op 11). No payload."""
    return pack_kvm_frame(KVM_OP_REQ_BLADE_PRESENT, b"", sessionid=sessionid, secure=secure)


def pack_heartbeat(sessionid: bytes, secure: bool = False) -> bytes:
    return pack_kvm_frame(KVM_OP_HEART_BEAT, b"", sessionid=sessionid, secure=secure)
