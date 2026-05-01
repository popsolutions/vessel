"""VirtualMedia protocol primitives.

Source of truth: docs/kvm-protocol-re.md and the decompiled
com/huawei/vm/console/communication/{ProtocolCode,ProtocolProcessor}.java.

Header is 12 bytes, big-endian. Op code at byte 0; flags at byte 1
(lower nibble: 0 = COMMAND, 1 = DATA for UFI/SFF); byte 2 is the ack
sub-code on op=ACK; byte 3 is the per-stream sequence id; bytes 4-11 are
the trans-field (length / offset depending on op). Heartbeat is just the
12-byte header with op=6, no body.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

FRAME_HEAD_SIZE = 12


class OpCode(IntEnum):
    """Top-level op code (byte 0 of the 12-byte header).

    Mirrors `com.huawei.vm.console.communication.ProtocolCode`.
    Java byte values are signed; we store the unsigned-byte equivalent
    (e.g. SFF_COMMAND_COMPLETE = -1 in Java is 0xFF here).
    """

    ACK = 0
    CERTIFY_ID = 1
    DEVICE_TYPE = 2
    UFI_DATA = 3
    SFF_DATA = 4
    CLOSE_VM = 5
    HEARTBIT = 6
    SHUTDOWN = 7
    MIC_FILE_CMD = 0xFC  # Java: -4
    CONSOLE_PRINT_CONTROLLER = 0xF0  # Java: -16
    SFF_COMMAND_COMPLETE = 0xFF  # Java: -1
    UFI_COMMAND_COMPLETE = 0xFE  # Java: -2


class AckCode(IntEnum):
    """ACK sub-code (byte 2 when op=ACK)."""

    CERTIFY_PASS = 0
    CERTIFY_ID_FAIL = 1
    CERTIFY_VER_NOTSUP = 2
    DEVICE_CREAT = 16
    DEVICE_FAIL_ENUM = 17
    CLOSE_DEVICE_RM = 33
    CLOSE_UPDATA = 34
    CLOSE_IPCONFIG = 35
    MIC_SENT = 36
    CN_EXIST = 49  # "connection exists" — vmedia session already active for this blade


class DeviceType(IntEnum):
    """Body of DEVICE_TYPE frames."""

    FLOPPY = 1
    CDROM = 2
    MULTI = 3


class CloseReason(IntEnum):
    """Body of CLOSE_VM frames."""

    LINK = 0
    FLOPPY = 1
    CDROM = 2


# Sub-types in lower nibble of byte 1 for UFI/SFF data
SUB_COMMAND = 0  # CDB
SUB_DATA = 1  # data after CDB
SUB_END = 3
SUB_CMD_OK = 0
SUB_CMD_FAIL = 1


@dataclass(frozen=True)
class Header:
    op: int
    flags: int
    ack_or_sub: int
    seq_id: int
    trans_field: bytes  # 8 bytes

    def is_ack(self) -> bool:
        return self.op == OpCode.ACK


def pack_header(
    op: int,
    flags: int = 0,
    ack_or_sub: int = 0,
    seq_id: int = 0,
    trans_field: bytes = b"\x00" * 8,
) -> bytes:
    if len(trans_field) != 8:
        raise ValueError(f"trans_field must be 8 bytes, got {len(trans_field)}")
    return bytes([op & 0xFF, flags & 0xFF, ack_or_sub & 0xFF, seq_id & 0xFF]) + trans_field


def parse_header(buf: bytes) -> Header:
    if len(buf) != FRAME_HEAD_SIZE:
        raise ValueError(f"header must be {FRAME_HEAD_SIZE} bytes, got {len(buf)}")
    return Header(
        op=buf[0],
        flags=buf[1],
        ack_or_sub=buf[2],
        seq_id=buf[3],
        trans_field=bytes(buf[4:12]),
    )


def heartbeat_frame() -> bytes:
    """12-byte heartbeat (op=HEARTBIT, no body)."""
    return pack_header(OpCode.HEARTBIT)


def shutdown_frame() -> bytes:
    return pack_header(OpCode.SHUTDOWN)


def device_type_frame(device: DeviceType) -> bytes:
    """DEVICE_TYPE: header + 1-byte body (the device enum).

    The 12-byte header is the canonical "frame" but we tack on the body
    here for callers that just want one buffer to send. Body length is
    encoded in bytes 4-7 of the trans field (int32 BE).
    """
    body = bytes([int(device)])
    trans = len(body).to_bytes(4, "big") + b"\x00\x00\x00\x00"
    return pack_header(OpCode.DEVICE_TYPE, trans_field=trans) + body


def close_frame(reason: CloseReason) -> bytes:
    body = bytes([int(reason)])
    trans = len(body).to_bytes(4, "big") + b"\x00\x00\x00\x00"
    return pack_header(OpCode.CLOSE_VM, trans_field=trans) + body
