"""Port of `com.kvm.Base` — global session state, constants, key derivation.

The Java `Base` class is a god-object holding every static value the
applet needs: derived session keys, frame-rate constants, scancode
tables, the current shelf type, etc. We split that here:

- `key_map.py`            — Linux scancode + Web `e.code` → USB HID
- this module             — session keys, low-level byte helpers,
                            framing constants

The two key-derivation entry points are:

- `init_session_keys(verifyvalue, secretiv)` — Java's
  `initSessionIDAndKey(int)` — runs PBKDF2(72) with the verifyvalue
  decimal as password and `secretiv` as salt. Splits the output into
  sessionID(24) + kvmKey(16) + kbdKey(16) + vmmKey(16) and big-end
  swaps each key (Java's `perIntToByteCon`).

- `rotate_session_id(verifyvalueext, negotiateiv, hmac, iterations)`
  — Java's `BladeThread.distributeConsultation`-driven re-derivation.
  After the chassis sends a `KVM_SUITE_LIST` (op 67) and we ack with
  `setSuitePack` (op 68), both sides recompute the sessionID via
  `PBKDF2(verifyvalueext, salt=negotiateiv, hmac, iter, length=24)`.
  The encryption keys (kvmKey/kbdKey/vmmKey) are NOT rotated here —
  only the sessionID changes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .aes import generate_stored_password_hash

# --- Wire-protocol framing constants ---------------------------------------
PACKHEAD1: int = 0xFE
PACKHEAD2: int = 0xF6
LEN_HIGHBIT_SECURE: int = 0x80   # `Base.getsecurekvm()` flag bit in length high

# --- Session sizes (Base.java:50-52) ---------------------------------------
SESSION_ID_LEN: int = 24
PASS_KEY_LEN: int = 16
SALT_IV_LEN: int = 16

# --- Heartbeat / handshake timing (Base.java:21-28) ------------------------
TIME_OUT_MS: int = 20000
CONNECT_COUNT: int = 5
BLADE_CONNECT_COUNT: int = 15
BLADE_AUTOCONNECT_COUNT: int = 300
CONNECT_TIME_MS: int = 1000
BLADE_RECEIVE_TIME_MS: int = 10000
BLADE_HEART_TIME_MS: int = 2000
RAPMSG_CLOSE_TIME: int = 5000

# --- Frame rate (Base.java:40-42) ------------------------------------------
ZERO_FRAME: int = 0
ONE_FRAME: int = 1
THIRTY_FRAME: int = 35   # default contrRate value (op 28)

# --- KVM data plane port (chassis-wide; bladeNO selects the blade) ---------
BLADE_PORT_DEFAULT: int = 2200
HANDSHAKE_PORT_DEFAULT: int = 2198


# --- Byte helpers (Java's KVMUtil.* statics) -------------------------------

def per_int_to_byte_con(data: bytes) -> bytes:
    """Mirror `KVMUtil.perIntToByteCon` — byte-swap each 4-byte chunk.

    Java's wire format embeds little-endian-style fields by writing
    them with `intToByte` (LE) then reversing each 4-byte chunk. This
    helper applies the reversal.

        bytes[0..3]   reversed  →  bytes[3], bytes[2], bytes[1], bytes[0]
        bytes[4..7]   reversed  →  bytes[7], bytes[6], bytes[5], bytes[4]
        ...
    """
    if len(data) % 4 != 0:
        raise ValueError(f"length must be multiple of 4, got {len(data)}")
    out = bytearray(len(data))
    for i in range(0, len(data), 4):
        out[i:i + 4] = data[i:i + 4][::-1]
    return bytes(out)


def int_to_byte_le(value: int) -> bytes:
    """Java's `KVMUtil.intToByte` — write a 32-bit int as LE bytes."""
    return (value & 0xFFFFFFFF).to_bytes(4, "little", signed=False)


def byte_to_int_be(data: bytes, offset: int, length: int) -> int:
    """Java's `KVMUtil.byteToIntCon` — read big-endian unsigned int."""
    n = 0
    for i in range(offset, offset + length):
        n = (n << 8) | (data[i] & 0xFF)
    return n


# --- Session-key bundle (initSessionIDAndKey output) -----------------------

@dataclass(frozen=True)
class SessionKeys:
    """Output of `Base.initSessionIDAndKey(verifyvalue, secretiv)`.

    Mirrors the four 16-or-24-byte fields Java exposes via static
    accessors on `Base`. `*_bigend` are the byte-swapped versions
    used when sending or encrypting on the wire
    (`getXxxSecretKeyBigEnd`).
    """
    session_id: bytes              # 24 bytes
    kvm_secret_key: bytes          # 16 bytes
    kbd_secret_key: bytes          # 16 bytes
    vmm_secret_key: bytes          # 16 bytes
    kvm_secret_key_bigend: bytes
    kbd_secret_key_bigend: bytes
    vmm_secret_key_bigend: bytes


def init_session_keys(verifyvalue: int, secretiv: bytes,
                      iterations: int = 5000) -> SessionKeys:
    """Java's `Base.initSessionIDAndKey(int userKey)`.

        char[] plain = String.valueOf(userKey).toCharArray();
        completeKey = AESHandler.generateStoredPasswordHash(plain, 72, negotiatesalt);
        sessionID    = completeKey[0:24]
        kvmSecretKey = completeKey[24:40]    bigEnd = perIntToByteCon(...)
        kbdSecretKey = completeKey[40:56]    bigEnd = perIntToByteCon(...)
        vmmSecretKey = completeKey[56:72]    bigEnd = perIntToByteCon(...)

    `negotiatesalt` is a 16-byte slice of the embed's `secretiv`
    (`KVMApplet.init` lines 98-100).
    """
    if len(secretiv) < 16:
        raise ValueError(f"secretiv must be >=16 bytes, got {len(secretiv)}")
    salt = secretiv[:16]
    hashed = generate_stored_password_hash(str(verifyvalue), 72,
                                           rand_salt=salt,
                                           hmac="PBKDF2WithHmacSHA1",
                                           iterations=iterations)
    return SessionKeys(
        session_id=hashed[0:24],
        kvm_secret_key=hashed[24:40],
        kbd_secret_key=hashed[40:56],
        vmm_secret_key=hashed[56:72],
        kvm_secret_key_bigend=per_int_to_byte_con(hashed[24:40]),
        kbd_secret_key_bigend=per_int_to_byte_con(hashed[40:56]),
        vmm_secret_key_bigend=per_int_to_byte_con(hashed[56:72]),
    )


def rotate_session_id(verifyvalueext: bytes | str, negotiateiv: bytes,
                      hmac: str, iterations: int) -> bytes:
    """Java's post-suite-negotiation `setSessionID(...)` step.

        BladeThread.distributeConsultation():
            setSessionID(AESHandler.generateStoredPasswordHash(
                kvmInterface.getVerifyValueExt().toCharArray(),
                24, Base.getnegotiateiv(), getHmac(), getIterations()));

    The `verifyvalueext` is 32 hex chars from the embed (different
    from `verifyvalue`). Java passes it as a `char[]` straight into
    PBKDF2 — each char's low byte goes in. We accept either a
    pre-encoded `bytes` or a `str` and pass through to PBKDF2.
    """
    if len(negotiateiv) < 16:
        raise ValueError(f"negotiateiv must be >=16 bytes, got {len(negotiateiv)}")
    return generate_stored_password_hash(verifyvalueext, 24,
                                         rand_salt=negotiateiv[:16],
                                         hmac=hmac,
                                         iterations=iterations)
