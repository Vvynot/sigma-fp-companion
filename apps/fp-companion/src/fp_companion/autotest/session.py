"""USB transport for the SIGMA fp fpSup shell (FPSH v1).

This reuses the frame protocol from ``firmware/tools/fpsh_client.py``:

* interface 0, bulk OUT ``0x01`` / bulk IN ``0x82``
* 64-byte little-endian frame, CRC32 over the first 64 bytes with the
  checksum field zeroed
* replies are matched by sequence number, stale frames are skipped

Only the shell command path (``CMD_SHL``) is exposed here.  The harness is
read-oriented by design but the shell itself is not restricted; callers own
the safety of the lines they send.
"""

from __future__ import annotations

import errno
import os
import random
import re
import struct
import sys
import time
import zlib

# Make direct runs with the env's python.exe work: pyusb needs libusb-1.0.dll,
# which conda ships under Library/bin.  Adding the directory both to the DLL
# search path (3.8+) and PATH covers the native and fallback loaders.
#
# The candidate directories are development environments that also ship shared
# libraries other packages resolve by name (libcrypto, icu, harfbuzz, ...).
# Once libusb is loaded they must leave the process-wide search path again, or
# a later Python extension (PySide6/Qt, for one) loads the wrong copies and
# fails with "the specified procedure could not be found".
def _setup_libusb_paths() -> list:
    """Put the development DLL directories on the search path.

    Returns the resources to release once libusb has been loaded: the
    ``os.add_dll_directory`` handles and the original ``PATH``.
    """
    prefix = sys.prefix
    candidates = [
        os.path.join(prefix, "Library", "bin"),
        os.path.join(prefix, "Lib", "site-packages", "usb1"),
        # Known development environment; harmless when absent.
        r"E:\Softwares\miniconda\envs\fp-companion\Library\bin",
        r"E:\Softwares\miniconda\envs\fp-companion\Lib\site-packages\usb1",
    ]
    original_path = os.environ.get("PATH", "")
    handles: list = []
    seen: set[str] = set()
    path_parts = original_path.split(os.pathsep)
    for directory in candidates:
        if directory in seen or not os.path.isdir(directory):
            continue
        seen.add(directory)
        if hasattr(os, "add_dll_directory"):
            try:
                handles.append(os.add_dll_directory(directory))
            except OSError:
                pass
        if directory not in path_parts:
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
            path_parts.append(directory)
    return [handles, original_path]


def _release_libusb_paths(resources: list) -> None:
    handles, original_path = resources
    for handle in handles:
        try:
            handle.close()
        except OSError:
            pass
    os.environ["PATH"] = original_path


_resources = _setup_libusb_paths()

import usb.backend.libusb1  # noqa: E402
import usb.core  # noqa: E402  (import after the DLL path setup)
import usb.util  # noqa: E402

# Force libusb to load now, while the development directory is still on the
# search path.  pyusb caches the library, so every later call reuses it and the
# search path can go back to what the rest of the process expects.
usb.backend.libusb1.get_backend()  # noqa: B018
_release_libusb_paths(_resources)
del _resources

VID = 0x1003
PID = 0xC432

IFACE = 0
EP_OUT = 0x01
EP_IN = 0x82

FRAME = 64
PAYLOAD = 44
HEADER = 20

CMD_SHL = 3

TIMEOUT_MS = 1000
RETRIES = 3
MAX_BLOCK = 16 * 1024 * 1024
MAX_SKIPPED = 32

LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9

_LIBUSB_ERRNO = {
    LIBUSB_ERROR_NO_DEVICE: getattr(errno, "ENODEV", None),
    LIBUSB_ERROR_TIMEOUT: getattr(errno, "ETIMEDOUT", None),
    LIBUSB_ERROR_OVERFLOW: getattr(errno, "EOVERFLOW", None),
    LIBUSB_ERROR_PIPE: getattr(errno, "EPIPE", None),
}

FMT = "<4sBBHIHHI"  # 20 bytes

# Matches one memory reply line: "get : A:0xC007F324, D:0xE309C6FC".
_MEM_RE = re.compile(
    r"A:\s*0x([0-9a-fA-F]+)\s*,\s*D:\s*0x([0-9a-fA-F]+)"
)


