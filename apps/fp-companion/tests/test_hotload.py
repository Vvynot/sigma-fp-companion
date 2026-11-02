"""Offline tests for :mod:`fp_companion.hotload`.

Ported from the verified prototype's ``--self-test`` suite.  No USB and no
clang: the transport is a fake camera that serves canned ``mem get`` replies
and applies ``mem set``, and the callfn bridge is the embedded image.
"""

from __future__ import annotations

import hashlib
import re
import struct
import sys

import fp_companion.hotload as hotload_pkg
from fp_companion.hotload import (
    BookError,
    CallDidNotReturn,
    EntryError,
    Hotload,
    Layout,
    LAYOUTS,
    LXPO_ID,
    RefuseError,
    SL_BOOK_AT,
    TransportError,
    UsageError,
    VerifyError,
)
from fp_companion.hotload import core
from fp_companion.hotload.core import (
    CAVE_VENEER,
    CODE_ADDR,
    DONE,
    ECHO_ORIG,
    ECHO_SLOT,
    LOAD_ONE_OFF,
    P_BLOCK,
    P_DONE,
    P_FN,
    P_R0,
    P_R1,
    P_R2,
    P_R3,
    P_R4,
    P_R5,
    P_R10,
    P_RET,
    PATH_BUF,
    PATH_BUF_BYTES,
    PATCHED_WORD,
    RELEASE_OFF,
    RELOAD_OFF,
    SITE_LV,
    SITE_PATCH,
    STOCK_LV_WORD,
    STOCK_PATCH_WORD,
    VENEER_LDR,
)

_MEM_SET_RE = re.compile(
    r"mem set 0x([0-9A-Fa-f]+) 0x([0-9A-Fa-f]+)")
_MEM_GET_RE = re.compile(
    r"mem get 0x([0-9A-Fa-f]+),,(0x[0-9A-Fa-f]+|\d+)")


class FakeCamera:
    """Records shell lines and serves canned ``mem get`` replies.

    ``drop_all_sets`` silently discards that many ``mem set`` commands (the
    transport's known behaviour).  ``corrupt_param_multi_read`` spoils the
    first multi-word read of the callfn block once, to exercise the fill retry.
    ``echo_result`` is the ``(P_RET, P_DONE)`` the simulated callfn leaves.
    """

    def __init__(self, mem: dict[int, int] | None = None, *,
                 drop_all_sets: int = 0, echo_result=None,
                 corrupt_param_multi_read: int = 0):
        self.mem = dict(mem or {})
        self.lines: list[str] = []
        self.drop_all_sets = drop_all_sets
        self.echo_result = echo_result or (0x1234, DONE)
        self.corrupt_param_multi_read = corrupt_param_multi_read
        self.retries = 3
        self.retries_at_echo: list[int] = []

    def shl(self, line: str) -> bytes:
        self.lines.append(line)
        if line == "echo":
            self.retries_at_echo.append(self.retries)
            ret, done = self.echo_result
            self.mem[P_BLOCK + P_RET] = ret & 0xFFFFFFFF
            self.mem[P_BLOCK + P_DONE] = done & 0xFFFFFFFF
            return b"callfn: returned\n"
        if line == "version":
            return b"Ver.5.02\n"

        match = _MEM_SET_RE.fullmatch(line)
        if match:
            if self.drop_all_sets > 0:
                self.drop_all_sets -= 1
                return b"OK\n"
            self.mem[int(match.group(1), 16)] = int(match.group(2), 16)
            return b"OK\n"

        match = _MEM_GET_RE.fullmatch(line)
        if match:
            addr = int(match.group(1), 16)
            count = int(match.group(2), 0)
            words = [(addr + off, self.mem.get(addr + off, 0))
                     for off in range(0, count, 4)]
            if (addr == P_BLOCK and count >= 0x28
                    and self.corrupt_param_multi_read > 0):
                self.corrupt_param_multi_read -= 1
                words[0] = (words[0][0], 0xDEADBEEF)
            return ("\n".join(f"A:0x{a:08X}, D:0x{d:08X}"
                              for a, d in words) + "\n").encode("latin-1")

        return b""


