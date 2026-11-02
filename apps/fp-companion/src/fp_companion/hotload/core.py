"""Host-side hot reload for the LExpo sup, over the fpSup USB shell.

This is the library form of the verified prototype
``firmware/tools/hotload_lexpo.py``.  It knows nothing about USB: every
operation runs over a *transport* object that supplies

    transport.shl(line) -> bytes | str
    transport.retries           (int, optional but expected)

:class:`Hotload` borrows the firmware shell's ``echo`` handler to run a resident
loader service in the shell's task context, then reads the result back.

Loader contract
---------------
The SIGMA fp runs fpSup loader v3.  Service-table version 6 adds::

    release (svc, sup_id)   at struct sl_svc + 44 (0x2C)
    load_one(svc, path)     at struct sl_svc + 48 (0x30)
    reload  (svc, sup_id)   at struct sl_svc + 52 (0x34)

``svc`` is the table pointer the loader keeps at ``SL_BOOK_AT``
(``0xC072F6F0``) for the whole boot.  LExpo's sup_id is ASCII ``"LXPO"``
little-endian = ``0x4F50584C``.

A loader built with ``SL_ALLSUPS`` carries the optional ``button`` field at
``+0x2C``, which shifts the three entries to ``+0x30/+0x34/+0x38``.  Use
``layout="allsups"`` for that build; the default is the standard build.

IMPORTANT: the values at ``svc+44/+48/+52`` are *function pointers*, not the
addresses of the table slots.  The loader is position-independent and copies
itself into a heap block, so each pointer is a runtime Thumb address.  This
module reads the pointer and calls it.  Calling ``book + 52`` itself would
jump into state data and freeze the shell.

Safety
------
``callfn.S`` does not check the function address.  A wrong address is a jump
into whatever is there: the shell freezes and the camera may need its battery
pulled.  Service-table pointers are validated before arming, and the code cave
is not overwritten unless it is empty, already ours, or ``force_code_addr`` is
set.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import shutil
import struct
import sys
import time

# --- loader contract (sloader.h) --------------------------------------------
SL_BOOK_AT = 0xC072F6F0          # holds &svc for the whole boot
SL_SVC_AT = 0xC072F6FC           # &svc only while entries run (not needed here)
LXPO_ID = 0x4F50584C             # "LXPO" little-endian

# struct sl_svc, standard build (no SL_ALLSUPS / SL_SVC_BUTTON):
#   +0x00 version      +0x04 boot_id    +0x08 claim      +0x0C claim_res
#   +0x10 chain_next   +0x14 cave_alloc +0x18 publish    +0x1C log
#   +0x20 holder       +0x24 self       +0x28 done_at_start
#   +0x2C release      +0x30 load_one   +0x34 reload
# With SL_ALLSUPS the optional button field occupies +0x2C and the last three
# move to +0x30/+0x34/+0x38.  Keep these numbers in step with sloader.h.
RELEASE_OFF = 0x2C               # 44
LOAD_ONE_OFF = 0x30              # 48
RELOAD_OFF = 0x34                # 52

RELEASE_OFF_ALLSUPS = 0x30       # 48
LOAD_ONE_OFF_ALLSUPS = 0x34      # 52
RELOAD_OFF_ALLSUPS = 0x38        # 56

# --- LExpo patch sites -------------------------------------------------------
SITE_PATCH = 0xC007F324
STOCK_PATCH_WORD = 0xE309C6F4
PATCHED_WORD = 0xE309C6FC

SITE_LV = 0xC02BAD0C
STOCK_LV_WORD = 0xE24DD064       # sub sp, sp, #0x64
LV_BL_MASK, LV_BL = 0xFF000000, 0xEB000000

# The live-view hook's veneer: `ldr pc, [pc, #-4]`, then the hook base.  The
# current 30LEXPO.BIN (SHA-256 499d96e7...) puts lv_done at hook_base + 0x80;
# the older +0x54 layout is not valid.  Only read when the site is a BL.
CAVE_VENEER = 0xC072E068
VENEER_LDR = 0xE51FF004
LV_DONE_OFFSET = 0x80


def _arm_bl_target(word: int, site: int) -> int | None:
    """Resolve an ARM ``BL`` word at ``site`` to its target, or None."""
    if (word & 0x0F000000) != 0x0B000000:
        return None
    off = word & 0x00FFFFFF
    if off & 0x00800000:
        off -= 0x01000000
    return (site + 8 + (off << 2)) & 0xFFFFFFFF

# --- the echo-handler borrow and callfn parameter block ----------------------
ECHO_SLOT = 0xC0BAC2F8           # command table entry 17, `echo`
ECHO_ORIG = 0xC03D99A0           # echo's stock handler

P_BLOCK = 0xC072F700             # callfn.S parameter block (host file-tool scratch)
P_FN, P_R0, P_R1, P_R2, P_R3 = 0x00, 0x04, 0x08, 0x0C, 0x10
P_R10, P_RET, P_DONE, P_R4, P_R5 = 0x14, 0x18, 0x1C, 0x20, 0x24
DONE = 0xD09E0000

CODE_ADDR = 0xC072FE00           # above the file-tool scratch 0xC072F700..0xC072FC84
CAVE_END = 0xC0730000

# callfn.S assembled for the fixed P_BLOCK (0xC072F700): 140 bytes, 35 words.
# Embedding it keeps a `pip install fp-companion` usable without LLVM/clang or
# the firmware checkout.  To rebuild, run the prototype's assembler and compare
# the SHA-256 (see firmware/tools/hotload_lexpo.py).
CALLFN_IMAGE_SHA256 = (
    "7497a97da0c55a402f62a0a1837530444b64a526c63440fb342d0d8fa7b5a14e"
)
CALLFN_IMAGE = bytes.fromhex(
    "f84f2de90080a0e100470fe372404ce3"
    "0000a0e31c0084e5009094e5040094e5"
    "081094e50c2094e5103094e514a094e5"
    "206094e5247094e50640a0e10750a0e1"
    "39ff2fe100470fe372404ce3180084e5"
    "000000e39e004de31c0084e500c098e5"
    "380b0be3ba004ce308108fe23cff2fe1"
    "0000a0e3f88fbde863616c6c666e3a20"
    "72657475726e6564000000a0"
)

# A 32-byte path scratch for load_one().  It sits above the file tools and is
# enough for "\fpSup\30LEXPO.BIN" plus the NUL.
PATH_BUF = 0xC072FC90
PATH_BUF_BYTES = 32

MEM_SET_TRIES = 8                # per sloader.h / callfn.py practice
READ_TRIES = 4

# plausible resident-memory window used to reject a zero or garbage pointer
PLAUSIBLE_LO = 0x40000000
PLAUSIBLE_HI = 0xC0000000


# --- errors ------------------------------------------------------------------
class HotloadError(RuntimeError):
    """Base class for every hotload failure."""


class UsageError(HotloadError):
    """The caller asked for something this tool refuses to attempt."""


class TransportError(HotloadError):
    """The shell or the USB transport failed."""


class VerifyError(HotloadError):
    """A write, a call or a site did not verify."""


class CallDidNotReturn(VerifyError):
    """The shell did run the borrowed handler but never reported completion."""

    def __init__(self, fn: int, value: int, done: int):
        self.fn = fn
        self.value = value
        self.done = done
        super().__init__(
            f"call 0x{fn:08X} did not return: P_RET=0x{value:08X}, "
            f"P_DONE=0x{done:08X} (want 0x{DONE:08X}).  A wrong function "
            f"address can freeze the shell; a battery pull may be needed.")


class BookError(VerifyError):
    """SL_BOOK_AT does not hold a resident loader table (or it is too old)."""


class EntryError(VerifyError):
    """A service-table entry is not a plausible resident code pointer."""


class RefuseError(VerifyError):
    """Refused to overwrite something that is not ours."""


# --- parsing -----------------------------------------------------------------
# One `A:..., D:...` pair from `mem get`; the shell prints one line per word.
_ADDR_DATA_RE = re.compile(
    r"A:\s*(?:0x)?([0-9A-Fa-f]+)\s*,\s*D:\s*(?:0x)?([0-9A-Fa-f]+)")
# A bare D token, for a shell that omits the address.
_D_RE = re.compile(r"D:\s*(?:0x)?([0-9A-Fa-f]+)")


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _plausible_mem(addr: int) -> bool:
    return PLAUSIBLE_LO <= addr < PLAUSIBLE_HI


class Layout:
    """Byte offsets of the three service-table entries."""

    __slots__ = ("release", "load_one", "reload")

    def __init__(self, release: int, load_one: int, reload: int):
        self.release = release
        self.load_one = load_one
        self.reload = reload

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"Layout(release=+{self.release:#x}, "
                f"load_one=+{self.load_one:#x}, reload=+{self.reload:#x})")


LAYOUTS = {
    "standard": Layout(RELEASE_OFF, LOAD_ONE_OFF, RELOAD_OFF),
    "allsups": Layout(RELEASE_OFF_ALLSUPS, LOAD_ONE_OFF_ALLSUPS,
                      RELOAD_OFF_ALLSUPS),
}


# =============================================================================
# Optional toolchain check (doctor only; never needed to run the bridge)
# =============================================================================
def clang_path() -> str | None:
    """Return a usable clang, adding a standard Windows LLVM install to PATH.

    This is only a diagnostic/rebuild aid.  The callfn bridge is embedded in
    :data:`CALLFN_IMAGE`, so runtime hotload does not invoke clang.
    """
    found = shutil.which("clang")
    if found:
        return found

    candidates: list[pathlib.Path] = []
    env = os.environ.get("LLVM_BIN")
    if env:
        candidates.append(pathlib.Path(env))
    if sys.platform == "win32":
        candidates.append(pathlib.Path(r"C:\Program Files\LLVM\bin"))

    exe = "clang.exe" if sys.platform == "win32" else "clang"
    for directory in candidates:
        candidate = directory / exe
        if candidate.is_file():
            os.environ["PATH"] = (
                str(directory) + os.pathsep + os.environ.get("PATH", "")
            )
            return str(candidate)
    return None


# =============================================================================
# Hotload
# =============================================================================
class Hotload:
    """Shell helpers and loader calls over a transport.

    ``transport`` only needs ``shl(line) -> bytes | str`` and, for ``call``, a
    ``retries`` attribute.  :class:`fp_companion.hotload.link.CameraLink` and
    ``fp_companion.autotest.session.CameraSession`` both supply these; the
    tests use a fake that supplies them without USB.
    """

    def __init__(self, transport, *, layout: str = "standard",
                 mem_tries: int = READ_TRIES, code_addr: int = CODE_ADDR,
                 force_code_addr: bool = False):
        if layout not in LAYOUTS:
            raise UsageError(f"unknown service layout {layout!r}")
        self.transport = transport
        # ``cam`` is kept as an alias for callers written against the prototype.
        self.cam = transport
        self.layout = LAYOUTS[layout]
        self.mem_tries = max(1, int(mem_tries))
        self.code_addr = code_addr
        self.force_code_addr = force_code_addr

    # --- shell primitives ---------------------------------------------------
    def sh(self, line: str) -> str:
        """One shell line, decoded.  Transport failures become TransportError."""
        try:
            reply = self.transport.shl(line)
        except Exception as exc:                       # ShellError, USBError
            raise TransportError(f"{line!r}: {exc}") from exc
        if isinstance(reply, (bytes, bytearray)):
            return bytes(reply).decode("latin-1")
        return str(reply)

    def mem_words(self, addr: int, count: int = 1,
                  tries: int | None = None) -> list[int]:
        """Read ``count`` 32-bit words at ``addr``, retrying missing words."""
        if count < 1:
            raise UsageError("count must be >= 1")
        tries = self.mem_tries if tries is None else max(1, int(tries))
        span = count * 4
        line = (f"mem get 0x{addr:08X},,4" if count == 1
                else f"mem get 0x{addr:08X},,0x{span:X}")
        seen: dict[int, int] = {}
        last = ""
        for _ in range(tries):
            last = self.sh(line)
            for a, d in _ADDR_DATA_RE.findall(last):
                seen[int(a, 16)] = int(d, 16)
            if count == 1:
                if addr in seen:
                    return [seen[addr]]
                # A shell may answer only the D field for a single word.
                m = _D_RE.search(last)
                if m:
                    return [int(m.group(1), 16)]
            elif all(addr + i * 4 in seen for i in range(count)):
                return [seen[addr + i * 4] for i in range(count)]
        raise VerifyError(
            f"mem get 0x{addr:08X} ({span} bytes) did not answer with every "
            f"word after {tries} tries: {last.strip()!r}")

    def mem_get(self, addr: int, tries: int | None = None) -> int:
        """One word.  Parses the ``D:0x...`` token; raises if it is absent."""
        return self.mem_words(addr, 1, tries)[0]

    def mem_set(self, addr: int, value: int, tries: int = MEM_SET_TRIES) -> None:
        """Write one word and prove it stuck; ``mem set`` drops whole commands."""
        value &= 0xFFFFFFFF
        last: object = "no reply"
        for _ in range(tries):
            self.sh(f"mem set 0x{addr:08X} 0x{value:08X}")
            try:
                got = self.mem_get(addr)
            except VerifyError as exc:
                last = exc
                continue
            if got == value:
                return
            last = f"read back 0x{got:08X}"
        raise VerifyError(
            f"mem set 0x{addr:08X} 0x{value:08X} did not stick after "
            f"{tries} attempts ({last})")

    def write_words(self, addr: int, data: bytes,
                    tries: int = MEM_SET_TRIES) -> None:
        """Write whole little-endian words, aligned, and read every one back."""
        data = bytes(data)
        if addr & 3:
            raise UsageError(
                f"write address 0x{addr:08X} is not 4-byte aligned")
        if len(data) % 4:
            raise UsageError(
                f"write length {len(data)} is not a whole number of words")
        if not data:
            return
        words = list(struct.unpack(f"<{len(data) // 4}I", data))
        for i, word in enumerate(words):
            self.mem_set(addr + i * 4, word)
        bad: list[int] = []
        last: object = None
        for attempt in range(max(1, tries)):
            if attempt:
                # Repair what the previous readback found, then read again.
                for i in bad:
                    self.mem_set(addr + i * 4, words[i])
            back = self.mem_words(addr, len(words))
            bad = [i for i in range(len(words)) if back[i] != words[i]]
            if not bad:
                return
            last = [f"0x{addr + i * 4:08X}" for i in bad]
        raise VerifyError(
            f"write at 0x{addr:08X}: words did not read back after {tries} "
            f"passes ({last})")

    @contextlib.contextmanager
    def echo_handler(self, at: int):
        """Borrow ``echo``'s handler slot, and put the stock handler back."""
        current = self.mem_get(ECHO_SLOT)
        if current not in (ECHO_ORIG, at):
            raise RefuseError(
                f"echo handler at 0x{ECHO_SLOT:08X} is 0x{current:08X}, not "
                f"0x{ECHO_ORIG:08X}; something else has borrowed it")
        if current != at:
            self.mem_set(ECHO_SLOT, at)
        try:
            yield
        finally:
            self.mem_set(ECHO_SLOT, ECHO_ORIG)

    # --- the call bridge ----------------------------------------------------
    def call(self, fn: int, r0: int = 0, r1: int = 0, r2: int = 0, r3: int = 0,
             r4: int = 0, r5: int = 0, r10: int = 0, *,
             code: bytes | None = None, code_addr: int | None = None,
             force_code_addr: bool | None = None) -> int:
        """Run ``fn`` once in the shell's task context and return r0.

        A wrong ``fn`` is a jump into whatever is there: the shell freezes and
        the camera may need a battery pull.  Service-table entry pointers are
        checked by :meth:`_entry` before this is reached.

        ``code`` and ``code_addr`` exist for tests and for a developer who
        rebuilds the bridge; normally the embedded :data:`CALLFN_IMAGE` is
        placed at ``self.code_addr``.
        """
        fn &= 0xFFFFFFFF
        if fn == 0:
            raise UsageError("refusing to call a NULL function pointer")
        code_addr = self.code_addr if code_addr is None else code_addr
        force = (self.force_code_addr if force_code_addr is None
                 else force_code_addr)

        if code is None:
            code = CALLFN_IMAGE
        if code_addr & 3:
            raise UsageError(
                f"callfn address 0x{code_addr:08X} is not 4-byte aligned")
        if code_addr + len(code) > CAVE_END:
            raise UsageError(
                f"callfn at 0x{code_addr:08X} would run past 0x{CAVE_END:08X}")
        if len(code) % 4:
            raise UsageError("callfn image is not a whole number of words")

        words = list(struct.unpack(f"<{len(code) // 4}I", code))
        first = self.mem_get(code_addr)
        if first != 0 and first != words[0] and not force:
            raise RefuseError(
                f"0x{code_addr:08X} already holds 0x{first:08X}, not callfn; "
                f"refusing to overwrite it (pass --force-code-addr to override)")
        self.write_words(code_addr, code)

        # `mem set` drops whole commands; a dropped P_FN leaves zero and the
        # echo jumps to it.  Nothing is armed until every word reads back.
        params = {
            P_FN: fn, P_R0: r0, P_R1: r1, P_R2: r2, P_R3: r3,
            P_R10: r10, P_RET: 0, P_DONE: 0, P_R4: r4, P_R5: r5,
        }
        expected = [params[off] & 0xFFFFFFFF for off in range(0, 0x28, 4)]
        last: object = None
        for _ in range(MEM_SET_TRIES):
            for off in range(0, 0x28, 4):
                self.mem_set(P_BLOCK + off, params[off])
            got = self.mem_words(P_BLOCK, 10)
            if got == expected:
                break
            last = got
        else:
            raise VerifyError(
                f"callfn parameters at 0x{P_BLOCK:08X} did not stick after "
                f"{MEM_SET_TRIES} fills: {last}")

        saved_retries = getattr(self.transport, "retries", None)
        with self.echo_handler(code_addr):
            # A retry here would run the function twice.  One send only.
            if saved_retries is not None:
                self.transport.retries = 1
            try:
                self.sh("echo")
            finally:
                if saved_retries is not None:
                    self.transport.retries = saved_retries
        # A retry here would run the function twice.  One send only.
        ret, done = self.mem_words(P_BLOCK + P_RET, 2)
        if done != DONE:
            raise CallDidNotReturn(fn, ret, done)
        return ret

    # --- hotload operations -------------------------------------------------
    def book(self) -> tuple[int, int]:
        """Return ``(&svc, version)`` after checking the loader is resident."""
        book = self.mem_get(SL_BOOK_AT)
        if not _plausible_mem(book):
            raise BookError(
                f"SL_BOOK_AT 0x{SL_BOOK_AT:08X} holds 0x{book:08X}; the "
                f"loader is not resident (want 0x{PLAUSIBLE_LO:08X}.."
                f"0x{PLAUSIBLE_HI - 1:08X})")
        version = self.mem_get(book)
        if version < 6:
            raise BookError(
                f"service table at 0x{book:08X} is version {version}; "
                f"release/load_one/reload need version >= 6")
        return book, version

    def _entry(self, offset: int) -> tuple[int, int]:
        """Resolve svc+offset to the function pointer stored there."""
        book, _ = self.book()
        ptr_addr = book + offset
        ptr = self.mem_get(ptr_addr)
        if not _plausible_mem(ptr):
            raise EntryError(
                f"svc entry +0x{offset:X} at 0x{ptr_addr:08X} holds "
                f"0x{ptr:08X}, not a resident code pointer; refusing to call it")
        return book, ptr

    def release(self) -> dict:
        book, ptr = self._entry(self.layout.release)
        value = _signed32(self.call(ptr, r0=book, r1=LXPO_ID))
        return {"return": value, "book": book, "entry": ptr}

    def reload(self) -> dict:
        book, ptr = self._entry(self.layout.reload)
        value = _signed32(self.call(ptr, r0=book, r1=LXPO_ID))
        return {"return": value, "book": book, "entry": ptr}

    def load(self, path: str | None = None,
             path_addr: int | None = None) -> dict:
        """Call load_one.  ``path`` is copied to a 32-byte scratch first."""
        if path is not None and path_addr is not None:
            raise UsageError("give path or path_addr, not both")
        if path is None and path_addr is None:
            raise UsageError("load needs path or path_addr")
        if path_addr is None:
            try:
                raw = path.encode("ascii") + b"\0"
            except UnicodeEncodeError as exc:
                raise UsageError(f"path {path!r} is not ASCII") from exc
            if len(raw) > PATH_BUF_BYTES:
                raise UsageError(
                    f"path {path!r} needs {len(raw)} bytes; the scratch at "
                    f"0x{PATH_BUF:08X} holds only {PATH_BUF_BYTES} including "
                    f"the NUL.  Use path_addr for a longer path.")
            present = self.mem_words(PATH_BUF, PATH_BUF_BYTES // 4)
            if any(present):
                old = b"".join(struct.pack("<I", word) for word in present)
                old_path = old.split(b"\0", 1)[0]
                if not (old_path.startswith(b"\\fpSup\\")
                        and old_path.upper().endswith(b".BIN")):
                    raise RefuseError(
                        f"path scratch 0x{PATH_BUF:08X} holds something that "
                        f"is not one of our paths ({old_path!r}); refusing to "
                        f"clobber it")
            padded = raw + b"\0" * (-len(raw) % 4)
            self.write_words(PATH_BUF, padded)
            path_addr = PATH_BUF
        elif path_addr == 0:
            raise UsageError("path_addr is 0")

        book, ptr = self._entry(self.layout.load_one)
        value = _signed32(self.call(ptr, r0=book, r1=path_addr))
        return {"return": value, "book": book, "entry": ptr,
                "path_addr": path_addr}

    # --- read-only views ----------------------------------------------------
    def _site_patch_state(self, word: int) -> str:
        if word == PATCHED_WORD:
            return "patched"
        if word == STOCK_PATCH_WORD:
            return "stock"
        return "foreign"

    def _lv_state(self, word: int) -> str:
        if word == STOCK_LV_WORD:
            return "disarmed"
        if word & LV_BL_MASK == LV_BL:
            return "armed"
        return "unknown"

    def _lv_done(self, lv_word: int) -> tuple[int | None, int | None]:
        """``(lv_done, address)`` if the armed hook's veneer says where it is."""
        if lv_word == STOCK_LV_WORD or lv_word & LV_BL_MASK != LV_BL:
            return None, None
        target = _arm_bl_target(lv_word, SITE_LV)
        if target is None:
            return None, None
        veneer = self.mem_words(target, 2)
        if veneer[0] != VENEER_LDR or veneer[1] == 0:
            return None, None
        addr = veneer[1] + LV_DONE_OFFSET
        try:
            return self.mem_get(addr), addr
        except VerifyError:
            return None, addr

    def _version_text(self) -> str:
        text = " | ".join(
            line.strip() for line in self.sh("version").splitlines()
            if line.strip())
        return text if len(text) <= 160 else text[:157] + "..."

    def probe(self) -> dict:
        """Read-only: version, both sites, SL_BOOK_AT and svc.version."""
        version = self._version_text()
        patch = self.mem_get(SITE_PATCH)
        lv = self.mem_get(SITE_LV)
        book_word = self.mem_get(SL_BOOK_AT)
        loader_ok = _plausible_mem(book_word)
        svc_version = self.mem_get(book_word) if loader_ok else None
        version_ok = svc_version is not None and svc_version >= 6
        return {
            "version": version,
            "site_patch": patch,
            "site_patch_state": self._site_patch_state(patch),
            "site_lv": lv,
            "site_lv_state": self._lv_state(lv),
            "sl_book": book_word,
            "loader_ok": loader_ok,
            "svc_version": svc_version,
            "version_ok": version_ok,
            "ok": loader_ok and version_ok,
        }

    def snapshot(self) -> dict:
        """Read-only: probe's fields plus lv_done when the hook reveals it."""
        data = self.probe()
        lv_done, lv_done_addr = self._lv_done(data["site_lv"])
        data.update({"lv_done": lv_done, "lv_done_addr": lv_done_addr})
        return data

    def verify_sites(self) -> dict:
        """After reload/load: the patch must be applied and the hook must be
        either armed or already self-disarmed."""
        patch = self.mem_get(SITE_PATCH)
        lv = self.mem_get(SITE_LV)
        patch_ok = patch == PATCHED_WORD
        lv_state = self._lv_state(lv)
        lv_ok = lv_state in ("armed", "disarmed")
        return {"patch": patch, "patch_state": self._site_patch_state(patch),
                "patch_ok": patch_ok, "lv": lv, "lv_state": lv_state,
                "lv_ok": lv_ok, "ok": patch_ok and lv_ok}

    def verify_released(self) -> dict:
        """After release: both LExpo sites should be back to the stock words."""
        patch = self.mem_get(SITE_PATCH)
        lv = self.mem_get(SITE_LV)
        patch_ok = patch == STOCK_PATCH_WORD
        lv_ok = lv == STOCK_LV_WORD
        return {"patch": patch, "patch_state": self._site_patch_state(patch),
                "patch_ok": patch_ok, "lv": lv, "lv_state": self._lv_state(lv),
                "lv_ok": lv_ok, "ok": patch_ok and lv_ok}

    # --- repeated reload ----------------------------------------------------
    def cycle(self, n: int = 1) -> list[dict]:
        """Reload ``n`` times, checking both sites after each; stop on failure."""
        if n < 1:
            raise UsageError("--n must be >= 1")
        results: list[dict] = []
        for i in range(1, n + 1):
            started = time.monotonic()
            try:
                res = self.reload()
            except (UsageError, TransportError):
                raise       # exit 2, not a verification result
            except HotloadError as exc:
                results.append({"iteration": i, "ok": False,
                                "error": str(exc),
                                "elapsed_s": time.monotonic() - started})
                break
            elapsed = time.monotonic() - started
            sites = self.verify_sites()
            ok = res["return"] == 0 and sites["ok"]
            results.append({"iteration": i, **res, "sites": sites,
                            "elapsed_s": elapsed, "ok": ok})
            if not ok:
                break
        return results

    def stress(self, n: int = 10) -> dict:
        """Run ``cycle(n)`` and add counts and timing statistics."""
        results = self.cycle(n)
        times = [r["elapsed_s"] for r in results if "elapsed_s" in r]
        ok = len(results) == n and all(r["ok"] for r in results)
        return {
            "n": n,
            "results": results,
            "ok": ok,
            "completed": len(results),
            "min_s": min(times) if times else None,
            "avg_s": (sum(times) / len(times)) if times else None,
            "max_s": max(times) if times else None,
        }
