"""Live Huawei iKVM client — handshake + per-blade frame stream.

End-to-end flow (per `KVMApplet.processShelf` + `PackData`/`UnPackData`):

    1. HTTPS login via `vmedia.login.login()` -> embed values
    2. PBKDF2(verifyvalue, secretiv, 5000, 72) -> 24-byte sessionID
       + 3 AES-128 keys (kvm/kbd/vmm) — the *chassis-wide* key bag
    3. TCP connect <HMM>:2198
    4. send REQ_BLADE_PRESENT  (op 11, no payload)
    5. recv PRESENT_BLADE       (op 1,  4-byte blade-bitmap)
    6. send REQ_BLADE_STATE     (op 20, [bladeNO, shareMode] in plain
                                 mode, 16-byte AES blob in secure)
    7. recv BLADE_STATE         (op 21, [marker, state-byte, ?,
                                 bladeIP(4 BE), bladePort(2 BE), ...])
    8. TCP connect <HMM>:bladePort
    9. send CONNECT_BLADE       (op 6, [bladeNO, colorBit, fpegAlg=0])
   10. recv IMAGE_DATA frames   (op 2, chunked) -> reassemble -> decode

Only the **OldRLE** path (fpegAlg=0) is wired here — that's what the
captured pcap proved we can decode. The NewRLE/JPEG path lands later
behind the same client API.

Many fields are best-effort: if the chassis wants extras we don't
provide (suite-list negotiation, etc.) we degrade gracefully — log
the unexpected op codes and keep going.
"""
from __future__ import annotations

import io
import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..vmedia.kvm_stream import (
    PACKHEAD1,
    PACKHEAD2,
    initial_session_keys,
    pack_kvm_frame,
)
from ..vmedia.login import Session, login
from .codec_old import BGR233_PALETTE, bgr233_to_rgb888, decode_old_rle  # noqa: F401

log = logging.getLogger(__name__)

# Op codes we send (from PackData.java)
OP_REQ_BLADE_PRESENT     = 11
OP_REQ_BLADE_STATE       = 20   # connMode=0 (legacy); chassis often expects 33
OP_REQ_BLADE_STATE_TRANS = 33   # connMode=1; what the captured Palemoon used
OP_CONNECT_BLADE         = 6
OP_HEART_BEAT            = 9
OP_KEY_PACK              = 3
OP_MOUSE_PACK            = 5
OP_CONTR_RATE            = 28   # frame-rate hint; payload = [framerate_byte]
OP_MOUSE_MODE            = 36
OP_MONITOR_BLADE         = 23   # PackData.monitorBlade; "wake/refresh" hint

DEFAULT_FRAMERATE = 35   # = Base.THIRTY_FRAME

# The KVM data-plane port is chassis-wide — packets identify their target
# blade via the `bladeNO` byte. The pcap proves this: 192.168.1.30:2200
# carried frames for whichever blade the client requested.
BLADE_PORT_DEFAULT = 2200

# Op codes we receive (from UnPackData.java)
OP_PRESENT_BLADE = 1
OP_IMAGE_DATA    = 2
OP_KEY_STATE     = 4
OP_CONNECT_STATE = 8
OP_BLADE_STATE   = 21
OP_SECRET_NEGO   = 64
OP_KVM_SUITE_LIST = 67


@dataclass(frozen=True)
class BladeState:
    """Subset of `KVMUtil.showBladeDown` output."""
    blade_ip: str
    blade_port: int
    rle_alg: bool          # state[0]
    fpeg_alg: bool         # state[1]
    bmc_reset: bool        # state[2]
    blade_down: bool       # state[3]
    flag4: bool            # state[4]
    kvm_supported: bool    # state[5]
    flag6: bool            # state[6]
    blade_present: bool    # state[7]
    secure_kvm: bool
    secure_vmm: bool


@dataclass
class _ParsedFrame:
    op: int
    payload: bytes
    raw_len: int           # body length on the wire (incl. CRC + op)