def _fake(mem: dict[int, int] | None = None, **kwargs) -> FakeCamera:
    base = {ECHO_SLOT: ECHO_ORIG}
    base.update(mem or {})
    return FakeCamera(base, **kwargs)


def _expect(exc_type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# -- public API ----------------------------------------------------------


def test_public_exports() -> None:
    for name in (
        "Hotload", "HotloadError", "UsageError", "TransportError",
        "VerifyError", "CallDidNotReturn", "BookError", "EntryError",
        "RefuseError", "Layout", "LAYOUTS", "SL_BOOK_AT", "LXPO_ID",
    ):
        assert hasattr(hotload_pkg, name), name


def test_offsets_and_layouts() -> None:
    assert RELEASE_OFF == 44
    assert LOAD_ONE_OFF == 48
    assert RELOAD_OFF == 52
    assert LXPO_ID == 0x4F50584C
    assert SL_BOOK_AT == 0xC072F6F0
    assert P_BLOCK == 0xC072F700
    assert CODE_ADDR == 0xC072FE00
    assert PATH_BUF == 0xC072FC90
    assert PATH_BUF_BYTES == 32
    assert SITE_PATCH == 0xC007F324
    assert PATCHED_WORD == 0xE309C6FC
    assert STOCK_PATCH_WORD == 0xE309C6F4
    assert SITE_LV == 0xC02BAD0C
    assert STOCK_LV_WORD == 0xE24DD064
    assert ECHO_SLOT == 0xC0BAC2F8
    assert ECHO_ORIG == 0xC03D99A0
    assert DONE == 0xD09E0000

    assert isinstance(LAYOUTS["standard"], Layout)
    assert LAYOUTS["standard"].release == 44
    assert LAYOUTS["standard"].load_one == 48
    assert LAYOUTS["standard"].reload == 52
    assert LAYOUTS["allsups"].release == 48
    assert LAYOUTS["allsups"].load_one == 52
    assert LAYOUTS["allsups"].reload == 56


def test_unknown_layout_is_usage_error() -> None:
    _expect(UsageError, Hotload, _fake(), layout="nope")


def test_transport_error_is_wrapped() -> None:
    class Broken:
        retries = 1

        def shl(self, line):
            raise OSError("usb gone")

    _expect(TransportError, Hotload(Broken()).mem_get, 0x1000)


# -- parsing and memory primitives --------------------------------------


def test_parse_mem_get() -> None:
    fake = _fake({SITE_PATCH: PATCHED_WORD})
    assert Hotload(fake).mem_get(SITE_PATCH) == PATCHED_WORD
    assert fake.lines[-1] == f"mem get 0x{SITE_PATCH:08X},,4"


def test_parse_mem_get_without_address() -> None:
    class NoAddr(FakeCamera):
        def shl(self, line):
            self.lines.append(line)
            return b"D:0x12345678\n"

    assert Hotload(NoAddr({ECHO_SLOT: ECHO_ORIG})).mem_get(0x1000) == 0x12345678


def test_parse_mem_get_rejects_error() -> None:
    class Bad(FakeCamera):
        def shl(self, line):
            self.lines.append(line)
            return b"ERR no such memory\n"

    _expect(VerifyError, Hotload(Bad({ECHO_SLOT: ECHO_ORIG}),
                                 mem_tries=2).mem_get, 0x1000)


def test_parse_multi_word() -> None:
    fake = _fake({0x1000: 0x11111111, 0x1004: 0x22222222})
    assert Hotload(fake).mem_words(0x1000, 2) == [0x11111111, 0x22222222]


def test_mem_set_retries_dropped_command() -> None:
    fake = _fake(drop_all_sets=2)
    Hotload(fake).mem_set(0xC007F324, 0xABCD1234)
    assert fake.mem[0xC007F324] == 0xABCD1234
    assert sum(1 for line in fake.lines
               if line.startswith("mem set")) == 3


def test_mem_set_gives_up_after_eight() -> None:
    fake = _fake(drop_all_sets=8)
    _expect(VerifyError, Hotload(fake).mem_set, 0xC007F324, 1)
    assert sum(1 for line in fake.lines
               if line.startswith("mem set")) == 8


def test_write_words_alignment_and_length() -> None:
    link = Hotload(_fake())
    _expect(UsageError, link.write_words, 0xC072FE02, b"\0\0\0\0")
    _expect(UsageError, link.write_words, 0xC072FE00, b"\0\0\0")


def test_write_words_verifies() -> None:
    fake = _fake()
    data = struct.pack("<4I", 1, 2, 3, 4)
    Hotload(fake).write_words(0xC072FE00, data)
    assert [fake.mem[0xC072FE00 + i * 4] for i in range(4)] == [1, 2, 3, 4]


# -- call bridge ---------------------------------------------------------


def test_call_fills_and_reads_back() -> None:
    fake = _fake()
    link = Hotload(fake)
    code = struct.pack("<8I", *range(8))
    ret = link.call(0x44ABCDE1, r0=1, r1=2, r2=3, r3=4, r4=5, r5=6,
                    r10=7, code=code)
    assert ret == 0x1234
    want = {P_FN: 0x44ABCDE1, P_R0: 1, P_R1: 2, P_R2: 3, P_R3: 4,
            P_R10: 7, P_RET: 0x1234, P_DONE: DONE, P_R4: 5, P_R5: 6}
    for off, value in want.items():
        assert fake.mem[P_BLOCK + off] == value, hex(off)
    assert fake.mem[ECHO_SLOT] == ECHO_ORIG
    assert [fake.mem[CODE_ADDR + i * 4] for i in range(8)] == list(range(8))


def test_call_done_check() -> None:
    fake = _fake(echo_result=(0x55, 0))
    _expect(CallDidNotReturn, Hotload(fake).call, 0x44ABCDE1,
            code=struct.pack("<I", 0))
    assert fake.mem[ECHO_SLOT] == ECHO_ORIG


def test_call_refuses_foreign_code() -> None:
    fake = _fake({CODE_ADDR: 0xDEADBEEF})
    link = Hotload(fake)
    code = struct.pack("<I", 0x11111111)
    _expect(RefuseError, link.call, 0x44ABCDE1, code=code)
    ret = link.call(0x44ABCDE1, code=code, force_code_addr=True)
    assert ret == 0x1234
    assert fake.mem[CODE_ADDR] == 0x11111111


def test_call_fill_retries_whole_block() -> None:
    fake = _fake(corrupt_param_multi_read=1)
    Hotload(fake).call(0x44ABCDE1, code=struct.pack("<I", 0))
    multi = [line for line in fake.lines
             if line == f"mem get 0x{P_BLOCK:08X},,0x28"]
    assert len(multi) >= 2, multi


def test_call_sends_echo_once_and_restores_retries() -> None:
    fake = _fake()
    Hotload(fake).call(0x44ABCDE1, code=struct.pack("<I", 0))
    assert fake.retries_at_echo == [1]
    assert fake.retries == 3
    assert fake.lines.count("echo") == 1


def test_call_refuses_borrowed_echo_slot() -> None:
    fake = _fake({ECHO_SLOT: 0xC0FFFFFF})
    _expect(RefuseError, Hotload(fake).call, 0x44ABCDE1,
            code=struct.pack("<I", 0))


def test_call_refuses_null_function() -> None:
    _expect(UsageError, Hotload(_fake()).call, 0)


# -- loader contract -----------------------------------------------------


def test_book_rejects_garbage_and_old_version() -> None:
    _expect(BookError, Hotload(_fake({SL_BOOK_AT: 0})).book)
    _expect(BookError, Hotload(_fake({SL_BOOK_AT: 0x44A00000,
                                      0x44A00000: 5})).book)
    assert Hotload(_fake({SL_BOOK_AT: 0x44A00000,
                          0x44A00000: 6})).book() == (0x44A00000, 6)


def test_reload_calls_the_pointer_not_the_slot() -> None:
    book, entry = 0x44A00000, 0x44ABCDE1
    fake = _fake({SL_BOOK_AT: book, book: 6, book + RELOAD_OFF: entry},
                 echo_result=(0, DONE))
    res = Hotload(fake).reload()
    assert res == {"return": 0, "book": book, "entry": entry}
    assert fake.mem[P_BLOCK + P_FN] == entry
    assert fake.mem[P_BLOCK + P_FN] != book + RELOAD_OFF
    assert fake.mem[P_BLOCK + P_R0] == book
    assert fake.mem[P_BLOCK + P_R1] == LXPO_ID


def test_release_uses_offset_44() -> None:
    book, entry = 0x44A00000, 0x44BBBBE1
    fake = _fake({SL_BOOK_AT: book, book: 6, book + RELEASE_OFF: entry},
                 echo_result=(0, DONE))
    res = Hotload(fake).release()
    assert res["entry"] == entry and res["return"] == 0
    assert fake.mem[P_BLOCK + P_R0] == book
    assert fake.mem[P_BLOCK + P_R1] == LXPO_ID


def test_allsups_layout_uses_shifted_offsets() -> None:
    book, entry = 0x44A00000, 0x44BBBBE1
    fake = _fake({SL_BOOK_AT: book, book: 6, book + 48: entry},
                 echo_result=(0, DONE))
    res = Hotload(fake, layout="allsups").release()
    assert res["entry"] == entry


def test_load_copies_short_path() -> None:
    book, entry = 0x44A00000, 0x44CCCCE1
    fake = _fake({SL_BOOK_AT: book, book: 6, book + LOAD_ONE_OFF: entry},
                 echo_result=(0, DONE))
    res = Hotload(fake).load(path="\\fpSup\\L.BIN")
    assert res["return"] == 0 and res["path_addr"] == PATH_BUF
    assert fake.mem[P_BLOCK + P_R1] == PATH_BUF
    raw = b"".join(struct.pack("<I", fake.mem.get(PATH_BUF + i * 4, 0))
                   for i in range(PATH_BUF_BYTES // 4))
    assert raw.startswith(b"\\fpSup\\L.BIN\0")


def test_load_rejects_long_path() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + LOAD_ONE_OFF: 0x44CCCCE1})
    _expect(UsageError, Hotload(fake).load,
            "\\fpSup\\" + "X" * 40 + ".BIN")


def test_load_rejects_non_ascii_path() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + LOAD_ONE_OFF: 0x44CCCCE1})
    _expect(UsageError, Hotload(fake).load, "\\fpSup\\é.BIN")


