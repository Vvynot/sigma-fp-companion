## Summary

Milestone 2 adds the LUT Studio and UVC Monitor tabs to fp Companion, a
multi-module compatibility planner in core, and a Windows GitHub Actions test
workflow. The existing Dashboard, Card Studio, Tether, and Offload tabs are
unchanged, and the app still has six tabs.

## What changed

- **LUT Studio** (`core/lut.py`, `core/lut_card.py`, `ui/lut_studio.py`):
  extended `.cube` parsing and validation, preview support, and staging of a
  LUT SUP directory (`LUT/source.cube` plus `settings.json`).
- **UVC Monitor** (`core/uvc.py`, `ui/monitor.py`): camera-free numpy helpers
  for LUT application, false color, luma histogram, and waveform, plus a
  monitor widget with histogram scopes and false-color preview.
- **Compatibility planner** (`core/merge.py`): checks whether a selection of
  scanned releases can share one card — same firmware, same loader, distinct
  modules, and no unexpected payload-path conflicts.
- **Integration** (`app.py`): `LutStudioWidget()` and `MonitorWidget()` replace
  the LUT Studio and Monitor placeholders.
- **CI** (`.github/workflows/tests.yml`, `docs/ci.md`): runs the pytest suite
  and the offscreen Qt smoke test on `windows-latest`.
- **Tests**: new coverage in `test_lut.py`, `test_lut_ui.py`, `test_uvc.py`,
  `test_monitor_ui.py`, and `test_merge.py`.

## Test results

- Full suite from `apps/fp-companion`: `146 passed` in 1.44s
  (`conda run -n fp-companion pytest -q`).
- Offscreen Qt verification: `fp Companion tabs=6`, exit code 0
  (`QT_QPA_PLATFORM=offscreen ... python scripts/verify_qt.py`).

## Limitations

- LUT Studio is **staging only**. It writes no firmware, loader, `AutoRun.txt`,
  `LUT.BIN`, or other bootable artifact; `settings.json` records
  `"bootable": false`, and the result is not bootable on the camera.
- The merge module is a **planner only**. It does not write a merged card or
  regenerate shared `AutoRun.txt`/loader files, and its eight-module limit has
  not been verified on hardware.
- The UVC Monitor does not stream from a camera by default; hardware streaming
  and real-device behavior are not exercised by the tests.
- No real SD card was written and no camera was connected.
- The vendor tree was not modified.

## Safety

Card Studio remains validated and non-destructive unless explicitly told to
write. Keep a backup of settings, and restore a stock color mode before
removing a test card. Never run `prom write` or write calibration data.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
