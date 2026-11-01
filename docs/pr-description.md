# fp Companion — Card Studio MVP

## Summary

Adds a Windows-first companion app for preparing SIGMA fp cards offline.

- Catalogue of 48 fpSup release directories, parsed from `MANIFEST.txt` where
  present and by hashing `AutoRun.txt` + payloads where absent.
- Validation before any write: firmware 5.02, target directory, system-drive and
  project-tree refusal, free space, missing source files, duplicate paths.
- Safe write path: full-card backup zip, per-file temp write, SHA-256 while
  copying, `os.replace`, final read-back verification, and
  `fpsup-manifest.json`.
- PySide6 Card Studio tab: release list, details, risk warnings, target picker,
  Dry run, and Write card.

## Test plan

- `pytest -q` -> 41 passed.
- Offscreen Qt check -> `fp Companion tabs=6`.
- End-to-end temp-target builds:
  - `fpsup-desqueeze-v0.1.4test`: 8 files, 313,428 bytes, 8 verified.
  - `fpsup-gyro-base-v1.14.0` (fallback, no MANIFEST): 2 files, 65,540 bytes,
    2 verified.

## Not included

- Multiple modules merged into one card.
- LUT/recipe packing.
- Real SD card or camera validation.

## Safety

Card writing is conservative: backup first, payload-only copy, per-file hash
verification, and a manifest. The app refuses the project tree and the system
drive root; non-system drive roots are allowed for SD cards.
