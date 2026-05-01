"""ISO file backing for the SFF-8020i SCSI loop.

Memory-maps an .iso file and serves 2048-byte sectors. Implements the
`IsoBacking` Protocol from `sff8020i.py`.
"""

from __future__ import annotations

import mmap
import os
from pathlib import Path

CDROM_BLOCK_SIZE = 2048


class IsoFileBacking:
    """Memory-mapped ISO file. Use as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._mm: mmap.mmap | None = None
        self._size: int = 0

    def __enter__(self) -> "IsoFileBacking":
        self._fd = os.open(str(self.path), os.O_RDONLY)
        st = os.fstat(self._fd)
        self._size = st.st_size
        if self._size == 0:
            os.close(self._fd)
            self._fd = None
            raise ValueError(f"empty file: {self.path}")
        self._mm = mmap.mmap(self._fd, 0, prot=mmap.PROT_READ)
        return self

    def __exit__(self, *_: object) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def size_bytes(self) -> int:
        return self._size

    @property
    def lba_count(self) -> int:
        # Round UP so the last partial block (if any) is reachable; pad with zeros on read.
        return (self._size + CDROM_BLOCK_SIZE - 1) // CDROM_BLOCK_SIZE

    def read_lba(self, lba: int, count: int) -> bytes:
        if self._mm is None:
            raise RuntimeError("backing not open; use as context manager")
        if count <= 0:
            return b""
        start = lba * CDROM_BLOCK_SIZE
        end = start + count * CDROM_BLOCK_SIZE
        if start >= self._size:
            return b"\x00" * (count * CDROM_BLOCK_SIZE)
        if end <= self._size:
            return bytes(self._mm[start:end])
        return bytes(self._mm[start : self._size]) + b"\x00" * (end - self._size)
