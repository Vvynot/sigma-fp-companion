# fp Companion

Windows-first desktop companion for the SIGMA fp.

## Run

```bash
conda run -n fp-companion python -m fp_companion
```

On this machine conda lives at `E:\Softwares\miniconda`; use its full path if it
is not on `PATH`. Verified with PySide6 6.8.3 / Qt 6.8.3. The package is
installed editable in the `fp-companion` env. Tests: `pytest -q` from this
directory.

## Modules

- Dashboard — camera/card/firmware status
- Card Studio — fpSup module cards, validation, backup
- LUT Studio — `.cube` import, preview, LUT SUP card
- Monitor — UVC preview, scopes, LUT, false color
- Tether — PTP control, capture, recording (later)
- Offload — checksum copy, proxies, DNG pack (later)

## Current scope

The first milestone is Card Studio: a native GUI for the fpSup card workflow
with dependency/conflict checks, byte-for-byte verification, and backup.

## Card Studio MVP

- Catalogue from `src/fp_companion/data/releases.json` (generated from
  `vendor/fpSup/fpSup/releases`).
- Release list with details, risk warnings, target directory picker, Dry run,
  and Write card.
- Validation: firmware 5.02, system-drive/project refusal, target free space,
  source files present, duplicate paths; warnings for alpha/test modules,
  SSD-only modes, and the stock-restore step.
- Write path: backup the existing card to a timestamped zip, copy only payload
  files listed in `MANIFEST.txt`, hash each file while copying, `os.replace`
  into place, re-read and verify SHA-256, then write `fpsup-manifest.json`.
- End-to-end simulation: `scripts/e2e_card.py` uses a real release and a temp
  target; it never touches a camera or a real SD card.

Not in this MVP: multiple modules merged into one card, LUT/recipe packing,
multi-version dependency resolution, and on-camera acceptance.

## Online debugging and tests

- [Hot reload](docs/hotload.md) — loader v3 service v6, `fp-hotload`, HIL suite.
- [Online tests](docs/online-tests.md) — suites, offline tests, verified results.
- [Windows toolchain](docs/windows.md) — WinUSB, libusb, clang, direct pyusb.

