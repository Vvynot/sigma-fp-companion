from __future__ import annotations

import json
from pathlib import Path

import pytest

from fp_companion.autotest.plan import Step, Suite, load_suite
from fp_companion.autotest.runner import run_suite
from fp_companion.autotest.session import ShellError
from fp_companion.autotest.steps import execute

ADDR = 0xC007F324
PATCHED = 0xE309C6FC


class FakeSession:
    """Canned shell/memory/status replies; never touches USB."""

    def __init__(self, replies=None, memory=None, statuses=None):
        self.replies = dict(replies or {})
        self.memory = dict(memory or {})
        self.statuses = dict(statuses or {})
        self.calls: list[tuple] = []

    def sh(self, line: str) -> str:
        self.calls.append(("sh", line))
        if line not in self.replies:
            raise ShellError(f"no canned reply for {line!r}")
        reply = self.replies[line]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def mem_get(self, addr: int, size: int = 4) -> int:
        self.calls.append(("mem_get", addr, size))
        if addr not in self.memory:
            raise ShellError(f"no memory at 0x{addr:08X}")
        return self.memory[addr]

    def status(self, name: str) -> str:
        self.calls.append(("status", name))
        if name not in self.statuses:
            raise ShellError(f"no status {name!r}")
        return self.statuses[name]


def _step(kind: str, **params) -> Step:
    return Step(kind=kind, params=params)


def _exec(step, session, *, interactive=False, input_fn=None):
    return execute(
        step,
        session,
        interactive=interactive,
        input_fn=input_fn if input_fn is not None else lambda _msg: "",
    )


# -- shell ---------------------------------------------------------------


def test_shell_expect_contains_pass():
    session = FakeSession(replies={"version": "fpSup 5.02\n"})
    result = _exec(
        _step("shell", command="version", expect_contains="fpSup"), session
    )
    assert result.ok
    assert result.kind == "shell"
    assert result.observation == "fpSup 5.02\n"
    assert "fpSup" in result.detail


def test_shell_expect_contains_fail_shows_observed():
    session = FakeSession(replies={"version": "other 1.0"})
    result = _exec(
        _step("shell", command="version", expect_contains="fpSup"), session
    )
    assert not result.ok
    assert "expected" in result.detail.lower()
    assert "other 1.0" in result.detail


def test_shell_expect_regex_pass_and_fail():
    session = FakeSession(replies={"version": "fpSup 5.02"})
    good = _exec(
        _step("shell", command="version", expect_regex=r"fpSup\s+\d+\.\d+"),
        session,
    )
    assert good.ok

    bad = _exec(
        _step("shell", command="version", expect_regex=r"^XX"), session
    )
    assert not bad.ok
    assert "fpSup 5.02" in bad.detail


def test_shell_transport_error_is_a_failed_step():
    session = FakeSession()
    result = _exec(_step("shell", command="version"), session)
    assert not result.ok
    assert "failed" in result.detail.lower()


# -- memory --------------------------------------------------------------


def test_mem_expect_pass_with_hex_strings():
    session = FakeSession(memory={ADDR: PATCHED})
    result = _exec(
        _step("mem_expect", addr="0xC007F324", value="E309C6FC"), session
    )
    assert result.ok
    assert result.observation == "0xE309C6FC"
    assert session.calls == [("mem_get", ADDR, 4)]


def test_mem_expect_fail_reports_observed_vs_expected():
    session = FakeSession(memory={ADDR: 0xE309C6F4})
    result = _exec(_step("mem_expect", addr=ADDR, value=PATCHED), session)
    assert not result.ok
    assert "0xE309C6FC" in result.detail  # expected
    assert "0xE309C6F4" in result.detail  # observed


def test_mem_expect_any_pass_and_fail():
    session = FakeSession(memory={ADDR: 0xE24DD064})
    ok = _exec(
        _step(
            "mem_expect_any",
            addr="0xC007F324",
            values=[0xE309C6FC, "0xE24DD064"],
        ),
        session,
    )
    assert ok.ok

    bad = _exec(
        _step("mem_expect_any", addr=ADDR, values=[1, 2]), session
    )
    assert not bad.ok


# -- status --------------------------------------------------------------


def test_status_expect_pass_and_fail():
    session = FakeSession(statuses={"app_current": "app"})
    ok = _exec(
        _step("status_expect", name="app_current", value="app"), session
    )
    assert ok.ok
    assert ok.observation == "app"

    bad = _exec(
        _step("status_expect", name="app_current", value="other"), session
    )
    assert not bad.ok
    assert "expected" in bad.detail.lower()


# -- prompt / wait -------------------------------------------------------


def test_prompt_non_interactive_fails_without_reading_input():
    called: list[str] = []

    def input_fn(message: str) -> str:
        called.append(message)
        return "yes"

    session = FakeSession()
    result = _exec(
        _step("prompt", message="Insert the card"),
        session,
        interactive=False,
        input_fn=input_fn,
    )
    assert not result.ok
    assert called == []


def test_prompt_interactive_passes_with_input_fn():
    called: list[str] = []

    def input_fn(message: str) -> str:
        called.append(message)
        return "yes"

    result = _exec(
        _step("prompt", message="Insert the card"),
        FakeSession(),
        interactive=True,
        input_fn=input_fn,
    )
    assert result.ok
    assert called == ["Insert the card"]


