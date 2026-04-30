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
from .codec_old import bgr233_to_rgb888, decode_old_rle

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
                 verify_tls: bool = False) -> None:
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
        self._send_connect_blade(slot, color_bit=0, fpeg_alg=False)
        self._send_contr_rate(DEFAULT_FRAMERATE)
        self._send_connect_blade(slot, color_bit=0, fpeg_alg=False)
        self._send_monitor_blade(slot)

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
        """Body: [bladeNO, colorBit, fpegAlg]; 4-byte verifyvalue sessionID."""
        assert self.blade_sock is not None
        body = bytes([blade_no & 0xFF, color_bit & 0xFF, 1 if fpeg_alg else 0])
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

    def _heartbeat_loop(self) -> None:
        """Heartbeat both sockets every ~2s."""
        while not self._stop.is_set():
            try:
                # SMM heartbeat carries 1-byte payload per captured pcap
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
    def frames(self) -> Iterator[tuple[int, int, int, bytes]]:
        """Yield (img_id, width, height, encoded_data) tuples — real frames only.

        The chassis sends ~99% sentinel/keepalive deltas (total=5,
        w=33408, h=480). We drop those — the browser keeps the last
        real keyframe on its canvas.
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
            chunk_no = p[2]
            img_id = p[3]
            if chunk_no == 0:
                if len(p) < 18:
                    continue
                total = int.from_bytes(p[4:8], "big")
                w = int.from_bytes(p[8:10], "big")
                h = int.from_bytes(p[10:12], "big")
                # Drop sentinels: tiny payload OR absurd dimensions
                if total < 100 or w == 0 or h == 0 or w > 4096 or h > 4096:
                    continue
                partial[img_id] = {"data": bytearray(), "total": total,
                                   "w": w, "h": h}
            else:
                slot = partial.get(img_id)
                if slot is None:
                    continue
                slot["data"].extend(p[4:])
                if len(slot["data"]) >= slot["total"]:
                    yield (img_id, slot["w"], slot["h"],
                           bytes(slot["data"][:slot["total"]]))
                    partial.pop(img_id, None)

    # ------------------------------------------------------------------
    # Input — preliminary; will be wired into the WS in a follow-up.
    # ------------------------------------------------------------------
    def send_key(self, scancode: int, pressed: bool) -> None:
        body = bytes([scancode & 0xFF, 1 if pressed else 0])
        if self.blade_sock is None:
            return
        self.blade_sock.sendall(pack_kvm_frame(
            OP_KEY_PACK, body, self._blade_sid(), secure=False))

    def send_mouse(self, dx: int, dy: int, buttons: int) -> None:
        body = bytes([dx & 0xFF, dy & 0xFF, buttons & 0xFF,
                      (self.blade_no or 0) & 0xFF])
        if self.blade_sock is None:
            return
        self.blade_sock.sendall(pack_kvm_frame(
            OP_MOUSE_PACK, body, self._blade_sid(), secure=False))


# ----------------------------------------------------------------------
# Adapter for the GUI: yield (img_id, png_bytes).
# ----------------------------------------------------------------------

def live_pngs(host: str, user: str, password: str, slot: int,
              verify_tls: bool = False) -> Iterator[tuple[int, bytes]]:
    """Yield (img_id, png_bytes) for live blade frames.

    Owns the `KvmClient` lifetime; closes on exit.
    """
    from PIL import Image as PILImage

    cli = KvmClient(host, user, password, verify_tls=verify_tls)
    with cli:
        cli.open(slot)
        for img_id, w, h, data in cli.frames():
            try:
                fb = decode_old_rle(data, w, h)
                rgb = bgr233_to_rgb888(fb)
                pim = PILImage.frombytes("RGB", (w, h), rgb)
                buf = io.BytesIO()
                pim.save(buf, format="PNG", optimize=False)
                yield img_id, buf.getvalue()
            except Exception as e:
                log.warning("decode/encode failed for img 0x%02x: %s",
                            img_id, e)
                continue