def test_load_rejects_dirty_scratch() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + LOAD_ONE_OFF: 0x44CCCCE1, PATH_BUF: 1})
    _expect(RefuseError, Hotload(fake).load, "\\fpSup\\L.BIN")


def test_load_reuses_previous_path_scratch() -> None:
    book = 0x44A00000
    old = b"\\fpSup\\OLD.BIN\0"
    old = old + b"\0" * (-len(old) % 4)
    words = struct.unpack(f"<{len(old) // 4}I", old)
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + LOAD_ONE_OFF: 0x44CCCCE1,
                  **{PATH_BUF + i * 4: word for i, word in enumerate(words)}},
                 echo_result=(0, DONE))
    res = Hotload(fake).load(path="\\fpSup\\NEW.BIN")
    assert res["return"] == 0 and res["path_addr"] == PATH_BUF


def test_load_accepts_existing_path_pointer() -> None:
    book, addr = 0x44A00000, 0x44C00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + LOAD_ONE_OFF: 0x44CCCCE1})
    res = Hotload(fake).load(path_addr=addr)
    assert res["path_addr"] == addr
    assert fake.mem[P_BLOCK + P_R1] == addr


def test_load_argument_validation() -> None:
    link = Hotload(_fake())
    _expect(UsageError, link.load)
    _expect(UsageError, link.load, "\\fpSup\\L.BIN", PATH_BUF)
    _expect(UsageError, link.load, path_addr=0)


