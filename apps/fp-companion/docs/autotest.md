# Autotest harness

`fp_companion.autotest` is a hardware-in-the-loop harness for SIGMA fp SUP
cards. It drives the fpSup USB shell (FPSH v1) read-only, runs a YAML suite of
expectations, and prints one PASS/FAIL line per step. It is the scripted,
repeatable replacement for the manual probe commands in
[`firmware/WORKFLOW.md`](../../../firmware/WORKFLOW.md) section 7; the physical
shutter and card handling stay manual in Phase 1.

## What it does

- Opens a `CameraSession` on the camera's fpSup shell (`1003:C432`, bulk
  endpoints `0x01`/`0x82`).
- Loads a suite (name, description, ordered steps) and executes each step
  against the session.
- Supports `shell`, `mem_expect`, `mem_expect_any`, `status_expect`, `prompt`
  and `wait` steps.
- Stops at the first failing step unless that step sets
  `continue_on_fail: true`.
- Optionally writes `<suite>-result.json` and `<suite>-report.md` into a report
  directory.
- Returns exit codes suitable for CI or a shell script.

The harness does not flash, format, mount a card, or write memory. Its only
camera channel is the shell; the caller owns the safety of lines in a suite.

## CLI

Run from `apps/fp-companion` with the `fp-companion` environment:

```sh
python -m fp_companion.autotest run <suite> [--report DIR] [--non-interactive] [--timeout-ms N]
python -m fp_companion.autotest doctor [--timeout-ms N]
python -m fp_companion.autotest list
```

`<suite>` is either a path to a `.yaml`/`.json` file, a built-in filename such
as `lexpo.yaml`, or the built-in `name:` value such as `lexpo-fp502`.

| Command | Purpose |
|---|---|
| `run` | Execute a suite. Prints `[PASS]`/`[FAIL]` per step, then `name: PASS (n/m steps)`. `--report DIR` writes JSON and Markdown reports. `--non-interactive` makes prompt steps fail instead of waiting for a key. `--timeout-ms N` overrides the per-shell-reply timeout (default 1000 ms). |
| `doctor` | Read-only connectivity check. Opens a session, prints the `version` reply and general status, and reports whether the shell responds. It assumes no card is mounted. |
| `list` | List the built-in suites in `src/fp_companion/autotest/suites/`. |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Suite passed (or the command was informational) |
| `1` | A suite step failed |
| `2` | Usage error, unknown/malformed suite, or transport/session failure |

Example:

```sh
# from apps/fp-companion
python -m fp_companion.autotest list
python -m fp_companion.autotest doctor
python -m fp_companion.autotest run lexpo-fp502
python -m fp_companion.autotest run lexpo.yaml --report out/autotest --non-interactive
```

## Suite schema

```yaml
name: lexpo-fp502
description: LExpo 40-500 s shutter patch, hook self-disarm, fp 5.02 only
steps:
  - kind: shell
    command: version
    expect_contains: "5.02.0"
  - kind: mem_expect_any
    addr: "0xC02BAD0C"
    values: ["0xE24DD064", "0xEB11CCD5"]
```

`name` and `steps` are required; `description` is optional. Step parameters may
be written inline (as above) or nested under `params:`.

| Kind | Parameters | Behaviour |
|---|---|---|
| `shell` | `command` (required), `expect_contains`, `expect_regex` | Sends one shell line; optionally checks the reply. |
| `mem_expect` | `addr`, `value`, optional `size` (default 4) | Reads a word with `mem get` and compares it exactly. |
| `mem_expect_any` | `addr`, `values` (non-empty list), optional `size` | Passes when the word matches any listed value. |
| `status_expect` | `name`, `value` | Compares `status get <name>` to the expected string. |
| `prompt` | `message` | Prints an operator instruction and waits for Enter. Fails under `--non-interactive`. |
| `wait` | `seconds` (0-600) | Sleeps before the next step. |

Addresses and values accept integers or hex strings with or without `0x`.
Step order is significant: the runner stops at the first failure unless the
step sets `continue_on_fail: true`.

## Phase 1 limits

The current harness is deliberately narrow:

- **Manual shutter.** It cannot press the physical shutter or turn the mode
  dial. A capture suite uses `prompt` to hand control to the operator, then
  `wait` to cover the exposure plus the long-exposure dark frame. The
  `lexpo-fp502` suite sets this out explicitly.
- **Manual card mount.** It cannot eject, mount, or read the SD card. Capture
  verification is still the PC mount plus `verify_capture.py` (WORKFLOW
  sections 9-10).
- **Read-only.** No `mem set`, `mem save`, `prom write`, `sdcard format`, or
  firmware update. Only `version`, `status get`, `mem get`, and suite-authored
  shell lines are sent.
- **Physical preconditions stay human.** STILL switch, firmware 5.02, stable
  power, and the spare card check remain operator responsibilities.
- **No card assumptions.** `doctor` must work with no card inserted.

## How it fits the LExpo workflow

The harness covers the mechanised part of the loop and leaves the physical
steps to a person:

1. **Before capture (WORKFLOW section 7):** `run lexpo-fp502` checks firmware
   5.02, the `0xC007F324` patch word, and the `0xC02BAD0C` hook site (armed
   `BL` or stock/self-disarmed).
2. **Capture (section 8):** the suite's `prompt` asks for M mode, 40 s, and a
   physical shutter press; `wait: 90` covers the 40 s exposure and its dark
   frame. A 500 s pass follows the same pattern with a longer wait.
3. **Card verification (sections 9-10):** still manual and outside the
   harness; mount the card on the PC and run `verify_capture.py`.
4. **Rollback (section 11):** still manual by design.

Because the hook self-disarms, the `mem_expect_any` step accepts either the
armed `BL` word or the stock word that the one-shot hook writes back. The patch
word must remain `0xE309C6FC` throughout.

See [`firmware/WORKFLOW.md`](../../../firmware/WORKFLOW.md) for the complete
preconditions, safety stops, and rollback procedure. A green suite is
necessary, not sufficient: physical shutter behaviour and card contents are
still verified by hand.

## Tests

The CLI is tested without a camera:

```sh
cd apps/fp-companion
/e/Softwares/miniconda/Scripts/conda.exe run --no-capture-output \
  -n fp-companion pytest -q tests/test_autotest_cli.py
```

The tests monkeypatch `CameraSession` with a scripted fake session.
