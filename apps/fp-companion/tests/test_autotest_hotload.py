"""Offline HIL tests for the ``hotload`` autotest step and suite.

The fake session speaks the same ``mem get``/``mem set``/``echo`` lines the
fpSup shell does, so the real :class:`fp_companion.hotload.Hotload` call bridge
runs without USB.  The bridge image is embedded in the package; clang is never
invoked.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from fp_companion.autotest.plan import VALID_STEP_KINDS, Step, load_suite
from fp_companion.autotest.runner import run_suite
from fp_companion.autotest.steps import execute
from fp_companion.hotload import core

BOOK = 0x44A00000
ENTRY_RELEASE = 0x44B000E1
ENTRY_LOAD = 0x44B001E1
ENTRY_RELOAD = 0x44B002E1

SUITE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "fp_companion"
    / "autotest"
    / "suites"
    / "lexpo-hotload.yaml"
)

_MEM_SET_RE = re.compile(
    r"mem set 0x([0-9A-Fa-f]+) 0x([0-9A-Fa-f]+)")
_MEM_GET_RE = re.compile(
    r"mem get 0x([0-9A-Fa-f]+),,(0x[0-9A-Fa-f]+|\d+)")


class FakeSession:
    """Canned firmware memory plus just enough loader behaviour."""

    def __init__(self) -> None:
        self.mem: dict[int, int] = {
            core.SL_BOOK_AT: BOOK,
            BOOK: 6,
            BOOK + core.RELEASE_OFF: ENTRY_RELEASE,
            BOOK + core.LOAD_ONE_OFF: ENTRY_LOAD,
            BOOK + core.RELOAD_OFF: ENTRY_RELOAD,
            core.SITE_PATCH: core.STOCK_PATCH_WORD,
            core.SITE_LV: core.STOCK_LV_WORD,
            core.ECHO_SLOT: core.ECHO_ORIG,
        }
        self.lines: list[str] = []
        self.load_paths: list[str] = []

    def sh(self, line: str) -> str:
        self.lines.append(line)
        if line == "version":
            return "SIGMA fp 5.02.0 (fpSup shell)\n"
        if line == "echo":
            self._run_pending_call()
            return "callfn: returned\n"

        match = _MEM_SET_RE.fullmatch(line)
        if match:
            self.mem[int(match.group(1), 16)] = int(match.group(2), 16)
            return "OK\n"

        match = _MEM_GET_RE.fullmatch(line)
        if match:
            addr = int(match.group(1), 16)
            count = int(match.group(2), 0)
            return "".join(
                f"A:0x{word:08X}, D:0x{self.mem.get(word, 0):08X}\n"
                for word in range(addr, addr + count, 4)
            )
        return ""

    def mem_get(self, addr: int, size: int = 4) -> int:
        return self.mem.get(addr, 0)

    def _run_pending_call(self) -> None:
        fn = self.mem.get(core.P_BLOCK + core.P_FN, 0)
        if fn == ENTRY_RELEASE:
            self.mem[core.SITE_PATCH] = core.STOCK_PATCH_WORD
            self.mem[core.SITE_LV] = core.STOCK_LV_WORD
        elif fn in (ENTRY_LOAD, ENTRY_RELOAD):
            if fn == ENTRY_LOAD:
                raw = b"".join(
                    struct.pack("<I", self.mem.get(core.PATH_BUF + i * 4, 0))
                    for i in range(core.PATH_BUF_BYTES // 4)
                )
                self.load_paths.append(raw.split(b"\0", 1)[0].decode("ascii"))
            self.mem[core.SITE_PATCH] = core.PATCHED_WORD
            # Leave the one-shot LV hook disarmed; verify_sites accepts that.
            self.mem[core.SITE_LV] = core.STOCK_LV_WORD
        self.mem[core.P_BLOCK + core.P_RET] = 0
        self.mem[core.P_BLOCK + core.P_DONE] = core.DONE


def _step(kind: str, **params) -> Step:
    return Step(kind=kind, params=params)


def _exec(step: Step, session: FakeSession):
    return execute(step, session, interactive=False, input_fn=lambda _msg: "")


# -- step registration ---------------------------------------------------


def test_hotload_is_a_valid_step_kind() -> None:
    assert "hotload" in VALID_STEP_KINDS


# -- step behaviour ------------------------------------------------------


def test_probe_step_is_read_only_and_reports_sites() -> None:
    session = FakeSession()
    result = _exec(_step("hotload", action="probe"), session)
    assert result.ok, result.detail
    assert "svc_version=6" in result.observation
    assert "patch=stock" in result.observation
    assert "lv=disarmed" in result.observation
    assert not any(line.startswith("mem set") for line in session.lines)


def test_reload_step_applies_patch_and_verifies() -> None:
    session = FakeSession()
    result = _exec(_step("hotload", action="reload"), session)
    assert result.ok, result.detail
    assert session.mem[core.SITE_PATCH] == core.PATCHED_WORD
    assert "return=0" in result.observation
    assert "patch=patched" in result.observation


def test_release_step_restores_stock_and_verifies() -> None:
    session = FakeSession()
    session.mem[core.SITE_PATCH] = core.PATCHED_WORD
    session.mem[core.SITE_LV] = 0xEB000000
    result = _exec(_step("hotload", action="release"), session)
    assert result.ok, result.detail
    assert session.mem[core.SITE_PATCH] == core.STOCK_PATCH_WORD
    assert session.mem[core.SITE_LV] == core.STOCK_LV_WORD
    assert "patch=stock" in result.observation


def test_load_step_copies_path_and_verifies() -> None:
    session = FakeSession()
    result = _exec(
        _step("hotload", action="load", path="\\fpSup\\30LEXPO.BIN"),
        session,
    )
    assert result.ok, result.detail
    assert session.load_paths[-1] == "\\fpSup\\30LEXPO.BIN"
    assert session.mem[core.SITE_PATCH] == core.PATCHED_WORD
    assert "return=0" in result.observation


def test_cycle_step_runs_every_iteration() -> None:
    session = FakeSession()
    result = _exec(_step("hotload", action="cycle", count=3), session)
    assert result.ok, result.detail
    assert "3/3 iterations passed" in result.observation


def test_stress_step_defaults_to_ten_iterations() -> None:
    session = FakeSession()
    result = _exec(_step("hotload", action="stress"), session)
    assert result.ok, result.detail
    assert "10/10 iterations passed" in result.observation


def test_expect_ok_false_inverts_the_verdict() -> None:
    session = FakeSession()
    session.mem[core.SL_BOOK_AT] = 0  # no resident loader: probe fails
    result = _exec(
        _step("hotload", action="probe", expect_ok=False), session
    )
    assert result.ok, result.detail
    assert "observed failure" in result.detail


def test_probe_failure_is_a_failed_step() -> None:
    session = FakeSession()
    session.mem[core.SL_BOOK_AT] = 0
    result = _exec(_step("hotload", action="probe"), session)
    assert not result.ok
    assert "observed failure" in result.detail


def test_invalid_action_is_rejected() -> None:
    result = _exec(_step("hotload", action="explode"), FakeSession())
    assert not result.ok
    assert "action" in result.detail


def test_load_without_path_is_rejected() -> None:
    result = _exec(_step("hotload", action="load"), FakeSession())
    assert not result.ok
    assert "path" in result.detail


def test_invalid_count_is_rejected() -> None:
    result = _exec(_step("hotload", action="cycle", count=0), FakeSession())
    assert not result.ok
    assert "count" in result.detail

    result = _exec(
        _step("hotload", action="cycle", count="many"), FakeSession()
    )
    assert not result.ok
    assert "count" in result.detail


# -- suite ---------------------------------------------------------------


def test_lexpo_hotload_suite_loads() -> None:
    suite = load_suite(SUITE_PATH)
    assert suite.name == "lexpo-hotload"
    kinds = [step.kind for step in suite.steps]
    assert kinds[0] == "shell"
    assert kinds.count("hotload") == 7
    assert "prompt" not in kinds

    load_steps = [
        step for step in suite.steps
        if step.kind == "hotload" and step.params.get("action") == "load"
    ]
    assert len(load_steps) == 1
    assert load_steps[0].params["path"] == "\\fpSup\\30LEXPO.BIN"

    cycle_steps = [
        step for step in suite.steps
        if step.kind == "hotload" and step.params.get("action") == "cycle"
    ]
    assert len(cycle_steps) == 1
    assert cycle_steps[0].params["count"] == 10


def test_lexpo_hotload_suite_runs_non_interactively() -> None:
    suite = load_suite(SUITE_PATH)
    session = FakeSession()
    result = run_suite(suite, session, interactive=False)
    assert result.ok, [step.detail for step in result.steps]
    assert len(result.steps) == len(suite.steps)

    # The suite ends with the loaded patch applied and a read-only probe.
    assert session.mem[core.SITE_PATCH] == core.PATCHED_WORD
    assert result.steps[-1].kind == "hotload"
