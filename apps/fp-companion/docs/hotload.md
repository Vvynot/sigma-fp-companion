# Hot reload (fpSup loader v3, service v6)

Runtime release / reload of a static-patch SUP without a power cycle. The card
still boots through the normal `AutoRun.txt` → `LOADER.BIN` → `*.BIN` chain;
nothing is flashed.

## What is supported

- `release(svc, sup_id)` — restore the SUP's journaled firmware words, drop its
  claims, quiesce, and free its block.
- `load_one(svc, path)` — load one `\fpSup\NAME.BIN` now, reusing a released
  slot when possible.
- `reload(svc, sup_id)` — validate the SUP's own file first, then release and
  load it. A bad file leaves the old SUP loaded.
- `SL_BOOK_AT = 0xC072F6F0` — the service-table pointer, valid for the whole
  boot.

LIFO only: a SUP can be released when every kept SUP after it made no journal
entries and added no claims. Static patch SUPs are safe; SUPs that install
CHAIN/SHARED_UI hooks or create tasks are not hot-unloadable. Cave veneers are
bump-allocated (about 8 B per reload) and are not freed.

## Host library

`fp_companion.hotload` accepts any transport with `shl(line)`; the Windows
transport is direct pyusb (FPSH v1, interface 0, EP 0x01/0x82). No Unix socket
daemon is required. The `callfn.S` bridge (140 bytes, assembled for
`P = 0xC072F700`) is embedded in the package, so a wheel install needs neither
clang nor the firmware checkout. clang is only an optional rebuild aid
reported by `doctor`.

```bash
fp-hotload doctor                 # WinUSB, libusb, optional clang, svc version, sites
fp-hotload probe                  # read-only
fp-hotload snapshot --json
fp-hotload reload --yes
fp-hotload cycle --n 10 --yes
fp-hotload stress --n 20 --yes
fp-hotload release --yes
fp-hotload load --path '\fpSup\30LEXPO.BIN' --yes
```

Writes print a plan and require `--yes`. `probe`/`snapshot` never write.

## Hardware-in-the-loop suite

```bash
cd apps/fp-companion
PYTHONPATH=src python -m fp_companion.autotest run lexpo-hotload \
  --non-interactive --report ../../firmware/out/reports
```

The suite runs `version`, `probe`, `reload`, `snapshot`, ten reload cycles,
`release`, a stock-site assertion, `load_one(path)`, and a final `probe`.

## Verified on camera (2026-11-03)

- fp 5.02, loader `v3` service `6`, `SL_BOOK_AT = 0x45038C58`.
- First reload `SL_OK`; hook re-armed and self-disarmed on the next LV frame.
- 50 consecutive reloads, then 20 more through `fp-hotload stress`: all PASS.
- Release → stock sites; cold boot re-applies; `poff 3` wake leaves the patch
  in place.
- `LOAD.LOG` reuses the same block (`0x45028040`, 0x2FC bytes) with fresh
  claims and no conflicts.
- HIL suite `lexpo-hotload`: 10/10 steps PASS.

## Safety

- Call from a task context; release performs a 100 ms busy-wait quiesce.
- Never release the loader, its journal, or its power-off registration.
- Do not hot-unload a SUP with a task, firmware observer, CHAIN or SHARED_UI
  layer. Reboot for those.
- A wrong function address passed to `callfn` freezes the shell; keep a battery
  pull available.
