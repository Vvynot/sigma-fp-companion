# Sigma fp Companion Workspace

Windows-first companion app workspace for the SIGMA fp (firmware 5.02).

## Layout

- `apps/fp-companion/` — application skeleton (PySide6)
- `vendor/` — read-only upstream tools and reference implementations
- `docs/` — architecture and research notes
- `scripts/` — helper scripts

## Environment

```bash
conda env create -f environment.yml
conda run -n fp-companion python -m fp_companion
```

On this machine conda lives at `E:\Softwares\miniconda`; use its full path if it
is not on `PATH`.

Verified 2026-11-02: Python 3.11.18, PySide6 6.8.3 (Qt 6.8.3), pytest 146 passed,
libusb backend available. PySide6 is pinned to 6.8.3 because PySide6 6.12's
Qt6Core imports unversioned ICU symbols while conda-forge ICU 78 exports only
`_78` symbols.

```bash
conda run -n fp-companion pytest -q
conda run -n fp-companion python scripts/verify_qt.py
```

## Current status

- Card Studio MVP: 48-release catalogue (including fallback scans for releases
  without `MANIFEST.txt`), firmware/loader/space/file validation, full-card
  backup, SHA-256 verification, `fpsup-manifest.json`, and a PySide6 UI.
- 146 tests pass; end-to-end temp-target builds verified with `desqueeze` and
  `gyro-base`.
- Not yet done: multi-module merge writing, LUT/recipe packing to a bootable
  SUP card, and real SD/camera validation.

## Milestone 2

- **LUT Studio** (`ui/lut_studio.py`): import and validate `.cube` LUTs, preview
  them, and stage a LUT SUP card layout. Staging only — the result is not
  bootable and no real card is written.
- **UVC Monitor** (`core/uvc.py`, `ui/monitor.py`): UVC live-view plumbing with
  histogram scopes, LUT preview, and false color. It runs offscreen in tests and
  does not require a connected camera.
- **Compatibility planner** (`core/merge.py`): computes a multi-module
  compatibility plan for combining release modules. Planner only — it does not
  write a merged card.
- **CI** (`.github/workflows/tests.yml`, `docs/ci.md`): runs the pytest suite on
  pushes and pull requests.

## Safety

The fp is a single production body. Test cards are alpha; back up settings and
restore a stock color mode before removing a card. Never run `prom write` or
write calibration data.