class KvmClient:
    """Live iKVM connection for one blade.

    Use as a context manager; iterate `frames()` to consume PNGs.
    """

    def __init__(self, host: str, user: str, password: str,
                 verify_tls: bool = False,
                 use_newrle: bool | None = None) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.verify_tls = verify_tls

        self.session: Session | None = None
        self.smm_sessionid: bytes = b""   # 24 bytes
        self.kvm_secret_key: bytes = b""  # 16 bytes (AES key, big-end)
        self.kbd_secret_key: bytes = b""
        self.vmm_secret_key: bytes = b""  # AES IV for reqBladeState

        self.smm_sock: socket.socket | None = None
        self.blade_sock: socket.socket | None = None
        self.blade_no: int | None = None
        self.blade_state: BladeState | None = None

        # Codec selector. Per-call `use_newrle` wins; otherwise fall
        # back to the HMM_KVM_USE_NEWRLE env var. When True we request
        # the chassis NewRLE/JPEG ("FPEG") path during connect_blade
        # and route frame data through kvm_core.decoder. Default
        # (None + unset env) = OldRLE BGR233 path known-good against
        # this chassis.
        if use_newrle is not None:
            self.use_newrle = bool(use_newrle)
        else:
            self.use_newrle = (
                os.environ.get("HMM_KVM_USE_NEWRLE", "").lower()
                in ("1", "true", "yes", "on")
            )

        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> "KvmClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        for s in (self.blade_sock, self.smm_sock):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        self.smm_sock = None
        self.blade_sock = None

    # ------------------------------------------------------------------
    def open(self, slot: int) -> None:
        """Run the full handshake for `slot` and leave both sockets ready."""
        if not 1 <= slot <= 32:
            raise ValueError(f"slot must be 1..32, got {slot}")
        self.blade_no = slot

        self.session = login(self.host, self.user, self.password,
                             verify_tls=self.verify_tls)
        log.info("KVM login ok host=%s securekvm=%s port=%s",
                 self.host, self.session.secure, self.session.handshake_port)

        keys = initial_session_keys(self.session.verifyvalue, self.session.aes_iv)
        self.smm_sessionid   = keys["sessionid"]
        self.kvm_secret_key  = keys["kvm_secret_key_bigend"]
        self.kbd_secret_key  = keys["kbd_secret_key_bigend"]
        self.vmm_secret_key  = keys["vmm_secret_key_bigend"]

        self.smm_sock = socket.create_connection(
            (self.host, self.session.handshake_port), timeout=15.0)
        self.smm_sock.settimeout(20.0)

        self._send_smm(OP_REQ_BLADE_PRESENT, b"")
        present = self._recv_until(self.smm_sock, OP_PRESENT_BLADE, timeout=15.0)
        log.info("PRESENT_BLADE bitmap=%s", present.payload[:8].hex())

        self._send_req_blade_state(slot)
        bstate = self._recv_until(self.smm_sock, OP_BLADE_STATE, timeout=15.0)
        self.blade_state = self._parse_blade_state(bstate.payload)
        log.info("BLADE_STATE %s", self.blade_state)

        if not self.blade_state.blade_present:
            raise RuntimeError(f"blade{slot} reports not-present")
        if not self.blade_state.kvm_supported:
            raise RuntimeError(f"blade{slot} reports KVM not supported")

        # Captured pcap shows ~6 SMM heartbeats between BLADE_STATE response
        # and the per-blade socket opening. The chassis seems to want the
        # SMM session "warm" before accepting blade-stream connections.
        for _ in range(3):
            self._send_smm(OP_HEART_BEAT, bytes([0]))
            time.sleep(0.5)

        # Data-plane port is chassis-wide (BLADE_STATE.port is an internal hint
        # in op-20 mode; for op-33 it does carry the right port — but it's
        # always 2200 in practice). Use the default.
        port = self.blade_state.blade_port or BLADE_PORT_DEFAULT
        self.blade_sock = socket.create_connection(
            (self.host, port), timeout=15.0)
        self.blade_sock.settimeout(30.0)

        # Per pcap: heartbeat -> connectBlade -> contrRate -> connectBlade.
        # Then monitorBlade(slot, 1) acts as a "send me current screen" hint —
        # without it, idle blades only produce sentinel deltas.
        self._send_blade_heartbeat()
        self._send_connect_blade(slot, color_bit=0, fpeg_alg=self.use_newrle)
        self._send_contr_rate(DEFAULT_FRAMERATE)
        self._send_connect_blade(slot, color_bit=0, fpeg_alg=self.use_newrle)
        self._send_monitor_blade(slot)
        if self.use_newrle:
            log.info(
                "KVM codec: requesting NewRLE/JPEG (fpeg_alg=True) — "
                "experimental path; unset HMM_KVM_USE_NEWRLE to revert."
            )

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name=f"kvm-hb-{slot}")
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    def _send_smm(self, op: int, payload: bytes) -> None:
        assert self.smm_sock is not None
        secure = self.session.secure if self.session else False
        sid = self.smm_sessionid if secure else self.smm_sessionid[:4]
        self.smm_sock.sendall(pack_kvm_frame(op, payload, sid, secure=secure))

    def _send_req_blade_state(self, blade_no: int) -> None:
        """Captured Palemoon used op 33 + connMode=1 + AES body (16 B)."""
        share_mode = 0
        secure = self.session.secure if self.session else False
        if not secure:
            self._send_smm(OP_REQ_BLADE_STATE, bytes([blade_no, share_mode]))
            return
        from secrets import token_bytes

        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        rand = bytearray(token_bytes(16))
        rand[0] = blade_no & 0xFF
        rand[1] = share_mode & 0xFF
        cipher = Cipher(algorithms.AES(self.kvm_secret_key),
                        modes.CBC(self.vmm_secret_key))
        enc = cipher.encryptor().update(bytes(rand))
        self._send_smm(OP_REQ_BLADE_STATE_TRANS, enc)

    def _send_connect_blade(self, blade_no: int, color_bit: int,
                            fpeg_alg: bool) -> None:
        """Body: [bladeNO, colorBit, fpegAlg]; 4-byte verifyvalue sessionID.

        Java's `PackData.connectBlade` appends an extra `0x01` byte at
        `packData[sessidLen + 10]` when `fpegAlg=true` — that's the
        actual signal the chassis uses to switch from OldRLE to the
        NewRLE/JPEG codec. Without this byte the chassis ignores the
        flag and keeps sending OldRLE frames. Verified against
        `re/decompiled/sources/com/kvm/PackData.java:336-338`.
        """
        assert self.blade_sock is not None
        body = bytes([blade_no & 0xFF, color_bit & 0xFF, 1 if fpeg_alg else 0])
        if fpeg_alg:
            body += b"\x01"
        self.blade_sock.sendall(pack_kvm_frame(OP_CONNECT_BLADE, body,
                                               self._blade_sid(), secure=False))

    # ------------------------------------------------------------------
    def _blade_sid(self) -> bytes:
        """Per-blade sessionID = `verifyvalue` written little-endian.

        `KVMUtil.intToByte_ret(int)` produces *little-endian* bytes
        (byte 0 = LSB, byte 3 = MSB); `pack_kvm_frame` then reverses
        each 4-byte chunk via `perIntToByteCon` to put it big-endian
        on the wire. Verified against the captured pcap.
        """
        if self.session is None:
            return b"\x00\x00\x00\x00"
        return self.session.verifyvalue.to_bytes(4, "little")

    def _send_contr_rate(self, framerate: int) -> None:
        """contrRate (op 28) — single byte body."""
        if self.blade_sock is None:
            return
        self.blade_sock.sendall(pack_kvm_frame(
            OP_CONTR_RATE, bytes([framerate & 0xFF]),
            self._blade_sid(), secure=False))

    def _send_monitor_blade(self, blade_no: int) -> None:
        """monitorBlade (op 23) — body [bladeNO, 1]; nudges chassis to push frame."""
        if self.blade_sock is None:
            return
        body = bytes([blade_no & 0xFF, 1])
        self.blade_sock.sendall(pack_kvm_frame(
            OP_MONITOR_BLADE, body, self._blade_sid(), secure=False))

    def _send_blade_heartbeat(self) -> None:
        """Per-blade heartbeat. Payload is `[bladeNO]` per captured pcap."""
        if self.blade_sock is None or self.blade_no is None:
            return
        body = bytes([self.blade_no & 0xFF])
        self.blade_sock.sendall(pack_kvm_frame(
            OP_HEART_BEAT, body, self._blade_sid(), secure=False))

    def request_keyframe(self) -> None:
        """Ask the chassis for a fresh I-frame.

        OldRLE diff frames carry `delta = old_screen XOR new_screen`. If
        our local `prev` ever drifts from the chassis's notion of `old`
        (a missed/corrupt chunk, an out-of-order delivery, a sentinel
        slipping through) every subsequent XOR compounds the error
        until the next keyframe rebases. Java's BladeThread requests an
        I-frame via `resendData` (op 8) the moment it detects trouble.
        We don't have the same drift detection yet, so we just nudge
        periodically to cap how long bad pixels can persist.
        """
        if self.blade_sock is None or self.blade_no is None:
            return
        body = bytes([self.blade_no & 0xFF])
        try:
            self.blade_sock.sendall(pack_kvm_frame(
                8, body, self._blade_sid(), secure=False))
        except OSError as e:
            log.debug("request_keyframe send failed: %s", e)

    def _heartbeat_loop(self) -> None:
        """Heartbeat both sockets every ~2s.

        We deliberately don't piggyback an op-8 I-frame request on
        every heartbeat tick — the chassis disconnects when
        `resendData` is sent too aggressively. Now that the W/2 row
        rotation has eliminated the stride bug that caused XOR drift,
        the chassis's own keyframe cadence (~one every 3 seconds in
        practice) is plenty.
        """
        while not self._stop.is_set():
            try:
                if self.smm_sock is not None:
                    self._send_smm(OP_HEART_BEAT, bytes([0]))
                self._send_blade_heartbeat()
            except OSError:
                return
            self._stop.wait(2.0)

    # ------------------------------------------------------------------
    def _recv_until(self, sock: socket.socket, want_op: int,
                    timeout: float = 15.0) -> _ParsedFrame:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for op {want_op}")
            sock.settimeout(remaining)
            fr = self._recv_one(sock)
            if fr.op == want_op:
                return fr
            log.debug("ignoring op %d (%d B) while waiting for %d",
                      fr.op, fr.raw_len, want_op)

    def _recv_one(self, sock: socket.socket) -> _ParsedFrame:
        head = self._recv_exact(sock, 4)
        if head[0] != PACKHEAD1 or head[1] != PACKHEAD2:
            raise ValueError(f"bad magic: {head[:2].hex()}")
        body_len = (head[2] << 8) | head[3]
        if body_len < 3:
            raise ValueError(f"body too small ({body_len})")
        body = self._recv_exact(sock, body_len)
        op = body[2]
        return _ParsedFrame(op=op, payload=body[3:], raw_len=body_len)

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"server closed after {len(buf)}/{n}")
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _parse_blade_state(payload: bytes) -> BladeState:
        """Parse BLADE_STATE op 21 payload (per `KVMUtil.showBladeDown`)."""
        if len(payload) < 9:
            raise ValueError(f"BLADE_STATE payload too short: {len(payload)}B "
                             f"hex={payload.hex()}")
        state_byte = payload[1]
        st = [(state_byte >> i) & 1 for i in range(8)]
        ip_bytes = payload[3:7]
        blade_ip = ".".join(str(b & 0xFF) for b in ip_bytes)
        blade_port = (payload[7] << 8) | payload[8]
        last = payload[-1]
        return BladeState(
            blade_ip=blade_ip,
            blade_port=blade_port,
            rle_alg=bool(st[0]),
            fpeg_alg=bool(st[1]),
            bmc_reset=bool(st[2]),
            blade_down=bool(st[3]),
            flag4=bool(st[4]),
            kvm_supported=bool(st[5]),
            flag6=bool(st[6]),
            blade_present=bool(st[7]),
            secure_kvm=(last & 0x02) == 0x02,
            secure_vmm=(last & 0x04) == 0x04,
        )

    # ------------------------------------------------------------------
    def frames(self) -> Iterator[tuple[int, int, int, bool, bytes]]:
        """Yield (img_id, width, height, is_diff, encoded_data) tuples.

        Header layout (Python `p` index = Java `bytes` index + 1, because
        Java's BladeThread.run strips an extra byte before the buffer
        reaches KVMUtil.setVar):

            p[1..2] BE   chunk position (0 = first chunk of a frame)
            p[3]         frame number (img_id, 1-byte rolling counter)
            p[4..7] BE   total compressed length (packLength)
            p[8]         top bit = diff flag, low 7 bits = width hi
            p[9]         width lo  ⇒  width = ((p[8] & 0x7F) << 8) | p[9]
            p[10..11] BE height
            p[13..14] BE remoteX (mouse, ignored here)
            p[15..16] BE remoteY
            p[17]        colorBit

        Sentinel/keepalive frames carry a tiny payload with diff=1 — they
        are valid diff frames carrying "no change". We surface them with
        `is_diff=True` so the upstream XOR step can apply them as a no-op
        rather than dropping the frame and stalling the canvas.
        """
        if self.blade_sock is None:
            raise RuntimeError("call open() before frames()")
        partial: dict[int, dict[str, Any]] = {}
        while not self._stop.is_set():
            try:
                fr = self._recv_one(self.blade_sock)
            except (TimeoutError, OSError) as e:
                log.warning("blade socket recv: %s", e)
                return
            if fr.op != OP_IMAGE_DATA:
                log.debug("blade non-image op %d (%d B)", fr.op, fr.raw_len)
                continue
            p = fr.payload
            if len(p) < 4:
                continue
            chunk_pos = (p[1] << 8) | p[2]
            img_id = p[3]
            if chunk_pos == 0:
                if len(p) < 18:
                    continue
                total = int.from_bytes(p[4:8], "big")
                is_diff = (p[8] & 0x80) != 0
                w = ((p[8] & 0x7F) << 8) | p[9]
                h = int.from_bytes(p[10:12], "big")
                if w == 0 or h == 0 or w > 4096 or h > 4096:
                    continue
                partial[img_id] = {"chunks": {}, "received": 0,
                                   "total": total, "w": w, "h": h,
                                   "diff": is_diff}
            else:
                slot = partial.get(img_id)
                if slot is None:
                    continue
                # Index by chunk_pos so we can reassemble in order. The
                # Huawei codec is allowed to deliver chunks in any
                # sequence (Java's KVMUtil.combine iterates `bufferA`
                # by chunk_pos, not arrival order). Linear append
                # produced left/right half-swaps when the chassis
                # interleaved them — visible as each chassis row
                # appearing rotated by W/2.
                if chunk_pos in slot["chunks"]:
                    continue
                payload = p[4:]
                slot["chunks"][chunk_pos] = payload
                slot["received"] += len(payload)
                if slot["received"] >= slot["total"]:
                    # Reassemble in chunk_pos order, MIRRORING JAVA EXACTLY:
                    # Java's `KVMUtil.combine()` builds `data` of size
                    # `packLength+1`, leaves `data[0] = 0` (untouched),
                    # then concatenates chunk[1..N] starting at `data[1]`
                    # (`int index = 1`). The OldRLE decoder then reads
                    # from `i = 1`, so `data[1]` (the first byte of
                    # chunk[1]) becomes the first colour byte. WITHOUT
                    # the leading zero, our decoder reads the SECOND
                    # byte of chunk[1] as the first colour and every
                    # subsequent run is shifted — accumulating to a
                    # ~260-pixel horizontal offset between Palemoon's
                    # render and ours.
                    ordered = bytearray(b"\x00")
                    for cp in sorted(slot["chunks"]):
                        ordered.extend(slot["chunks"][cp])
                    payload_bytes = bytes(ordered[:slot["total"] + 1])
                    # Java's DrawThread skip rule: a diff frame whose
                    # entire payload is "5 bytes with leading 0" is a
                    # pure keepalive — XOR-ing it onto the framebuffer
                    # paints garbage (the OldRLE state machine reads
                    # past the end and produces stray colours in the
                    # top-left). Real small diffs start with a non-zero
                    # byte, so the leading-byte test cleanly separates
                    # them from sentinels.
                    if (slot["diff"] and slot["total"] == 5
                            and payload_bytes[0] == 0):
                        partial.pop(img_id, None)
                        continue
                    yield (img_id, slot["w"], slot["h"], slot["diff"],
                           payload_bytes)
                    partial.pop(img_id, None)

    # ------------------------------------------------------------------
    # Input — keyboard (HID 8-byte report) and absolute mouse.
    # ------------------------------------------------------------------
    def _encrypt_input(self, plaintext: bytes) -> bytes:
        """AES-128-CBC NoPadding encrypt — Java's `AESHandler.encry_bytes`.

        Pads `plaintext` with zeros to the next 16-byte boundary and
        encrypts with `kbd_secret_key` as the AES key and
        `vmm_secret_key` as the IV (Java reuses vmmkey as the IV for
        keyboard/mouse — see `BladeThread.getBladeKeyIV`). Output is a
        multiple of 16 bytes.

        The chassis ALWAYS expects encrypted keyboard/mouse on the
        per-blade socket once the session is established — confirmed
        by capturing Palemoon's outgoing op-3 frames and seeing
        16-byte AES-shaped ciphertext where our code was sending raw
        HID bytes. The chassis side AES-decrypted our plaintext into
        random scancodes, which the user saw as totally unrelated
        characters.
        """
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        pad_len = (-len(plaintext)) % 16
        padded = plaintext + b"\x00" * pad_len
        cipher = Cipher(algorithms.AES(self.kbd_secret_key),
                        modes.CBC(self.vmm_secret_key))
        return cipher.encryptor().update(padded) + cipher.encryptor().finalize()

    def _send_encrypted_input(self, op: int, blade_no: int,
                              encrypted: bytes) -> None:
        """Build & send an encrypted keyboard/mouse frame.

        Wire layout (matches Palemoon's captured op-3/op-5 traffic):
          [FE F6][lenH lenL] [sessionID 4B] [00 00] [op] [bladeNo] [enc]

        CRC field is forced to 0,0 in the encrypted path (Java does
        `packData[sessidLen+4]=0; packData[sessidLen+5]=0;`). Length
        field counts CRC + op + bladeNo + ciphertext.
        """
        if self.blade_sock is None:
            return
        from ..vmedia.kvm_stream import _byte_swap_4byte_chunks

        body_with_crc = (b"\x00\x00"
                         + bytes([op & 0xFF, blade_no & 0xFF])
                         + encrypted)
        body_len = len(body_with_crc)
        sessionid_swapped = _byte_swap_4byte_chunks(self._blade_sid())
        head = bytes([PACKHEAD1, PACKHEAD2,
                      (body_len >> 8) & 0x7F, body_len & 0xFF])
        self.blade_sock.sendall(head + sessionid_swapped + body_with_crc)

    def send_key_report(self, hid_report: bytes) -> None:
        """Send an 8-byte HID keyboard report, AES-encrypted.

        Delegates to the validated `kvm_core.pack.keyboard_pack`.

        HID report layout (USB Boot Keyboard):
            byte 0: modifier mask (LCtrl=1, LShift=2, LAlt=4, LMeta=8,
                                   RCtrl=16, RShift=32, RAlt=64, RMeta=128)
            byte 1: reserved (0)
            bytes 2..7: up to 6 simultaneously-pressed USB HID usage codes
        """
        if self.blade_sock is None or self.blade_no is None or self.session is None:
            return
        if len(hid_report) != 8:
            raise ValueError(f"hid_report must be 8 bytes, got {len(hid_report)}")
        from ..kvm_core.pack import keyboard_pack
        frame = keyboard_pack(
            blade_no=self.blade_no,
            hid_report_8b=hid_report,
            blade_codekey=self.session.verifyvalue,
            encrypted=False,   # bThread.getEncrytedStatus() — false = 4B sessionID
            is_new=True,       # bThread.isNew() — true ⇒ payload AES-encrypted
        )
        self.blade_sock.sendall(frame)

    def send_mouse_abs(self, x: int, y: int, buttons: int,
                       wheel: int = 0) -> None:
        """Send absolute mouse coords (0..3000), AES-encrypted.

        Delegates to the validated `kvm_core.pack.mouse_pack_abs`.
        """
        if self.blade_sock is None or self.blade_no is None or self.session is None:
            return
        from ..kvm_core.pack import mouse_pack_abs
        frame = mouse_pack_abs(
            x_3000=x, y_3000=y, buttons=buttons, wheel=wheel,
            blade_no=self.blade_no,
            blade_codekey=self.session.verifyvalue,
            encrypted=False,
            is_new=True,
        )
        self.blade_sock.sendall(frame)


