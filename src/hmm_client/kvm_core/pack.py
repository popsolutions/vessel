"""Port of `com.kvm.PackData` — outgoing wire-frame builders.

Every Huawei iKVM packet on the wire follows the same envelope:

    [FE F6][hi  lo][sessionID 4 or 24 B][CRC 2 B BE][op 1 B][payload...]

`hi`, `lo` encode the body length where `body = CRC + op + payload`.
The high bit of `hi` (`LEN_HIGHBIT_SECURE = 0x80`) is set in *secure*
mode (when the chassis embed has `securekvm=1`).

`sessionID` is either:
- 4 bytes — the codekey int (`verifyvalue` for SMM packets, the
  per-blade `imagePaneCodeKey` for blade packets) written in
  big-endian. Java derives this via `intToByte_ret(codekey)` (LE)
  + `perIntToByteCon` (4-byte chunk reversal), netting BE. We just
  emit BE directly.
- 24 bytes — the post-suite-negotiation derived `sessionID`,
  byte-swapped per `perIntToByteCon`.

`CRC` is CRC-16/CCITT-FALSE (poly 0x1021, init 0) computed over
`[op] + payload`, written **big-endian** on the wire (Java emits
`temp[1]` then `temp[0]` after `intToByte` LE — which is the high
byte first). Some encrypted-mode builders skip the CRC and emit
`0x00 0x00` instead.
"""

from __future__ import annotations

from secrets import token_bytes

from .aes import encry, encry_bytes
from .base import (
    LEN_HIGHBIT_SECURE,
    PACKHEAD1,
    PACKHEAD2,
    per_int_to_byte_con,
)

# --- Op codes (PackData.java; outgoing) -----------------------------------
OP_KEY_PACK = 3  # keyboardPackCommon
OP_KEY_STATE = 4  # keyBoardState
OP_MOUSE_PACK = 5  # mousePack / mousePackNew_abs
OP_CONNECT_BLADE = 6  # connectBlade
OP_INTERRUPT_BLADE = 7
OP_RESEND_DATA = 8  # resendData (request I-frame)
OP_HEART_BEAT = 9
OP_REQ_BLADE_PRESENT = 11
OP_REQ_BLADE_STATE = 20  # connMode=0 (legacy)
OP_REQ_BLADE_STATE_TRANS = 33  # connMode=1 (secure body)
OP_MONITOR_BLADE = 23
OP_REPLAY_TO_SMM = 26
OP_SET_COLOR_BIT = 27
OP_CONTR_RATE = 28
OP_MOUSE_MODE = 36
OP_REQ_VMM_CODEKEY = 49
OP_GET_SUITE_LIST = 66
OP_SET_SUITE_PACK = 68


# --- CRC-16/CCITT-FALSE (poly=0x1021, init=0x0000) ------------------------
# Java's `KVMUtil.crc.wCrc((short)0, data, len)` with the wPoly=4129 branch.
_CRC16_TABLE: list[int] = []
for _i in range(256):
    _crc = _i << 8
    for _ in range(8):
        _crc = ((_crc << 1) ^ 0x1021) if (_crc & 0x8000) else (_crc << 1)
        _crc &= 0xFFFF
    _CRC16_TABLE.append(_crc)
del _i, _crc


def crc16(data: bytes, init: int = 0x0000) -> int:
    crc = init & 0xFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc & 0xFFFF


# --- Frame builder (the common envelope) ----------------------------------


def _codekey_4be(codekey: int) -> bytes:
    """`getImagePaneCodeKey_bytes` non-encrypted path — 4-byte BE int."""
    return (codekey & 0xFFFFFFFF).to_bytes(4, "big", signed=False)


