"""CLI tests for the fp-companion autotest harness.

No real camera is opened: every test replaces the CLI's ``CameraSession``
with a scripted fake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fp_companion.autotest import cli


class FakeSession:
    """Scripted stand-in for ``session.CameraSession``."""

    shell_reply = "SIGMA fp 5.02.0 (fpSup shell)"
    status_reply = "status: idle"
    words = {
        0xC007F324: 0xE309C6FC,
        0xC02BAD0C: 0xE24DD064,
    }

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def sh(self, line: str) -> str:
        if line.strip() == "status get":
            return self.status_reply
        return self.shell_reply

    def mem_get(self, addr: int, size: int = 4) -> int:
        return self.words.get(addr, 0x00000000)

    def version(self) -> str:
        return self.shell_reply

    def status(self, name: str = "") -> str:
        return self.status_reply


class WrongMemSession(FakeSession):
    """The patch word is still stock, so a mem expectation must fail."""

    words = {
        0xC007F324: 0xE309C6F4,
        0xC02BAD0C: 0xE24DD064,
    }


FAKE_SUITE = """\
name: fake-suite
description: CLI test suite
steps:
  - kind: shell
    command: version
    expect_contains: "5.02.0"
  - kind: mem_expect
    addr: "0xC007F324"
    value: "0xE309C6FC"
  - kind: mem_expect_any
    addr: "0xC02BAD0C"
    values: ["0xE24DD064", "0xEB11CCD5"]
  - kind: wait
    seconds: 0
"""

FAILING_SUITE = """\
name: failing-suite
steps:
  - kind: mem_expect
    addr: "0xC007F324"
    value: "0xE309C6FC"
"""


def _write_suite(tmp_path: Path, text: str, name: str = "suite.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "run" in output
    assert "doctor" in output
    assert "list" in output


def test_list_includes_builtin_lexpo(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0

    output = capsys.readouterr().out
    assert "lexpo-fp502" in output
    assert "lexpo.yaml" in output


def test_run_fake_suite_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite_path = _write_suite(tmp_path, FAKE_SUITE)
    report_dir = tmp_path / "report"
    monkeypatch.setattr(cli, "CameraSession", FakeSession)

    code = cli.main(
        [
            "run",
            str(suite_path),
            "--non-interactive",
            "--report",
            str(report_dir),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out.count("[PASS]") == 4
    assert "fake-suite: PASS (4/4 steps)" in captured.out
    assert (report_dir / "fake-suite-result.json").is_file()
    payload = json.loads(
        (report_dir / "fake-suite-result.json").read_text(encoding="utf-8")
    )
    assert payload["ok"] is True


def test_failing_mem_expectation_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite_path = _write_suite(tmp_path, FAILING_SUITE)
    monkeypatch.setattr(cli, "CameraSession", WrongMemSession)

    code = cli.main(["run", str(suite_path), "--non-interactive"])
    captured = capsys.readouterr()

    assert code == 1
    assert "[FAIL]" in captured.out
    assert "mem_expect" in captured.out
    assert "failing-suite: FAIL (0/1 steps)" in captured.out


def test_doctor_with_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "CameraSession", FakeSession)

    code = cli.main(["doctor"])
    output = capsys.readouterr().out

    assert code == 0
    assert "5.02.0" in output
    assert "status: idle" in output
    assert "shell: responsive" in output


def test_unknown_suite_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "no-such-suite", "--non-interactive"]) == 2
    assert "suite not found" in capsys.readouterr().err


def test_run_without_suite_is_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run"])

    assert excinfo.value.code == 2