# ----------------------------------------------------------------------
# Adapter for the GUI: yield (img_id, png_bytes), applying XOR for diffs.
# ----------------------------------------------------------------------

def open_live_session(host: str, user: str, password: str, slot: int,
                      verify_tls: bool = False,
                      use_newrle: bool | None = None) -> "KvmClient":
    """Build a `KvmClient`, run the handshake, return it ready to stream.

    Caller owns the lifetime — typically used as a context manager:

        with open_live_session(...) as cli:
            for png in iter_pngs(cli):
                ...

    `use_newrle=True` requests the chassis NewRLE/JPEG codec; `False`
    forces OldRLE. `None` falls back to HMM_KVM_USE_NEWRLE env var
    (default = OldRLE).
    """
    cli = KvmClient(host, user, password, verify_tls=verify_tls,
                    use_newrle=use_newrle)
    try:
        cli.open(slot)
    except Exception:
        cli.close()
        raise
    return cli


def _iter_pngs_newrle(cli: "KvmClient") -> Iterator[tuple[int, bytes]]:
    """NewRLE/JPEG decode path — bypasses the OldRLE BGR233 pipeline.

    The chassis NewRLE codec manages diffs internally via copy-from-tile
    tokens (zip_type 5/6, rZipType 4..7), so there is no need for the
    `is_diff` XOR step that the OldRLE path uses. We just decode each
    payload through the stateful `NewRleDecoder` (preserves tile state
    across frames so copy-from-prev works) and PNG-encode the
    composed canvas.

    Resolution changes are handled inside `NewRleDecoder.decode()` —
    it re-inits its tile grid when (image_width, image_height) changes.
    """
    from PIL import Image as PILImage   # noqa: F401  (used implicitly)

    from ..kvm_core.decoder.image_decoder import NewRleDecoder, compose_frame

    decoder: NewRleDecoder | None = None
    cur_w = cur_h = 0

    n_perf = 0
    perf_decode_ms = 0.0
    perf_encode_ms = 0.0
    perf_total_ms = 0.0
    debug_dumped = 0   # log first few payloads as hex for protocol inspection

    for img_id, w, h, is_diff, data in cli.frames():
        t_start = time.monotonic()
        if decoder is None or cur_w != w or cur_h != h:
            decoder = NewRleDecoder(w, h)
            cur_w, cur_h = w, h
            log.info("KVM(NewRLE) resolution: %dx%d", w, h)
        if debug_dumped < 3:
            head = data[:48].hex()
            log.warning(
                "NewRLE frame #%d (img=0x%02x diff=%s len=%d) head=%s",
                debug_dumped, img_id, is_diff, len(data), head,
            )
            debug_dumped += 1

        try:
            blocks = decoder.decode(data, w, h)
        except Exception as e:
            log.warning(
                "NewRLE decode failed for img 0x%02x (diff=%s, %d B): %s",
                img_id, is_diff, len(data), e,
            )
            continue
        t_decoded = time.monotonic()
        perf_decode_ms += (t_decoded - t_start) * 1000.0

        try:
            frame = compose_frame(blocks, w, h)
            buf = io.BytesIO()
            frame.save(buf, format="PNG", optimize=False, compress_level=1)
            yield img_id, buf.getvalue()
            t_end = time.monotonic()
            perf_encode_ms += (t_end - t_decoded) * 1000.0
            perf_total_ms += (t_end - t_start) * 1000.0
            n_perf += 1
            if n_perf >= 30:
                log.info(
                    "KVM(NewRLE) perf (last 30): decode=%.1fms encode=%.1fms"
                    " total=%.1fms ⇒ ~%.1f fps max",
                    perf_decode_ms / n_perf, perf_encode_ms / n_perf,
                    perf_total_ms / n_perf,
                    1000.0 / max(perf_total_ms / n_perf, 0.01),
                )
                n_perf = 0
                perf_decode_ms = perf_encode_ms = perf_total_ms = 0.0
        except Exception as e:
            log.warning("NewRLE encode failed for img 0x%02x: %s", img_id, e)
            continue