def test_wait_zero_seconds_and_range_check():
    assert _exec(_step("wait", seconds=0), FakeSession()).ok
    assert not _exec(_step("wait", seconds=601), FakeSession()).ok


# -- runner --------------------------------------------------------------


def _suite(*steps: Step) -> Suite:
    return Suite(name="demo", description="a demo suite", steps=list(steps))


def test_runner_stops_on_first_failure():
    session = FakeSession(
        replies={
            "a": "ok",
            "bad": "no",
            "c": "ok",
        }
    )
    suite = _suite(
        _step("shell", command="a", expect_contains="ok"),
        _step("shell", command="bad", expect_contains="yes"),
        _step("shell", command="c", expect_contains="ok"),
    )
    result = run_suite(suite, session, interactive=False)
    assert not result.ok
    assert [step.index for step in result.steps] == [0, 1]
    assert result.steps[0].ok and not result.steps[1].ok
    assert len(session.calls) == 2  # third step never ran


def test_runner_continues_with_continue_on_fail():
    session = FakeSession(replies={"bad": "no", "after": "ok"})
    suite = _suite(
        _step(
            "shell",
            command="bad",
            expect_contains="yes",
            continue_on_fail=True,
        ),
        _step("shell", command="after", expect_contains="ok"),
    )
    result = run_suite(suite, session, interactive=False)
    assert not result.ok
    assert len(result.steps) == 2
    assert not result.steps[0].ok
    assert result.steps[1].ok


def test_runner_writes_json_and_markdown_reports(tmp_path: Path):
    session = FakeSession(
        replies={"version": "fpSup 5.02"},
        memory={ADDR: PATCHED},
    )
    suite = _suite(
        _step("shell", command="version", expect_contains="fpSup"),
        _step("mem_expect", addr="0xC007F324", value="0xE309C6FC"),
    )
    result = run_suite(
        suite, session, interactive=False, report_dir=tmp_path
    )
    assert result.ok
    assert result.suite_name == "demo"
    assert result.started and result.ended

    json_path = tmp_path / "demo-result.json"
    md_path = tmp_path / "demo-report.md"
    assert json_path.is_file()
    assert md_path.is_file()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["suite"] == "demo"
    assert payload["ok"] is True
    assert [step["index"] for step in payload["steps"]] == [0, 1]
    assert payload["steps"][1]["kind"] == "mem_expect"

    markdown = md_path.read_text(encoding="utf-8")
    assert "# demo" in markdown
    assert "PASS" in markdown
    assert "mem_expect" in markdown


# -- suites --------------------------------------------------------------


def test_load_suite_yaml(tmp_path: Path):
    path = tmp_path / "suite.yaml"
    path.write_text(
        """
name: yaml-suite
description: from yaml
steps:
  - kind: shell
    params:
      command: version
      expect_contains: fp
  - kind: mem_expect
    addr: 0xC007F324
    value: 0xE309C6FC
""",
        encoding="utf-8",
    )
    suite = load_suite(path)
    assert suite.name == "yaml-suite"
    assert suite.description == "from yaml"
    assert [step.kind for step in suite.steps] == ["shell", "mem_expect"]
    assert suite.steps[0].params["command"] == "version"
    assert suite.steps[1].params["addr"] == ADDR
    assert suite.steps[1].params["value"] == PATCHED


def test_load_suite_json(tmp_path: Path):
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "name": "json-suite",
                "description": "from json",
                "steps": [
                    {
                        "kind": "status_expect",
                        "name": "app_current",
                        "value": "app",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    suite = load_suite(path)
    assert suite.name == "json-suite"
    assert suite.steps[0].kind == "status_expect"
    assert suite.steps[0].params == {"name": "app_current", "value": "app"}


def test_load_suite_rejects_unknown_kind(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\ndescription: nope\nsteps:\n  - kind: exec\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown kind"):
        load_suite(path)


def test_libusb_path_setup_is_released_after_import(monkeypatch):
    """The dev DLL directory must not stay on the process search path.

    The transport borrows libusb from a conda ``Library/bin`` that also holds
    shared libraries Qt resolves by name (libcrypto, icu, harfbuzz).  Leaving
    the directory on ``PATH`` makes a later PySide6 import load the wrong
    copies and fail, which is exactly what the released resources prevent.
    """
    from fp_companion.autotest import session

    monkeypatch.setenv("PATH", "original")
    closed = []

    class Handle:
        def close(self):
            closed.append(True)

    session._release_libusb_paths([[Handle()], "original"])
    assert closed == [True]
    assert session.os.environ["PATH"] == "original"


def test_session_import_restores_path_and_loads_libusb():
    from fp_companion.autotest import session

    suffixes = ("library/bin", "site-packages/usb1")
    parts = [
        part.replace(chr(92), "/").lower()
        for part in session.os.environ.get("PATH", "").split(session.os.pathsep)
    ]
    assert not any(part.endswith(suffixes) for part in parts)
    assert session.usb.backend.libusb1._lib_object is not None
