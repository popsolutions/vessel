"""SFF-8020i / ATAPI SCSI command parsing and response generation.

The iBMC plays the SCSI **initiator** (sends CDBs); we play the **target**
(emulating a CDROM). Each CDB arrives in an SFF_DATA frame (op 0x04, flags
lower-nibble = 0 = COMMAND); we respond with one or more SFF_DATA frames
(flags lower-nibble = 1 = DATA), then SFF_COMMAND_COMPLETE (op 0xFF).

Frames built here carry only the BODY (CRC isn't used in the VM stream — the
proto.py 12-byte header has its own length field). Wrap with proto.pack_header
+ trans_field length for the wire.

This module is stateless about the disc — pass an `IsoBacking` from
`cdrom_iso.py` for read operations.
"""

from __future__ import annotations

import struct
from typing import Protocol

# SCSI / ATAPI opcodes (subset; see SFF-8020i)
TEST_UNIT_READY = 0x00
REQUEST_SENSE = 0x03
INQUIRY = 0x12
START_STOP_UNIT = 0x1B
PREVENT_ALLOW_MEDIUM_REMOVAL = 0x1E
READ_CAPACITY = 0x25
READ_10 = 0x28
READ_TOC = 0x43
GET_CONFIGURATION = 0x46
GET_EVENT_STATUS_NOTIFICATION = 0x4A
MODE_SENSE_6 = 0x1A
MODE_SENSE_10 = 0x5A
READ_DISC_INFORMATION = 0x51

CDROM_BLOCK_SIZE = 2048


class IsoBacking(Protocol):
    """Anything that can serve READ(10) requests."""

    @property
    def lba_count(self) -> int: ...
    def read_lba(self, lba: int, count: int) -> bytes: ...


def parse_cdb(cdb: bytes) -> tuple[int, dict[str, int]]:
    """Decode a SCSI CDB. Returns (opcode, fields)."""
    if not cdb:
        return 0, {}
    op = cdb[0]
    f: dict[str, int] = {"opcode": op}
    if op == INQUIRY:
        f["evpd"] = cdb[1] & 1 if len(cdb) > 1 else 0
        f["page_code"] = cdb[2] if len(cdb) > 2 else 0
        f["alloc_len"] = (cdb[3] << 8) | cdb[4] if len(cdb) > 4 else 0
    elif op == READ_CAPACITY:
        f["lba"] = (cdb[2] << 24) | (cdb[3] << 16) | (cdb[4] << 8) | cdb[5] if len(cdb) > 5 else 0
        f["pmi"] = cdb[8] & 1 if len(cdb) > 8 else 0
    elif op == READ_10:
        f["lba"] = (cdb[2] << 24) | (cdb[3] << 16) | (cdb[4] << 8) | cdb[5] if len(cdb) > 5 else 0
        f["transfer_len"] = (cdb[7] << 8) | cdb[8] if len(cdb) > 8 else 0
    elif op == READ_TOC:
        f["msf"] = (cdb[1] >> 1) & 1 if len(cdb) > 1 else 0
        f["format"] = cdb[2] & 0x0F if len(cdb) > 2 else 0
        f["start_track"] = cdb[6] if len(cdb) > 6 else 0
        f["alloc_len"] = (cdb[7] << 8) | cdb[8] if len(cdb) > 8 else 0
    elif op == MODE_SENSE_6:
        f["page_code"] = cdb[2] & 0x3F if len(cdb) > 2 else 0
        f["alloc_len"] = cdb[4] if len(cdb) > 4 else 0
    elif op == MODE_SENSE_10:
        f["page_code"] = cdb[2] & 0x3F if len(cdb) > 2 else 0
        f["alloc_len"] = (cdb[7] << 8) | cdb[8] if len(cdb) > 8 else 0
    return op, f