def iter_pngs(cli: "KvmClient") -> Iterator[tuple[int, bytes]]:
    """Decode the live frame stream into PNGs, applying XOR for diffs.

    The Huawei codec emits ~1 keyframe every few seconds plus a stream
    of XOR-deltas in BGR233 space. We keep the last decoded BGR233
    framebuffer per (width, height); for keyframes we replace it, for
    diff frames we XOR in place. Then we always render the persistent
    framebuffer — so every yielded PNG is a complete screen, not a
    partial update.

    Encoding goes through PIL's 8-bit "P" (palette) mode using
    `BGR233_PALETTE`. That keeps the per-pixel work inside libpng — a
    640×480 frame encodes in ~5 ms instead of the ~275 ms an RGB
    expansion + PNG would take.

    When `cli.use_newrle` is True (HMM_KVM_USE_NEWRLE env var), the
    NewRLE/JPEG decode path takes over via `_iter_pngs_newrle`. That
    path manages diffs internally via copy-from-tile tokens, so it
    bypasses the BGR233 framebuffer + XOR pipeline entirely.
    """
    if cli.use_newrle:
        yield from _iter_pngs_newrle(cli)
        return

    from PIL import Image as PILImage

    fb: bytearray | None = None
    fb_w = 0
    fb_h = 0
    # Per-frame timing accumulators; flushed to log every 30 frames so
    # we can see where the live pipeline burns its budget. Helps decide
    # whether the user-visible lag is decoder, XOR, encode, or chassis-
    # side latency.
    n_perf = 0
    perf_decode_ms = 0.0
    perf_xor_ms = 0.0
    perf_encode_ms = 0.0
    perf_total_ms = 0.0
    for img_id, w, h, is_diff, data in cli.frames():
        t_start = time.monotonic()
        try:
            decoded = decode_old_rle(data, w, h)
        except Exception as e:
            log.warning("decode failed for img 0x%02x (diff=%s, %d B): %s",
                        img_id, is_diff, len(data), e)
            continue
        t_decoded = time.monotonic()
        perf_decode_ms += (t_decoded - t_start) * 1000.0
        # Resolution change or first keyframe: reset the persistent buffer.
        if fb is None or fb_w != w or fb_h != h:
            if is_diff:
                # No base to XOR onto at this resolution yet. Drop the
                # frame and wait for the next keyframe at the new size.
                continue
            log.info("KVM resolution: %dx%d (decoded buffer %d B)",
                     w, h, len(decoded))
            fb = bytearray(decoded)
            fb_w, fb_h = w, h
        elif is_diff:
            # XOR-delta in BGR233 space. Going through int.from_bytes /
            # to_bytes lets CPython do the XOR in C, which makes a 640×480
            # framebuffer (300 KB) cost microseconds instead of the tens
            # of milliseconds a Python-level for-loop would.
            n = min(len(fb), len(decoded))
            xored = (int.from_bytes(bytes(fb[:n]), "big")
                     ^ int.from_bytes(decoded[:n], "big")).to_bytes(n, "big")
            fb[:n] = xored
        else:
            fb[:] = decoded
        try:
            # Live chassis screens show text correctly when we render
            # the decoded buffer directly (no row rotation). A live
            # Debian terminal screenshot proved that rotating by W/2
            # produces a fake "two-column" split where each chassis row
            # gets cut in half and the halves displayed in swapped
            # positions — i.e. the ROTATION is what causes the split,
            # not the chassis. Earlier offline tests against captured
            # 720×400 Emulex BIOS frames seemed to need the rotation;
            # that may have been a one-off codec mode quirk that the
            # NewRLE port will clarify. Default OFF for now.
            #   HMM_KVM_ROW_ROTATE=off    — no rotation (default)
            #   HMM_KVM_ROW_ROTATE=always — force W/2 row rotation
            policy = os.environ.get("HMM_KVM_ROW_ROTATE", "off").lower()
            do_rotate = (policy == "always")
            if do_rotate:
                half = fb_w // 2
                rotated = bytearray(fb_w * fb_h)
                for y in range(fb_h):
                    row_start = y * fb_w
                    row_end = row_start + fb_w
                    row = fb[row_start:row_end]
                    rotated[row_start:row_end] = row[half:] + row[:half]
                src_buf = bytes(rotated)
            else:
                src_buf = bytes(fb)
            t_xor = time.monotonic()
            perf_xor_ms += (t_xor - t_decoded) * 1000.0
            pim = PILImage.frombytes("P", (fb_w, fb_h), src_buf)
            pim.putpalette(BGR233_PALETTE)
            buf = io.BytesIO()
            pim.save(buf, format="PNG", optimize=False, compress_level=1)
            yield img_id, buf.getvalue()
            t_end = time.monotonic()
            perf_encode_ms += (t_end - t_xor) * 1000.0
            perf_total_ms += (t_end - t_start) * 1000.0
            n_perf += 1
            if n_perf >= 30:
                log.info(
                    "KVM perf (last 30 frames): decode=%.1fms xor+rotate=%.1fms"
                    " encode+yield=%.1fms total=%.1fms ⇒ ~%.1f fps max",
                    perf_decode_ms / n_perf, perf_xor_ms / n_perf,
                    perf_encode_ms / n_perf, perf_total_ms / n_perf,
                    1000.0 / max(perf_total_ms / n_perf, 0.01),
                )
                n_perf = 0
                perf_decode_ms = perf_xor_ms = perf_encode_ms = perf_total_ms = 0.0
        except Exception as e:
            log.warning("encode failed for img 0x%02x: %s", img_id, e)
            continue


def live_pngs(host: str, user: str, password: str, slot: int,
              verify_tls: bool = False) -> Iterator[tuple[int, bytes]]:
    """Backwards-compatible one-shot helper. Owns the cli lifetime."""
    with open_live_session(host, user, password, slot,
                           verify_tls=verify_tls) as cli:
        yield from iter_pngs(cli)