class ShellError(RuntimeError):
    """Transport or shell failure."""


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _zero_checksum(frame: bytes) -> bytes:
    buf = bytearray(frame[:FRAME])
    buf[16:20] = b"\x00\x00\x00\x00"
    return bytes(buf)


def build_frame(sequence: int, command: int, payload: bytes, flags: int = 0) -> bytes:
    """Build one FPSH command transfer, CRC over the first 64 bytes."""
    if len(payload) > 0xFFFF:
        raise ShellError("command too long")
    txlen = max(FRAME, (HEADER + len(payload) + 1 + 3) & ~3)
    buf = bytearray(txlen)
    struct.pack_into(
        FMT, buf, 0, b"FPSH", 1, command, flags, sequence, len(payload), 0, 0
    )
    buf[HEADER:HEADER + len(payload)] = payload
    struct.pack_into("<I", buf, 16, _crc32(bytes(buf[:FRAME])))
    return bytes(buf)


def parse_frame(raw: bytes) -> dict:
    if len(raw) != FRAME:
        raise ShellError(f"short frame: {len(raw)} bytes")
    magic, version, command, flags, sequence, plen, status, checksum = (
        struct.unpack_from(FMT, raw, 0)
    )
    if magic != b"FPSH" or version != 1:
        raise ShellError(f"bad frame magic={magic!r} version={version}")
    if _crc32(_zero_checksum(raw)) != checksum:
        raise ShellError("bad frame CRC")
    return {
        "command": command,
        "flags": flags,
        "sequence": sequence,
        "payload_length": plen,
        "status": status,
        "payload": raw[HEADER:HEADER + min(plen, PAYLOAD)],
    }


def _is_libusb_error(exc: usb.core.USBError, code: int) -> bool:
    if getattr(exc, "errno", None) == code:
        return True
    if getattr(exc, "backend_error_code", None) == code:
        return True
    mapped = _LIBUSB_ERRNO.get(code)
    return mapped is not None and getattr(exc, "errno", None) == mapped


