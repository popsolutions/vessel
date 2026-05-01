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


def _huawei_crc_field(crc16: int) -> bytes:
    """Encode CRC as 16-bit BIG-ENDIAN (high byte first), confirmed empirically
    from a captured Palemoon session. Earlier "sign-only" theory from the
    decompiled source was a jadx artifact — actual wire format is plain BE.
    """
    return struct.pack(">H", crc16 & 0xFFFF)


def pack_kvm_frame(op: int, payload: bytes, sessionid: bytes, secure: bool = False) -> bytes:
    """Build a KVM frame for the wire.

    Java's PackData runs `KVMUtil.perIntToByteCon` on the sessionID before
    writing it into the packet — that's a per-4-byte-chunk reversal. So a
    sessionID `[a b c d e f g h ...]` becomes `[d c b a h g f e ...]` on the
    wire. Same swap applied here.

    Length field counts (CRC + op + payload) bytes (all bytes after the
    sessionID). CRC is encoded per `_huawei_crc_field` (sign-only quirk).
    """
    if len(sessionid) not in (4, 24):
        raise ValueError(f"sessionid must be 4 or 24 bytes, got {len(sessionid)}")
    sessionid_wire = _byte_swap_4byte_chunks(sessionid)
    body = bytes([op & 0xFF]) + payload
    crc = crc16_ccitt(body)
    body_with_crc = _huawei_crc_field(crc) + body
    body_len = len(body_with_crc)  # = 2 + 1 + len(payload)
    if body_len > 0x7FFF:
        raise ValueError(f"body too large for 15-bit length: {body_len}")
    hi = ((body_len >> 8) & 0x7F) | (LEN_HIGHBIT_SECURE if secure else 0)
    lo = body_len & 0xFF
    return bytes([PACKHEAD1, PACKHEAD2, hi, lo]) + sessionid_wire + body_with_crc


def _byte_swap_4byte_chunks(data: bytes) -> bytes:
    """Mirror KVMUtil.perIntToByteCon: byte-swap each 4-byte chunk."""
    if len(data) % 4 != 0:
        raise ValueError(f"length must be % 4, got {len(data)}")
    out = bytearray(len(data))
    for i in range(0, len(data), 4):
        out[i : i + 4] = data[i : i + 4][::-1]
    return bytes(out)


def initial_session_keys(
    verifyvalue: int, secretiv: bytes, iterations: int = 5000
) -> dict[str, bytes]:
    """Java's Base.initSessionIDAndKey() — initial chassis-wide sessionID + AES keys.

    plain  = str(verifyvalue)          # decimal string ("245898693")
    salt   = secretiv (16 bytes)
    iter   = 5000 (from generateStoredPasswordHash 3-arg form)
    length = 72 bytes
    ↓
    sessionID    = out[0:24]
    kvmSecretKey = out[24:40]    bigEnd = perIntToByteCon(kvm)
    kbdSecretKey = out[40:56]    bigEnd = perIntToByteCon(kbd)
    vmmSecretKey = out[56:72]    bigEnd = perIntToByteCon(vmm)
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    password = str(verifyvalue).encode("ascii")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=72, salt=secretiv, iterations=iterations)
    out = kdf.derive(password)
    return {
        "sessionid": out[:24],
        "kvm_secret_key": out[24:40],
        "kbd_secret_key": out[40:56],
        "vmm_secret_key": out[56:72],
        "kvm_secret_key_bigend": _byte_swap_4byte_chunks(out[24:40]),
        "kbd_secret_key_bigend": _byte_swap_4byte_chunks(out[40:56]),
        "vmm_secret_key_bigend": _byte_swap_4byte_chunks(out[56:72]),
    }


def derive_sessionid_pbkdf2(
    verifyvalueext_hex: str, salt: bytes, iterations: int = 5000, length: int = 24
) -> bytes:
    """Post-suite-negotiation sessionID per BladeThread.java:339.

    NOTE: this is NOT used for the first REQ_BLADE_PRESENT packet. It rotates
    AFTER setSuitePack (op 68) negotiates a new iteration count. The initial
    sessionID comes from `initial_session_keys()` instead.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    password = verifyvalueext_hex.encode("ascii")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=length, salt=salt, iterations=iterations)
    return kdf.derive(password)


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
        op=op,
        payload=payload,
        crc_ok=(expected == actual),
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
    """REQ_VMM_CODEKEY (op 49). Payload: 1 byte = bladeNO.

    Sent on the per-blade KVM stream port (e.g., 2200 for Blade 1, returned
    in the BLADE_STATE response). The 4-byte sessionID is the per-blade
    `imagePaneCodeKey` int (byte-swapped via perIntToByteCon).
    """
    return pack_kvm_frame(
        KVM_OP_REQ_VMM_CODEKEY, bytes([blade_no & 0xFF]), sessionid=sessionid, secure=secure
    )


def parse_vmm_codekey_report(payload: bytes) -> tuple[bytes, bytes]:
    """Decode VMM_CODEENCRYPT_REPORT (op 50) payload.

    Layout (per the captured Palemoon session):
        [1 B status/flag = 00] [20 B negoCodeKey ASCII hex] [16 B negoSalt]

    Returns (negoCodeKey: 20-byte ASCII string, negoSalt: 16-byte bytes).
    """
    if len(payload) < 37:
        raise ValueError(f"VMM_CODEENCRYPT_REPORT payload must be ≥37 B, got {len(payload)}")
    # payload[0] = status flag (00 = ok per observed traffic)
    nego_codekey = payload[1:21]  # 20 bytes (ASCII chars)
    nego_salt = payload[21:37]  # 16 bytes (binary)
    return nego_codekey, nego_salt


def derive_vmedia_session_keys(
    nego_codekey: bytes, nego_salt: bytes, iterations: int = 5000
) -> dict[str, bytes]:
    """PBKDF2-HMAC-SHA1 derivation per VMConsole.createSecretCertifyCode (bCodeKeyNego=true).

    password = nego_codekey (20 ASCII bytes -> char[] -> UTF-8 bytes)
    salt     = nego_salt (16 bytes)
    iter     = 5000 (initial; rotated by setSuitePack)
    length   = 56 bytes
    ↓
    sessionid = out[:24]   ← CERTIFY_ID body field on port 8501 (after byte-swap)
    secretKey = out[24:40]
    secretIV  = out[40:56]
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if len(nego_codekey) != 20:
        raise ValueError(f"nego_codekey must be 20 bytes, got {len(nego_codekey)}")
    if len(nego_salt) != 16:
        raise ValueError(f"nego_salt must be 16 bytes, got {len(nego_salt)}")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=56, salt=nego_salt, iterations=iterations)
    out = kdf.derive(bytes(nego_codekey))
    return {
        "sessionid": out[:24],
        "secret_key": out[24:40],
        "secret_iv": out[40:56],
    }


def pack_req_blade_present(sessionid: bytes, secure: bool = False) -> bytes:
    """REQ_BLADE_PRESENT (op 11). No payload."""
    return pack_kvm_frame(KVM_OP_REQ_BLADE_PRESENT, b"", sessionid=sessionid, secure=secure)


def pack_heartbeat(sessionid: bytes, secure: bool = False) -> bytes:
    return pack_kvm_frame(KVM_OP_HEART_BEAT, b"", sessionid=sessionid, secure=secure)
