"""Execution of individual harness steps.

Each supported step kind is handled by one small function.  Failures are
reported as ``StepResult(ok=False, ...)`` rather than raised, so the runner can
continue through a suite when ``continue_on_fail`` is set.
"""

from __future__ import annotations

import re
import time

from .plan import Step
from .runner import StepResult

_SNIPPET = 300


def _as_int(value: object, field: str) -> int:
    """Parse an int or hex string (``"0xC007F324"`` / ``"C007F324"``)."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} is empty")
        try:
            return int(text, 16)  # accepts an optional 0x prefix
        except ValueError as exc:
            raise ValueError(f"{field} is not a hex integer: {value!r}") from exc
    raise ValueError(f"{field} must be an integer or hex string, got {type(value).__name__}")


def _word(value: int) -> str:
    if 0 <= value <= 0xFFFFFFFF:
        return f"0x{value:08X}"
    return f"0x{value:X}"


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    flat = text.replace("\r", " ").replace("\n", " ")
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3] + "..."


def _result(
    step: Step, ok: bool, detail: str, observation: str = "", index: int = -1
) -> StepResult:
    return StepResult(
        index=index, kind=step.kind, ok=ok, detail=detail, observation=observation
    )


def _shell(step: Step, session: object) -> StepResult:
    command = step.params.get("command")
    if not isinstance(command, str) or not command.strip():
        return _result(step, False, "shell step needs a non-empty 'command'")

    try:
        reply = session.sh(command)
    except Exception as exc:  # transport failures are step failures
        return _result(step, False, f"shell command failed: {exc}")

    contains = step.params.get("expect_contains")
    regex = step.params.get("expect_regex")

    if contains is not None:
        expected = str(contains)
        if expected not in reply:
            return _result(
                step,
                False,
                f"expected reply to contain {expected!r}; "
                f"observed {_snippet(reply)!r}",
                reply,
            )

    if regex is not None:
        pattern = str(regex)
        try:
            matched = re.search(pattern, reply) is not None
        except re.error as exc:
            return _result(step, False, f"invalid expect_regex {pattern!r}: {exc}", reply)
        if not matched:
            return _result(
                step,
                False,
                f"expected reply to match /{pattern}/; "
                f"observed {_snippet(reply)!r}",
                reply,
            )

    if contains is not None and regex is not None:
        detail = f"reply contained {str(contains)!r} and matched /{regex}/"
    elif contains is not None:
        detail = f"reply contained {str(contains)!r}"
    elif regex is not None:
        detail = f"reply matched /{regex}/"
    else:
        detail = "command succeeded"
    return _result(step, True, detail, reply)


def _mem_expect(step: Step, session: object) -> StepResult:
    try:
        addr = _as_int(step.params.get("addr"), "addr")
        value = _as_int(step.params.get("value"), "value")
        size = int(step.params.get("size", 4))
    except (ValueError, TypeError) as exc:
        return _result(step, False, f"invalid mem_expect parameters: {exc}")

    try:
        observed = session.mem_get(addr, size)
    except Exception as exc:
        return _result(step, False, f"mem_get 0x{addr:08X} failed: {exc}")

    observation = _word(observed)
    ok = observed == value
    detail = (
        f"addr 0x{addr:08X}: expected {_word(value)}, observed {_word(observed)}"
    )
    return _result(step, ok, detail, observation)


def _mem_expect_any(step: Step, session: object) -> StepResult:
    raw_values = step.params.get("values")
    if not isinstance(raw_values, (list, tuple)) or not raw_values:
        return _result(step, False, "mem_expect_any needs a non-empty 'values' list")
    try:
        addr = _as_int(step.params.get("addr"), "addr")
        values = [_as_int(item, "values[]") for item in raw_values]
        size = int(step.params.get("size", 4))
    except (ValueError, TypeError) as exc:
        return _result(step, False, f"invalid mem_expect_any parameters: {exc}")

    try:
        observed = session.mem_get(addr, size)
    except Exception as exc:
        return _result(step, False, f"mem_get 0x{addr:08X} failed: {exc}")

    observation = _word(observed)
    expected = " or ".join(_word(item) for item in values)
    ok = observed in values
    detail = f"addr 0x{addr:08X}: expected {expected}, observed {_word(observed)}"
    return _result(step, ok, detail, observation)


def _status_expect(step: Step, session: object) -> StepResult:
    name = step.params.get("name")
    if not isinstance(name, str) or not name.strip():
        return _result(step, False, "status_expect needs a non-empty 'name'")
    if "value" not in step.params:
        return _result(step, False, "status_expect needs a 'value'")
    expected = str(step.params["value"])

    try:
        observed = session.status(name)
    except Exception as exc:
        return _result(step, False, f"status get {name} failed: {exc}")

    ok = observed == expected
    detail = f"status {name!r}: expected {expected!r}, observed {observed!r}"
    return _result(step, ok, detail, observed)


def _prompt(step: Step, session: object, *, interactive: bool, input_fn) -> StepResult:
    message = step.params.get("message", "")
    message = str(message)
    print(message)
    if not interactive:
        return _result(
            step, False, f"interactive input required for prompt: {message!r}"
        )
    try:
        answer = input_fn(message)
    except Exception as exc:
        return _result(step, False, f"prompt input failed: {exc}")
    return _result(
        step, True, f"operator acknowledged prompt: {message!r}", str(answer)
    )


def _wait(step: Step) -> StepResult:
    raw = step.params.get("seconds")
    if isinstance(raw, bool):
        return _result(step, False, "wait 'seconds' must be a number")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return _result(step, False, f"wait needs a numeric 'seconds', got {raw!r}")
    if not 0.0 <= seconds <= 600.0:
        return _result(step, False, f"wait seconds out of range 0..600: {seconds}")

    time.sleep(seconds)
    return _result(step, True, f"waited {seconds:g}s")


_HOTLOAD_ACTIONS = frozenset(
    {"probe", "snapshot", "release", "load", "reload", "cycle", "stress"}
)


def _hotload(step: Step, session: object) -> StepResult:
    """Drive :mod:`fp_companion.hotload` over the live harness session.

    ``action`` is one of ``probe``, ``snapshot``, ``release``, ``load``,
    ``reload``, ``cycle`` or ``stress``.  ``path`` is required for ``load`` and
    ``count`` selects the number of iterations for ``cycle`` (default 1) or
    ``stress`` (default 10).  ``expect_ok`` (default true) says whether the
    operation is expected to succeed; set it false for a negative test.

    The site words are verified automatically: ``release`` uses
    ``verify_released()``; ``load``/``reload``/``cycle``/``stress`` use
    ``verify_sites()``.
    """
    raw_action = step.params.get("action")
    if (not isinstance(raw_action, str)
            or raw_action.strip().lower() not in _HOTLOAD_ACTIONS):
        allowed = ", ".join(sorted(_HOTLOAD_ACTIONS))
        return _result(
            step, False,
            f"hotload needs an 'action' of {allowed}; got {raw_action!r}",
        )
    action = raw_action.strip().lower()

    default_count = 10 if action == "stress" else 1
    raw_count = step.params.get("count", default_count)
    if isinstance(raw_count, bool):
        return _result(step, False, "hotload 'count' must be an integer")
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return _result(
            step, False, f"hotload 'count' must be an integer, got {raw_count!r}"
        )
    if count < 1:
        return _result(step, False, f"hotload 'count' must be >= 1, got {count}")

    expect_ok = step.params.get("expect_ok", True)
    if not isinstance(expect_ok, bool):
        return _result(step, False, "hotload 'expect_ok' must be a boolean")

    path = step.params.get("path")
    if action == "load":
        if not isinstance(path, str) or not path:
            return _result(
                step, False, "hotload 'load' needs a non-empty 'path'")
    elif path is not None and not isinstance(path, str):
        return _result(step, False, "hotload 'path' must be a string")

    try:
        from ..hotload.core import Hotload
        from ..hotload.link import CameraLink

        hotload = Hotload(CameraLink(session=session))

        if action == "probe":
            data = hotload.probe()
            actual = bool(data["ok"])
            observation = (
                f"loader_ok={data['loader_ok']} "
                f"svc_version={data['svc_version']} "
                f"patch={data['site_patch_state']} "
                f"lv={data['site_lv_state']}"
            )
        elif action == "snapshot":
            data = hotload.snapshot()
            actual = bool(data["ok"])
            lv_done = ("n/a" if data["lv_done"] is None
                       else str(data["lv_done"]))
            observation = (
                f"loader_ok={data['loader_ok']} "
                f"patch={data['site_patch_state']} "
                f"lv={data['site_lv_state']} lv_done={lv_done}"
            )
        elif action == "release":
            res = hotload.release()
            sites = hotload.verify_released()
            actual = res["return"] == 0 and bool(sites["ok"])
            observation = (
                f"return={res['return']} "
                f"patch={sites['patch_state']} lv={sites['lv_state']}"
            )
        elif action == "reload":
            res = hotload.reload()
            sites = hotload.verify_sites()
            actual = res["return"] == 0 and bool(sites["ok"])
            observation = (
                f"return={res['return']} "
                f"patch={sites['patch_state']} lv={sites['lv_state']}"
            )
        elif action == "load":
            res = hotload.load(path=path)
            sites = hotload.verify_sites()
            actual = res["return"] == 0 and bool(sites["ok"])
            observation = (
                f"return={res['return']} path_addr=0x{res['path_addr']:08X} "
                f"patch={sites['patch_state']} lv={sites['lv_state']}"
            )
        elif action == "cycle":
            results = hotload.cycle(count)
            passed = sum(1 for item in results if item["ok"])
            actual = len(results) == count and passed == count
            observation = f"{passed}/{count} iterations passed"
        else:  # stress
            summary = hotload.stress(count)
            passed = sum(1 for item in summary["results"] if item["ok"])
            actual = bool(summary["ok"])
            timing = ""
            if summary["min_s"] is not None:
                timing = (
                    f"; min/avg/max "
                    f"{summary['min_s'] * 1000:.1f}/"
                    f"{summary['avg_s'] * 1000:.1f}/"
                    f"{summary['max_s'] * 1000:.1f} ms"
                )
            observation = f"{passed}/{count} iterations passed{timing}"
    except Exception as exc:  # transport, usage and verification failures
        return _result(step, False, f"hotload {action} failed: {exc}")

    ok = actual == expect_ok
    expected = "success" if expect_ok else "failure"
    detail = (
        f"hotload {action}: observed {'success' if actual else 'failure'} "
        f"({observation}); expected {expected}"
    )
    return _result(step, ok, detail, observation)


def execute(step: Step, session: object, *, interactive: bool, input_fn) -> StepResult:
    """Execute one step and return its result (never raises for step failures)."""
    if step.kind == "shell":
        return _shell(step, session)
    if step.kind == "mem_expect":
        return _mem_expect(step, session)
    if step.kind == "mem_expect_any":
        return _mem_expect_any(step, session)
    if step.kind == "status_expect":
        return _status_expect(step, session)
    if step.kind == "prompt":
        return _prompt(step, session, interactive=interactive, input_fn=input_fn)
    if step.kind == "wait":
        return _wait(step)
    if step.kind == "hotload":
        return _hotload(step, session)
    return _result(step, False, f"unknown step kind {step.kind!r}")