def test_entry_pointer_is_validated() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6, book + RELOAD_OFF: 0})
    _expect(EntryError, Hotload(fake).reload)
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + RELOAD_OFF: 0x12345678})
    _expect(EntryError, Hotload(fake).reload)


# -- views ---------------------------------------------------------------


def test_verify_sites_states() -> None:
    cases = ((0xEB000000, True), (STOCK_LV_WORD, True),
             (0x12345678, False))
    for lv, expected in cases:
        fake = _fake({SITE_PATCH: PATCHED_WORD, SITE_LV: lv})
        assert Hotload(fake).verify_sites()["ok"] is expected, hex(lv)
    fake = _fake({SITE_PATCH: STOCK_PATCH_WORD, SITE_LV: STOCK_LV_WORD})
    assert Hotload(fake).verify_sites()["ok"] is False
    assert Hotload(fake).verify_released()["ok"] is True


def test_snapshot_omits_lv_done_when_disarmed() -> None:
    fake = _fake({SITE_PATCH: PATCHED_WORD, SITE_LV: STOCK_LV_WORD,
                  SL_BOOK_AT: 0x44A00000, 0x44A00000: 6})
    snap = Hotload(fake).snapshot()
    assert snap["site_patch_state"] == "patched"
    assert snap["site_lv_state"] == "disarmed"
    assert snap["lv_done"] is None and snap["lv_done_addr"] is None
    assert snap["svc_version"] == 6
    assert snap["version"].startswith("Ver")