def build_frame(
    op: int, payload: bytes, *, sessionid: bytes, secure: bool = False, with_crc: bool = True
) -> bytes:
    """Build a wire frame given an op + payload + sessionID bytes.

    - `sessionid` must already be in wire form (BE 4-byte int for plain
      or 24-byte derived sessionID for secure). Caller pre-swaps.
    - `with_crc=False` zeroes the CRC field (used by encrypted
      keyboard/mouse and a few other ops where Java explicitly sets
      `packData[sessidLen+4]=0; packData[sessidLen+5]=0;`).
    """
    body_len = 2 + 1 + len(payload)  # CRC + op + payload
    if body_len > 0x7FFF:
        raise ValueError(f"body too large: {body_len}")
    hi = ((body_len >> 8) & 0x7F) | (LEN_HIGHBIT_SECURE if secure else 0)
    lo = body_len & 0xFF
    if with_crc:
        c = crc16(bytes([op & 0xFF]) + payload)
        crc_bytes = bytes([(c >> 8) & 0xFF, c & 0xFF])
    else:
        crc_bytes = b"\x00\x00"
    return (
        bytes([PACKHEAD1, PACKHEAD2, hi, lo]) + sessionid + crc_bytes + bytes([op & 0xFF]) + payload
    )


def _smm_sid(smm_codekey: int, secure: bool, session_id_24: bytes | None) -> bytes:
    """Pick the right sessionID bytes for an SMM-addressed packet."""
    if secure:
        if session_id_24 is None or len(session_id_24) != 24:
            raise ValueError("secure mode needs 24-byte sessionID")
        return per_int_to_byte_con(session_id_24)
    return _codekey_4be(smm_codekey)


def _blade_sid(blade_codekey: int, encrypted: bool, session_id_24: bytes | None) -> bytes:
    """Pick the right sessionID bytes for a per-blade packet."""
    if encrypted:
        if session_id_24 is None or len(session_id_24) != 24:
            raise ValueError("encrypted blade thread needs 24-byte sessionID")
        return per_int_to_byte_con(session_id_24)
    return _codekey_4be(blade_codekey)


# --- SMM (handshake / port 2198) builders ---------------------------------


def req_blade_present(
    smm_codekey: int, *, secure: bool = False, session_id_24: bytes | None = None
) -> bytes:
    """`PackData.reqBladePresent` — op 11, no payload."""
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    return build_frame(OP_REQ_BLADE_PRESENT, b"", sessionid=sid, secure=secure)


def req_blade_state(
    blade_no: int,
    share_mode: int,
    smm_codekey: int,
    *,
    conn_mode: int = 0,
    secure: bool = False,
    session_id_24: bytes | None = None,
    kvm_key: bytes | None = None,
    vmm_iv: bytes | None = None,
) -> bytes:
    """`PackData.reqBladeState` — op 20 (conn_mode=0) or 33 (conn_mode=1).

    Plain mode body: `[bladeNo, shareMode]` (2 bytes).
    Secure mode body: AES-encrypted 16-byte block:
        rand[0]=bladeNo, rand[1]=shareMode, rand[2..15]=random
        encrypted with kvm_key + vmm_iv (16 bytes ciphertext).
    """
    op = OP_REQ_BLADE_STATE_TRANS if conn_mode == 1 else OP_REQ_BLADE_STATE
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    if secure:
        if kvm_key is None or vmm_iv is None:
            raise ValueError("secure reqBladeState needs kvm_key + vmm_iv")
        rand = bytearray(token_bytes(16))
        rand[0] = blade_no & 0xFF
        rand[1] = 0 if share_mode == 0 else 1
        payload = encry_bytes(bytes(rand), kvm_key, vmm_iv, 16)
        return build_frame(op, payload, sessionid=sid, secure=True)
    payload = bytes([blade_no & 0xFF, 0 if share_mode == 0 else 1])
    return build_frame(op, payload, sessionid=sid, secure=False)


def heart_beat_smm(
    smm_codekey: int, *, secure: bool = False, session_id_24: bytes | None = None
) -> bytes:
    """SMM heartbeat — op 9, payload `[0]`."""
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    return build_frame(OP_HEART_BEAT, bytes([0]), sessionid=sid, secure=secure)


def contr_rate_smm(
    frame_num: int, smm_codekey: int, *, secure: bool = False, session_id_24: bytes | None = None
) -> bytes:
    """`PackData.contrRate(frameNum)` — op 28, payload `[frameNum]`.

    Java sets CRC field to 0,0 explicitly here.
    """
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    return build_frame(
        OP_CONTR_RATE, bytes([frame_num & 0xFF]), sessionid=sid, secure=secure, with_crc=False
    )


