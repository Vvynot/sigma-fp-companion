"""Read a SUP ``*.BIN`` header offline; no camera and no SD card needed.

``FSB1`` (32 bytes, v3) and ``FSB4`` (48 bytes, v4) layouts are the structs in
``sloader.h``/``sloader4.h``.  Header checks mirror the loaders' ``header_ok``
(``sloader.c``/``sloader4.c``) with one deliberate exception: ``min_svc`` is
*recorded*, not rejected here.  Compatibility of that field belongs to the
solver, which knows which loader is being targeted.

Neither header carries a claim table; claims are run-time calls to
``svc->claim``.  This module therefore never invents claims.
"""

from __future__ import annotations

import struct
from pathlib import Path

from .model import (
    FORMAT_FSB1,
    FORMAT_FSB4,
    FORMAT_UNKNOWN,
    STATUS_BAD_HEADER,
    STATUS_OK,
    SupInfo,
)

SL_MAGIC = 0x31425346  # "FSB1" (sloader.h)
SL4_MAGIC = 0x34425346  # "FSB4" (sloader4.h)
SL_HEADER_LEN = 32
SL4_HEADER_LEN = 48
MAX_BLOCK = 0x01000000  # sloader.c / sloader4.c MAX_BLOCK

_FSB1 = struct.Struct("<8I")
_FSB4 = struct.Struct("<12I")


def parse_sup_file(path: str | Path) -> SupInfo:
    file_path = Path(path)
    return parse_sup_bytes(
        file_path.read_bytes(), path=str(file_path), name=file_path.name
    )


def parse_sup_bytes(
    data: bytes, *, path: str = "", name: str = ""
) -> SupInfo:
    """Parse one sup image.  Never raises for a malformed header."""
    if not name and path:
        name = Path(path).name
    sup = SupInfo(path=path or name, name=name, sup_id=0, size=len(data))
    if len(data) < 4:
        sup.status = STATUS_BAD_HEADER
        sup.problems.append(f"file is {len(data)} byte(s): shorter than a magic")
        return sup

    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == SL_MAGIC:
        return _parse_fsb1(data, sup)
    if magic == SL4_MAGIC:
        return _parse_fsb4(data, sup)

    sup.format = FORMAT_UNKNOWN
    sup.status = STATUS_BAD_HEADER
    sup.problems.append(
        f"unknown magic 0x{magic:08X} (expected FSB1 0x{SL_MAGIC:08X} "
        f"or FSB4 0x{SL4_MAGIC:08X})"
    )
    return sup


def _finish(sup: SupInfo) -> SupInfo:
    sup.status = STATUS_OK if not sup.problems else STATUS_BAD_HEADER
    return sup


def _parse_fsb1(data: bytes, sup: SupInfo) -> SupInfo:
    sup.format = FORMAT_FSB1
    if len(data) < SL_HEADER_LEN:
        sup.status = STATUS_BAD_HEADER
        sup.problems.append(
            f"truncated FSB1 header: {len(data)} of {SL_HEADER_LEN} bytes"
        )
        return sup

    (
        magic,
        header_len,
        file_len,
        block_size,
        entry_off,
        sup_id,
        version,
        min_svc,
    ) = _FSB1.unpack_from(data, 0)

    sup.sup_id = sup_id
    sup.version = version
    sup.min_svc = min_svc
    sup.header_len = header_len
    sup.file_len = file_len
    sup.block_size = block_size
    sup.entry_off = entry_off

    if header_len != SL_HEADER_LEN:
        sup.problems.append(f"header_len {header_len} != {SL_HEADER_LEN}")
    if file_len != len(data):
        sup.problems.append(f"file_len {file_len} != actual size {len(data)}")
    if file_len < SL_HEADER_LEN:
        sup.problems.append(f"file_len {file_len} is smaller than the header")
    if block_size < file_len:
        sup.problems.append(f"block_size {block_size} < file_len {file_len}")
    if block_size > MAX_BLOCK:
        sup.problems.append(f"block_size {block_size} > MAX_BLOCK {MAX_BLOCK}")

    _check_entry(sup, entry_off, file_len, SL_HEADER_LEN)
    return _finish(sup)


def _parse_fsb4(data: bytes, sup: SupInfo) -> SupInfo:
    sup.format = FORMAT_FSB4
    if len(data) < SL4_HEADER_LEN:
        sup.status = STATUS_BAD_HEADER
        sup.problems.append(
            f"truncated FSB4 header: {len(data)} of {SL4_HEADER_LEN} bytes"
        )
        return sup

    (
        magic,
        header_len,
        file_len,
        block_size,
        entry_off,
        sup_id,
        version,
        min_svc,
        flags,
        dma_off,
        dma_len,
        name_off,
    ) = _FSB4.unpack_from(data, 0)

    sup.sup_id = sup_id
    sup.version = version
    sup.min_svc = min_svc
    sup.header_len = header_len
    sup.file_len = file_len
    sup.block_size = block_size
    sup.entry_off = entry_off
    sup.flags = flags
    sup.dma_off = dma_off
    sup.dma_len = dma_len
    sup.name_off = name_off

    if header_len != SL4_HEADER_LEN:
        sup.problems.append(f"header_len {header_len} != {SL4_HEADER_LEN}")
    if file_len != len(data):
        sup.problems.append(f"file_len {file_len} != actual size {len(data)}")
    if file_len < SL4_HEADER_LEN:
        sup.problems.append(f"file_len {file_len} is smaller than the header")
    if block_size < file_len:
        sup.problems.append(f"block_size {block_size} < file_len {file_len}")
    if block_size > MAX_BLOCK:
        sup.problems.append(f"block_size {block_size} > MAX_BLOCK {MAX_BLOCK}")

    _check_entry(sup, entry_off, file_len, SL4_HEADER_LEN)

    if dma_len and (
        dma_off + dma_len > block_size or dma_off + dma_len <= dma_off
    ):
        sup.problems.append(
            f"DMA range 0x{dma_off:08X}+0x{dma_len:X} escapes block {block_size}"
        )
    if name_off:
        if name_off >= len(data):
            sup.problems.append(
                f"name_off 0x{name_off:X} is past the file ({len(data)} bytes)"
            )
        elif file_len and name_off >= file_len:
            sup.problems.append(
                f"name_off 0x{name_off:X} is past file_len {file_len}"
            )
        else:
            end = data.find(b"\0", name_off)
            if end < 0:
                end = len(data)
            raw = data[name_off:end]
            decoded = raw.decode("utf-8", errors="replace").strip()
            sup.display_name = decoded or None
    return _finish(sup)


def _check_entry(
    sup: SupInfo, entry_off: int, file_len: int, header_len: int
) -> None:
    """Mirror ``header_ok``'s entry rules (bit 0 = Thumb)."""
    entry = entry_off & ~1
    if not (header_len <= entry < file_len):
        sup.problems.append(
            f"entry_off 0x{entry_off:X} (entry 0x{entry:X}) outside "
            f"[{header_len}, {file_len})"
        )
    if not (entry_off & 1) and entry & 3:
        sup.problems.append(
            f"entry_off 0x{entry_off:X} is ARM-aligned but not word-aligned"
        )
