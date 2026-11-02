"""Data model for the offline SUP compatibility solver.

The layouts and claim kinds here are taken from the loaders themselves:

* ``firmware/work/fpSup-lexpo/fp_usb_shell/v3/sloader.h`` -- ``FSB1``,
  32-byte header, ``SL_CHAIN``/``SL_EXCL``/``SL_SHARED_UI``.
* ``vendor/fpSup/fpSup/loader-v4/sloader4.h`` -- ``FSB4``, 48-byte header,
  service version 7.

A sup header does *not* declare its claims: claims are made at run time through
``svc->claim``/``svc->claim_res``.  :class:`Claim` therefore carries a
``source`` so a solver can tell a claim declared by build metadata from one
observed in ``LOAD.LOG``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

FORMAT_FSB1 = "FSB1"
FORMAT_FSB4 = "FSB4"
FORMAT_UNKNOWN = "unknown"

# sloader.h: enum { SL_CHAIN = 1, SL_EXCL = 2, SL_SHARED_UI = 3 };
# 0 is our "kind not recorded" value (LOAD.LOG's CLAIM line has no kind).
KIND_UNKNOWN = 0
SL_CHAIN = 1
SL_EXCL = 2
SL_SHARED_UI = 3

KIND_NAMES = {
    KIND_UNKNOWN: "UNKNOWN",
    SL_CHAIN: "CHAIN",
    SL_EXCL: "EXCL",
    SL_SHARED_UI: "SHARED_UI",
}

_KIND_BY_NAME = {
    "CHAIN": SL_CHAIN,
    "SL_CHAIN": SL_CHAIN,
    "EXCL": SL_EXCL,
    "SL_EXCL": SL_EXCL,
    "SHARED_UI": SL_SHARED_UI,
    "SL_SHARED_UI": SL_SHARED_UI,
}

# SupInfo.status values.
STATUS_OK = "ok"
STATUS_LOADED = "loaded"
STATUS_RELEASED = "released"
STATUS_PENDING = "pending"
STATUS_LOG_ONLY = "log_only"
STATUS_BAD_HEADER = "bad_header"
STATUS_OLD_FORMAT = "old_format"
STATUS_DISABLED = "disabled"
STATUS_DUPLICATE = "duplicate"


def kind_from(value: Any, default: int = SL_EXCL) -> int:
    """Normalise an integer or a name such as ``"EXCL"`` to a claim kind."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _KIND_BY_NAME.get(value.strip().upper(), default)
    return default


def kind_name(kind: int) -> str:
    return KIND_NAMES.get(kind, f"KIND_{kind}")


def sup_id_text(sup_id: int) -> str:
    """Four-character sup_id, or hex when the bytes are not printable ASCII.

    ``sup_id`` is a little-endian uint32: ``struct.unpack('<I', b'LXPO')`` is
    the value the loaders store, so decoding packs it back little-endian.
    """
    if not sup_id:
        return "0000"
    raw = struct.pack("<I", sup_id & 0xFFFFFFFF)
    if all(0x20 <= byte <= 0x7E for byte in raw):
        return raw.decode("ascii")
    return f"0x{sup_id:08X}"


@dataclass(frozen=True)
class Claim:
    """One claim on a firmware range (or a resource when ``len`` is 0).

    ``length_known`` is false for a claim observed in ``LOAD.LOG``: the v3 log
    records only the claim's first address (``L_CLAIM``), not its length.
    """

    addr: int
    len: int
    kind: int = KIND_UNKNOWN
    source: str = "header"
    length_known: bool = True

    @property
    def kind_name(self) -> str:
        return kind_name(self.kind)

    @property
    def end(self) -> int:
        return self.addr + max(self.len, 0)

    def covers(self, addr: int) -> bool:
        if self.len <= 0:
            return addr == self.addr
        return self.addr <= addr < self.addr + self.len

    def to_dict(self) -> dict[str, Any]:
        return {
            "addr": self.addr,
            "addr_hex": f"0x{self.addr:08X}",
            "len": self.len,
            "kind": self.kind,
            "kind_name": self.kind_name,
            "source": self.source,
            "length_known": self.length_known,
        }


@dataclass
class SupInfo:
    """One ``\\fpSup\\*.BIN`` file, plus what a LOAD.LOG says about it."""

    path: str
    name: str
    sup_id: int
    format: str = FORMAT_UNKNOWN
    version: int | None = None
    min_svc: int | None = None
    claims: list[Claim] = field(default_factory=list)
    status: str = STATUS_PENDING
    loaded: bool = False
    released: bool = False
    conflicts: list[str] = field(default_factory=list)

    # Extra detail, useful to ``graph``/``explain`` and to the solver.
    display_name: str | None = None
    has_header: bool = True
    size: int = 0
    header_len: int | None = None
    file_len: int | None = None
    block_size: int | None = None
    entry_off: int | None = None
    flags: int | None = None
    dma_off: int | None = None
    dma_len: int | None = None
    name_off: int | None = None
    problems: list[str] = field(default_factory=list)
    load_count: int = 0
    release_count: int = 0

    @property
    def sup_id_text(self) -> str:
        return sup_id_text(self.sup_id)

    @property
    def label(self) -> str:
        """Display name when the FSB4 header carries one, else the file name."""
        return self.display_name or self.name or self.path

    def loader_accepts(self, loader_version: int) -> bool:
        """Whether this file's format is loadable by that service version.

        Verified behaviour: v3 (service <= 6) refuses ``FSB4``; v4
        (service >= 7) logs ``OLD_FORMAT`` and refuses ``FSB1``.
        """
        if self.format == FORMAT_FSB1:
            return loader_version <= 6
        if self.format == FORMAT_FSB4:
            return loader_version >= 7
        return False

    def is_old_format_for(self, loader_version: int) -> bool:
        """True when a loader would call this file the older format."""
        return loader_version >= 7 and self.format == FORMAT_FSB1

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "display_name": self.display_name,
            "label": self.label,
            "sup_id": self.sup_id,
            "sup_id_text": self.sup_id_text,
            "format": self.format,
            "version": self.version,
            "min_svc": self.min_svc,
            "status": self.status,
            "loaded": self.loaded,
            "released": self.released,
            "load_count": self.load_count,
            "release_count": self.release_count,
            "has_header": self.has_header,
            "size": self.size,
            "header_len": self.header_len,
            "file_len": self.file_len,
            "block_size": self.block_size,
            "entry_off": self.entry_off,
            "flags": self.flags,
            "dma_off": self.dma_off,
            "dma_len": self.dma_len,
            "name_off": self.name_off,
            "claims": [claim.to_dict() for claim in self.claims],
            "conflicts": list(self.conflicts),
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class Issue:
    """A structured finding shared by the graph and the solver."""

    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    sups: tuple[str, ...] = ()

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "sups": list(self.sups),
        }