def make_inquiry_response(
    vendor: str = "Virtual ",
    product: str = "DVD-ROM VM 1.1.0",
    rev: str = " 225",
    alloc_len: int = 36,
) -> bytes:
    """Build a 36-byte standard INQUIRY response for a CDROM (matches Palemoon)."""
    buf = bytearray(36)
    buf[0] = 0x05  # PERIPHERAL_DEVICE_TYPE = CD/DVD-ROM
    buf[1] = 0x80  # RMB = 1 (removable medium)
    buf[2] = 0x00  # version
    buf[3] = 0x21  # response data format = 2 (per Palemoon's 0x21)
    buf[4] = 0x1F  # additional length = 31 (total = 36)
    buf[5] = 0x00
    buf[6] = 0x00
    buf[7] = 0x00
    v = (vendor + " " * 8)[:8].encode("ascii")
    buf[8:16] = v
    p = (product + " " * 16)[:16].encode("ascii")
    buf[16:32] = p
    r = (rev + " " * 4)[:4].encode("ascii")
    buf[32:36] = r
    return bytes(buf[:alloc_len])


def make_read_capacity_response(lba_count: int, block_size: int = CDROM_BLOCK_SIZE) -> bytes:
    """8-byte READ_CAPACITY(10) response: last-LBA (BE32) + block-size (BE32)."""
    last_lba = max(0, lba_count - 1)
    return struct.pack(">II", last_lba, block_size)


def make_read_toc_response(lba_count: int, msf: bool = False, format_: int = 0) -> bytes:
    """Minimal READ_TOC response for a single-track data CD/DVD.

    Format 0 (formatted TOC):
      [TOC data length BE16][first track][last track]
        per-track: [reserved][ADR/CTRL][track#][reserved][LBA BE32]
      AA = lead-out
    """
    if format_ != 0:
        return b"\x00\x0a\x01\x01" + b"\x00" * 8
    last_track = 0xAA
    track1_ctrl = 0x14  # ADR=1 (CDROM), CTRL=4 (data)
    if msf:

        def to_msf(lba: int) -> bytes:
            f = lba % 75
            s = (lba // 75) % 60
            m = lba // 75 // 60
            return bytes([0, m, s, f])

        track_data = bytes([0, track1_ctrl, 1, 0]) + to_msf(0)
        leadout = bytes([0, 0x14, last_track, 0]) + to_msf(lba_count)
    else:
        track_data = bytes([0, track1_ctrl, 1, 0]) + struct.pack(">I", 0)
        leadout = bytes([0, 0x14, last_track, 0]) + struct.pack(">I", lba_count)
    body = track_data + leadout
    header = struct.pack(">H", len(body) + 2) + bytes([1, 1])
    return header + body


def make_mode_sense_response(page_code: int, alloc_len: int = 8) -> bytes:
    """Minimal MODE_SENSE response — 4-byte mode header + zeros."""
    header = bytes([3, 0, 0, 0])
    return (header + b"\x00" * max(0, alloc_len - 4))[:alloc_len]


def make_test_unit_ready_response() -> bytes:
    """TEST_UNIT_READY: 0-byte data, just success at the COMMAND_COMPLETE level."""
    return b""


def make_read_response(backing: IsoBacking, lba: int, count: int) -> bytes:
    """READ(10) → return count*2048 bytes from the backing."""
    return backing.read_lba(lba, count)


def opcode_name(op: int) -> str:
    return {
        TEST_UNIT_READY: "TEST_UNIT_READY",
        REQUEST_SENSE: "REQUEST_SENSE",
        INQUIRY: "INQUIRY",
        START_STOP_UNIT: "START_STOP_UNIT",
        PREVENT_ALLOW_MEDIUM_REMOVAL: "PREVENT_ALLOW_MEDIUM_REMOVAL",
        READ_CAPACITY: "READ_CAPACITY",
        READ_10: "READ_10",
        READ_TOC: "READ_TOC",
        MODE_SENSE_6: "MODE_SENSE_6",
        MODE_SENSE_10: "MODE_SENSE_10",
        GET_CONFIGURATION: "GET_CONFIGURATION",
        GET_EVENT_STATUS_NOTIFICATION: "GET_EVENT_STATUS_NOTIFICATION",
        READ_DISC_INFORMATION: "READ_DISC_INFORMATION",
    }.get(op, f"OP_0x{op:02X}")