def test_snapshot_reads_lv_done_from_veneer() -> None:
    hook_base = 0x4508C314
    done_addr = hook_base + 0x80
    veneer = 0xC072E100
    off = (veneer - (SITE_LV + 8)) >> 2
    bl = 0xEB000000 | (off & 0xFFFFFF)
    fake = _fake({
        SITE_PATCH: PATCHED_WORD,
        SITE_LV: bl,
        SL_BOOK_AT: 0x44A00000,
        0x44A00000: 6,
        veneer: VENEER_LDR,
        veneer + 4: hook_base,
        done_addr: 1,
    })
    snap = Hotload(fake).snapshot()
    assert snap["site_lv_state"] == "armed"
    assert snap["lv_done"] == 1
    assert snap["lv_done_addr"] == done_addr


def test_snapshot_rejects_a_bl_whose_veneer_is_not_ours() -> None:
    veneer = 0xC072E100
    off = (veneer - (SITE_LV + 8)) >> 2
    bl = 0xEB000000 | (off & 0xFFFFFF)
    fake = _fake({
        SITE_PATCH: PATCHED_WORD,
        SITE_LV: bl,
        SL_BOOK_AT: 0x44A00000,
        0x44A00000: 6,
        veneer: 0xE1A00000,          # mov r0, r0: not the ldr pc veneer
    })
    snap = Hotload(fake).snapshot()
    assert snap["site_lv_state"] == "armed"
    assert snap["lv_done"] is None and snap["lv_done_addr"] is None


def test_probe_reports_loader_missing() -> None:
    fake = _fake({SITE_PATCH: STOCK_PATCH_WORD, SITE_LV: STOCK_LV_WORD,
                  SL_BOOK_AT: 0})
    data = Hotload(fake).probe()
    assert data["loader_ok"] is False
    assert data["svc_version"] is None
    assert data["ok"] is False


def test_probe_ok_when_loader_is_v6() -> None:
    fake = _fake({SITE_PATCH: PATCHED_WORD, SITE_LV: STOCK_LV_WORD,
                  SL_BOOK_AT: 0x44A00000, 0x44A00000: 6})
    data = Hotload(fake).probe()
    assert data["loader_ok"] is True
    assert data["svc_version"] == 6
    assert data["ok"] is True


# -- cycles --------------------------------------------------------------


def test_cycle_verifies_each_iteration() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + RELOAD_OFF: 0x44ABCDE1,
                  SITE_PATCH: PATCHED_WORD, SITE_LV: STOCK_LV_WORD},
                 echo_result=(0, DONE))
    results = Hotload(fake).cycle(3)
    assert len(results) == 3
    assert all(item["ok"] for item in results)