def get_suite_list(
    blade_no: int, smm_codekey: int, *, secure: bool = False, session_id_24: bytes | None = None
) -> bytes:
    """`PackData.getSuiteList` — op 66, payload `[bladeNo]`."""
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    return build_frame(OP_GET_SUITE_LIST, bytes([blade_no & 0xFF]), sessionid=sid, secure=secure)


def set_suite_pack(
    blade_no: int,
    iterations: int,
    suite_type: int,
    smm_codekey: int,
    *,
    secure: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """`PackData.setSuitePack` — op 68.

    Payload: `[bladeNo, suite_type, iter_be_4B]`.
    """
    sid = _smm_sid(smm_codekey, secure, session_id_24)
    payload = bytes(
        [
            blade_no & 0xFF,
            suite_type & 0xFF,
            (iterations >> 24) & 0xFF,
            (iterations >> 16) & 0xFF,
            (iterations >> 8) & 0xFF,
            iterations & 0xFF,
        ]
    )
    return build_frame(OP_SET_SUITE_PACK, payload, sessionid=sid, secure=secure)


# --- Per-blade (port 2200) builders ----------------------------------------


def connect_blade(
    blade_no: int,
    color_bit: int,
    fpeg_alg: bool,
    blade_codekey: int,
    *,
    encrypted: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """`PackData.connectBlade` — op 6.

    Plain body: `[bladeNo, colorBit, fpegAlg]`.
    With `fpegAlg=true` Java appends one extra byte `0x01` (the
    `packData[sessidLen + 10] = 1` line).
    """
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    payload = bytes([blade_no & 0xFF, color_bit & 0xFF, 1 if fpeg_alg else 0])
    if fpeg_alg:
        payload += b"\x01"
    return build_frame(OP_CONNECT_BLADE, payload, sessionid=sid)


def monitor_blade(
    blade_no: int,
    blade_codekey: int,
    *,
    encrypted: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """`PackData.monitorBlade` — op 23, payload `[bladeNo, 1]`."""
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    return build_frame(OP_MONITOR_BLADE, bytes([blade_no & 0xFF, 1]), sessionid=sid)


def resend_data(
    blade_no: int,
    blade_codekey: int,
    *,
    encrypted: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """`PackData.resendData` — op 8, payload `[bladeNo]`. Asks for I-frame."""
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    return build_frame(OP_RESEND_DATA, bytes([blade_no & 0xFF]), sessionid=sid)


def heart_beat_blade(
    blade_no: int,
    blade_codekey: int,
    *,
    encrypted: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """Per-blade heartbeat — op 9, payload `[bladeNo]` (Java).

    Note this differs from SMM heartbeat where payload is `[0]`.
    """
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    return build_frame(OP_HEART_BEAT, bytes([blade_no & 0xFF]), sessionid=sid)


def contr_rate_blade(
    frame_num: int,
    blade_no: int,
    blade_codekey: int,
    *,
    encrypted: bool = False,
    session_id_24: bytes | None = None,
) -> bytes:
    """`PackData.contrRate(frameNum, bladeNo)` — op 28."""
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    return build_frame(OP_CONTR_RATE, bytes([frame_num & 0xFF, blade_no & 0xFF]), sessionid=sid)


# --- Keyboard --------------------------------------------------------------


def keyboard_pack(
    blade_no: int,
    hid_report_8b: bytes,
    *,
    blade_codekey: int,
    encrypted: bool,
    is_new: bool,
    session_id_24: bytes | None = None,
    kbd_key: bytes | None = None,
    kbd_iv: bytes | None = None,
) -> bytes:
    """`PackData.keyboardPackCommon` + `PackData.encry` — op 3.

    Java has TWO independent flags here, and which AES key to use
    depends on the combination:

    - `encrypted` ←→ `bThread.getEncrytedStatus()`:
        controls sessionID size (4 vs 24 bytes) AND which AES path
        the private `encry()` helper takes.

    - `is_new` ←→ `bThread.isNew()`:
        controls whether to encrypt the 8-byte HID at all. When false,
        send plaintext (CRC computed over `[op, bladeNo, hid8]`).

    When `is_new=True`, the encryption path mirrors Java's
    `PackData.encry()`:

        if (bThread.getEncrytedStatus()) {                  // Linux path
            encry_bytes(src, kbdkey, vmmkey_as_iv, 8)       // 8 → 16 bytes
        } else {                                            // Windows path
            encry(src, codekey, 8)                          // 8 → 16 bytes
        }                                                   // key=[codekey_be_4B,
                                                            //      5,6,7,8,1,2,3,4,5,6,7,8]
                                                            // iv=zeros

    The captured Palemoon traffic uses `getEncrytedStatus()=false`
    (4-byte sessionID) + `isNew()=true` (16-byte ciphertext) — so the
    actual encryption uses the **Windows** path with the codekey-
    derived AES key and a zero IV. This is the bug that was making
    typed characters land as random scancodes.
    """
    if len(hid_report_8b) != 8:
        raise ValueError(f"hid_report must be 8 bytes, got {len(hid_report_8b)}")
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    if is_new:
        if encrypted:
            if kbd_key is None or kbd_iv is None:
                raise ValueError("encrypted+is_new keyboard needs kbd_key + kbd_iv")
            ciphertext = encry_bytes(hid_report_8b, kbd_key, kbd_iv, 8)
        else:
            ciphertext = encry(hid_report_8b, blade_codekey, 8)
        payload = bytes([blade_no & 0xFF]) + ciphertext
        return build_frame(OP_KEY_PACK, payload, sessionid=sid, with_crc=False)
    payload = bytes([blade_no & 0xFF]) + hid_report_8b
    return build_frame(OP_KEY_PACK, payload, sessionid=sid)


# --- Mouse ------------------------------------------------------------------


def mouse_pack_abs(
    x_3000: int,
    y_3000: int,
    buttons: int,
    wheel: int,
    blade_no: int,
    *,
    blade_codekey: int,
    encrypted: bool,
    is_new: bool = True,
    session_id_24: bytes | None = None,
    kbd_key: bytes | None = None,
    kbd_iv: bytes | None = None,
) -> bytes:
    """`PackData.mousePackNew_abs` — op 5.

    Coordinates must be pre-scaled to the chassis's [0..3000] range.
    Mouse data is 6 plaintext bytes:
        [buttons, x_hi, x_lo, y_hi, y_lo, wheel]
    Java's `intToByte` writes x/y little-endian then re-orders bytes
    on the wire — netting big-endian (x_hi precedes x_lo).

    Same `encrypted` / `is_new` two-flag scheme as `keyboard_pack`:
    when `is_new=True` we encrypt the 6-byte mouse data into 16 bytes
    via either `encry_bytes` (encrypted=True / Linux path) or `encry`
    (encrypted=False / Windows path with codekey-derived AES key).
    """
    x = x_3000 & 0xFFFF
    y = y_3000 & 0xFFFF
    sid = _blade_sid(blade_codekey, encrypted, session_id_24)
    mouse_6 = bytes(
        [
            buttons & 0xFF,
            (x >> 8) & 0xFF,
            x & 0xFF,
            (y >> 8) & 0xFF,
            y & 0xFF,
            wheel & 0xFF,
        ]
    )
    if is_new:
        if encrypted:
            if kbd_key is None or kbd_iv is None:
                raise ValueError("encrypted+is_new mouse needs kbd_key + kbd_iv")
            ciphertext = encry_bytes(mouse_6, kbd_key, kbd_iv, 6)
        else:
            ciphertext = encry(mouse_6, blade_codekey, 6)
        payload = bytes([blade_no & 0xFF]) + ciphertext
        return build_frame(OP_MOUSE_PACK, payload, sessionid=sid, with_crc=False)
    payload = bytes([blade_no & 0xFF]) + mouse_6
    return build_frame(OP_MOUSE_PACK, payload, sessionid=sid)
