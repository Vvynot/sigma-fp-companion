"""Parse ``\\fpSup\\LOAD.LOG`` into named events.

The record codes are the loaders' own enums, not a guess:

* v3 ``firmware/work/fpSup-lexpo/fp_usb_shell/v3/sloader.c`` has ``L_START``
  through ``L_HOTLOAD`` (1 .. 0x15).
* v4 ``vendor/fpSup/fpSup/loader-v4/sloader4.c`` keeps v3's codes and adds
  ``L_OLD_FORMAT`` at 0x14 plus, in some builds, ``L_PWRFILE`` (0x31) and
  ``L_UI`` (0x32).

Each line is ``"cc aaaaaaaa bbbbbbbb text"``; v3 pads the file to 4096 bytes
with spaces, v4 to 8192.  ``0x14``/``0x15`` are ambiguous between the two
enums, so :func:`resolve_code` uses the v4 ``OLD_FORMAT`` signature
(``a == "FSB1"``) when it can and otherwise the selected family.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SL3_MAGIC = 0x31425346  # "FSB1"
SL4_MAGIC = 0x34425346  # "FSB4"

# Codes common to v3 and v4 (sloader.c / sloader4.c).
COMMON_CODES: dict[int, str] = {
    0x01: "START",
    0x02: "NO_DIR",
    0x03: "SKIP_NAME",
    0x04: "TOO_MANY",
    0x05: "OPEN_FAIL",
    0x06: "BAD_HEADER",
    0x07: "DUP_ID",
    0x08: "NO_MEMORY",
    0x09: "SHORT_READ",
    0x0A: "LOADED",
    0x0B: "RELEASED",
    0x0C: "REPAIRED",
    0x0D: "SUP",
    0x0E: "DONE",
    0x0F: "NO_POFF",
    0x10: "CONFLICT",
    0x11: "LISTED",
    0x12: "CLAIM",
    0x13: "DISABLED",
}

V3_CODES: dict[int, str] = {
    **COMMON_CODES,
    0x14: "UNLOAD",
    0x15: "HOTLOAD",
}

V4_CODES: dict[int, str] = {
    **COMMON_CODES,
    0x14: "OLD_FORMAT",
    0x15: "PWRLOG",
    0x31: "PWRFILE",
    0x32: "UI",
}

V3_ONLY = {0x14, 0x15}
V4_ONLY = {0x31, 0x32}

_LINE = re.compile(
    r"^\s*([0-9A-Fa-f]{1,2})\s+([0-9A-Fa-f]{1,8})\s+([0-9A-Fa-f]{1,8})"
    r"(?:\s+(.*))?\s*$"
)


@dataclass(frozen=True)
class LoadEvent:
    """One line of LOAD.LOG, decoded."""

    code: int
    name: str
    a: int
    b: int
    text: str = ""
    line: int = 0
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "code_hex": f"{self.code:02X}",
            "name": self.name,
            "a": self.a,
            "a_hex": f"0x{self.a:08X}",
            "b": self.b,
            "b_hex": f"0x{self.b:08X}",
            "text": self.text,
            "line": self.line,
        }


def resolve_code(
    code: int, *, a: int = 0, b: int = 0, family: str = "auto"
) -> str:
    """Return the symbolic name for one record code.

    ``family`` is ``"v3"``, ``"v4"`` or ``"auto"``.  ``0x14`` is
    ``OLD_FORMAT`` when its ``a`` is the FSB1 magic with ``b == 0`` (the v4
    signature), else ``UNLOAD`` (v3).  ``0x15`` is ``PWRLOG`` only when the
    caller says the log is v4; otherwise it is v3's ``HOTLOAD``.
    """
    family = (family or "auto").lower()
    if code in COMMON_CODES:
        return COMMON_CODES[code]
    if code == 0x14:
        if (a == SL3_MAGIC and b == 0) or family == "v4":
            return "OLD_FORMAT"
        return "UNLOAD"
    if code == 0x15:
        return "PWRLOG" if family == "v4" else "HOTLOAD"
    if code in V4_ONLY:
        return V4_CODES[code]
    return f"UNKNOWN_{code:02X}"


def _decode(source: str | bytes | Path) -> str:
    if isinstance(source, Path):
        return source.read_bytes().decode("latin-1", errors="replace")
    if isinstance(source, (bytes, bytearray)):
        return bytes(source).decode("latin-1", errors="replace")
    if not isinstance(source, str):
        raise TypeError(f"LOAD.LOG source must be str, bytes or Path, not {type(source)!r}")
    # A bare string may be a path.  Logs always contain newlines, so avoid
    # Path() on a long multi-line blob (invalid characters on Windows).
    if "\n" not in source and "\r" not in source and len(source) < 4096:
        try:
            path = Path(source)
            if path.is_file():
                return path.read_bytes().decode("latin-1", errors="replace")
        except (OSError, ValueError):
            pass
    return source


def parse_loadlog(
    source: str | bytes | Path, *, family: str = "auto"
) -> list[LoadEvent]:
    """Parse LOAD.LOG text, bytes or a path into :class:`LoadEvent` objects.

    Fixed-size padding (4096 bytes in v3, 8192 in v4), trailing spaces and
    lines that are only padding are ignored.  An unparseable line is skipped
    rather than failing the whole log: the firmware writes fixed-width records
    but a partly written tail can exist.
    """
    text = _decode(source)
    events: list[LoadEvent] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        match = _LINE.match(raw)
        if match is None:
            continue
        code = int(match.group(1), 16)
        a = int(match.group(2), 16)
        b = int(match.group(3), 16)
        body = (match.group(4) or "").rstrip()
        events.append(
            LoadEvent(
                code=code,
                name=resolve_code(code, a=a, b=b, family=family),
                a=a,
                b=b,
                text=body,
                line=lineno,
                raw=raw.rstrip(),
            )
        )
    return events


def parse_loadlog_file(
    path: str | Path, *, family: str = "auto"
) -> list[LoadEvent]:
    return parse_loadlog(Path(path), family=family)


def detect_family(events: Iterable[LoadEvent]) -> str:
    """Best-effort ``"v3"``/``"v4"``/``"unknown"`` from the codes present."""
    names = {event.name for event in events}
    if names & {"OLD_FORMAT", "PWRLOG", "PWRFILE", "UI"}:
        return "v4"
    if names & {"UNLOAD", "HOTLOAD"}:
        return "v3"
    return "unknown"


def event_summary(events: Iterable[LoadEvent]) -> dict[str, int]:
    """Count events by symbolic name, for ``graph``/``explain`` output."""
    counts: dict[str, int] = {}
    for event in events:
        counts[event.name] = counts.get(event.name, 0) + 1
    return counts