def test_stress_adds_timing_summary() -> None:
    book = 0x44A00000
    fake = _fake({SL_BOOK_AT: book, book: 6,
                  book + RELOAD_OFF: 0x44ABCDE1,
                  SITE_PATCH: PATCHED_WORD, SITE_LV: STOCK_LV_WORD},
                 echo_result=(0, DONE))
    summary = Hotload(fake).stress(2)
    assert summary["ok"] is True
    assert summary["completed"] == 2
    assert summary["min_s"] is not None
    assert summary["avg_s"] is not None
    assert summary["max_s"] is not None


# -- link adapters -------------------------------------------------------


def test_camera_link_propagates_retries_to_session() -> None:
    from fp_companion.hotload.link import CameraLink

    class Session:
        retries = 3

        def sh(self, line):
            return ""

        def mem_get(self, addr, size=4):
            return 0

    session = Session()
    link = CameraLink(session=session)
    assert link.retries == 3
    link.retries = 1
    assert session.retries == 1
    link.retries = 3
    assert session.retries == 3


def test_hotload_one_echo_guard_reaches_the_session() -> None:
    """The retry guard must reach the transport, not stop at the adapter."""
    from fp_companion.hotload.link import CameraLink

    class Session:
        def __init__(self, cam):
            self.cam = cam

        @property
        def retries(self):
            return self.cam.retries

        @retries.setter
        def retries(self, value):
            self.cam.retries = value

        def sh(self, line):
            return self.cam.shl(line).decode("latin-1")

        def mem_get(self, addr, size=4):
            return self.cam.mem.get(addr, 0)

    cam = _fake()
    Hotload(CameraLink(session=Session(cam))).call(
        0x44ABCDE1, code=struct.pack("<I", 0))
    assert cam.retries_at_echo == [1]
    assert cam.retries == 3


def test_direct_link_uses_package_session_not_firmware_tools(monkeypatch) -> None:
    import inspect

    import fp_companion.autotest.session as session_mod
    from fp_companion.hotload import link as link_module

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.retries = int(kwargs.get("retries", 3))
            self.closed = False
            self.calls = []

        def sh(self, line):
            self.calls.append(line)
            return "ok"

        def mem_get(self, addr, size=4):
            return 0

        def close(self):
            self.closed = True

    monkeypatch.setattr(session_mod, "CameraSession", FakeSession)
    monkeypatch.delitem(sys.modules, "fpsh_client", raising=False)

    direct = link_module.DirectLink(retries=3)
    assert isinstance(direct.session, FakeSession)
    assert direct.session.kwargs.get("retries") == 3
    assert direct.camera is direct.session
    assert direct.retries == 3
    assert direct.shl("version") == b"ok"
    assert direct.session.calls == ["version"]
    direct.retries = 1
    assert direct.session.retries == 1
    direct.close()
    assert direct.session.closed
    assert "fpsh_client" not in sys.modules

    source = inspect.getsource(link_module).replace("\\", "/")
    assert "firmware/tools" not in source
    assert "import fpsh_client" not in source
    assert "importlib" not in source
    assert "REPO_ROOT" not in source


# -- embedded bridge -----------------------------------------------------


def test_embedded_callfn_image_matches_the_verified_bridge() -> None:
    assert len(core.CALLFN_IMAGE) == 140
    assert len(core.CALLFN_IMAGE) % 4 == 0
    assert core.CALLFN_IMAGE_SHA256 == (
        "7497a97da0c55a402f62a0a1837530444b64a526c63440fb342d0d8fa7b5a14e"
    )
    assert hashlib.sha256(core.CALLFN_IMAGE).hexdigest() == \
        core.CALLFN_IMAGE_SHA256
    assert core.CALLFN_IMAGE[:4] == struct.pack("<I", 0xE92D4FF8)


def test_call_uses_the_embedded_image_without_clang() -> None:
    fake = _fake()
    Hotload(fake).call(0x44ABCDE1)
    words = struct.unpack(
        f"<{len(core.CALLFN_IMAGE) // 4}I", core.CALLFN_IMAGE)
    assert [fake.mem[CODE_ADDR + i * 4] for i in range(len(words))] == list(words)


def test_importing_hotload_does_not_import_pyusb() -> None:
    import os
    import subprocess
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import sys; import fp_companion.hotload; "
        "print('usb.core' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"