class CameraSession:
    """A live shell session against the camera.

    Use as a context manager so the interface is always released::

        with CameraSession() as session:
            print(session.version())
    """

    def __init__(self, timeout_ms: int = TIMEOUT_MS, retries: int = RETRIES):
        self.timeout_ms = timeout_ms
        self.retries = max(1, retries)
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise ShellError(f"camera {VID:04x}:{PID:04x} not found")
        self._claimed = False
        try:
            cfg = self.dev.get_active_configuration()
            if cfg is None or cfg.bConfigurationValue != 1:
                self.dev.set_configuration()
        except usb.core.USBError:
            try:
                self.dev.set_configuration()
            except usb.core.USBError:
                pass
        try:
            usb.util.claim_interface(self.dev, IFACE)
            self._claimed = True
        except usb.core.USBError as exc:
            raise ShellError(f"claim interface {IFACE} failed: {exc}") from exc
        # Start at a random sequence so a stale reply from an earlier client
        # run cannot be mistaken for ours.
        self.sequence = random.randrange(0x10000000, 0x7FFFFFF0)

    def close(self) -> None:
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, IFACE)
            except usb.core.USBError:
                pass
            self._claimed = False

    def __enter__(self) -> "CameraSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- framing ---------------------------------------------------------

    def _next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        if self.sequence == 0xFFFFFFFF:
            self.sequence = 1
        return self.sequence

    def _clear_halt(self, endpoint: int) -> None:
        try:
            self.dev.clear_halt(endpoint)
        except (AttributeError, usb.core.USBError):
            try:
                usb.util.clear_halt(self.dev, endpoint)
            except Exception:
                pass

    def _read_exact(self, length: int, timeout: int) -> bytes:
        return bytes(self.dev.read(EP_IN, length, timeout=timeout))

    def drain_in(self) -> None:
        """Best-effort clear of leftover IN data."""
        for _ in range(8):
            try:
                moved = len(self._read_exact(16384, 20))
            except usb.core.USBError as exc:
                if _is_libusb_error(exc, LIBUSB_ERROR_PIPE):
                    self._clear_halt(EP_IN)
                    continue
                if _is_libusb_error(exc, LIBUSB_ERROR_OVERFLOW):
                    continue
                break
            if moved == 0:
                break

    def _collect(self, sequence: int, expect_cmd: int) -> tuple[bytes, int]:
        out = bytearray()
        skipped = 0
        while True:
            frame = parse_frame(self._read_exact(FRAME, self.timeout_ms))
            if frame["sequence"] != sequence:
                if frame["flags"] & 2:
                    n = frame["payload_length"]
                    if n > MAX_BLOCK:
                        raise ShellError(f"stale block too large: {n}")
                    self._read_exact(n, self.timeout_ms)  # discard block
                skipped += 1
                if skipped > MAX_SKIPPED:
                    raise ShellError(
                        f"{skipped} stale frames, last seq "
                        f"{frame['sequence']} wanted {sequence}"
                    )
                continue

            if frame["command"] != expect_cmd:
                raise ShellError(
                    f"reply command {frame['command']} != {expect_cmd}"
                )

            if frame["flags"] & 2:
                n = frame["payload_length"]
                if n > MAX_BLOCK:
                    raise ShellError(f"reply block too large: {n}")
                want_crc = struct.unpack_from("<I", frame["payload"], 0)[0]
                block = self._read_exact(n, self.timeout_ms)
                if len(block) != n:
                    raise ShellError(f"short block: {len(block)}/{n}")
                if _crc32(block) != want_crc:
                    raise ShellError("reply block CRC mismatch")
                return block, frame["status"]

            out.extend(frame["payload"][: min(frame["payload_length"], PAYLOAD)])
            if not (frame["flags"] & 1):
                return bytes(out), frame["status"]

    def _exchange_once(
        self, command: int, payload: bytes, expect_cmd: int
    ) -> tuple[bytes, int]:
        sequence = self._next_sequence()
        tx = build_frame(sequence, command, payload)
        sent = self.dev.write(EP_OUT, tx, timeout=self.timeout_ms)
        if sent != len(tx):
            raise ShellError(f"OUT short: {sent}/{len(tx)}")
        data, status = self._collect(sequence, expect_cmd)
        if status != 0:
            raise ShellError(f"camera status {status}")
        return data, status

    def exchange(self, command: int, payload: bytes, expect_cmd: int) -> bytes:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                data, _ = self._exchange_once(command, payload, expect_cmd)
                return data
            except usb.core.USBError as exc:
                last = exc
                if _is_libusb_error(exc, LIBUSB_ERROR_NO_DEVICE):
                    raise ShellError("camera disappeared (NO_DEVICE)") from exc
                if _is_libusb_error(exc, LIBUSB_ERROR_PIPE):
                    self._clear_halt(EP_IN)
                    self._clear_halt(EP_OUT)
                else:
                    self.drain_in()
                if attempt < self.retries:
                    time.sleep(0.05)
            except ShellError as exc:
                last = exc
                self.drain_in()
                if attempt < self.retries:
                    time.sleep(0.05)
        raise ShellError(f"failed after {self.retries} attempts: {last}") from last

    # -- public shell helpers -------------------------------------------

    def sh(self, line: str) -> str:
        """Run one shell line and return the reply text (latin-1 decoded)."""
        if any(ch in line for ch in "\r\n\0"):
            raise ShellError("shell line must be a single line")
        payload = b"shl " + line.encode("latin-1")
        data = self.exchange(CMD_SHL, payload, CMD_SHL)
        return data.decode("latin-1").rstrip("\x00")

    def mem_get(self, addr: int, size: int = 4) -> int:
        """Read ``size`` bytes via ``mem get`` and return the D word.

        ``mem get ADDR,OFFSET,LEN`` always reports the first word as ``D``;
        callers needing other words can send their own ``sh()`` line.
        """
        if size <= 0:
            raise ShellError(f"invalid size {size}")
        address = int(addr)
        reply = self.sh(f"mem get 0x{address:08X},,{int(size)}")
        matches = _MEM_RE.findall(reply)
        if not matches:
            raise ShellError(f"cannot parse memory reply: {reply.strip()!r}")
        # Prefer the requested address; a multi-word read still returns the
        # first word for callers that only asked for one.
        for found, data in matches:
            if int(found, 16) == address:
                return int(data, 16)
        return int(matches[0][1], 16)

    def version(self) -> str:
        """Return the shell ``version`` reply, stripped."""
        return self.sh("version").strip()

    def status(self, name: str) -> str:
        """Return ``status get <name>``, stripped."""
        return self.sh(f"status get {name}").strip()
