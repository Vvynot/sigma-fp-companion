# Online tests

Three kinds of check run against a live fp over the fpSup USB shell. None of
them writes a card or changes a camera setting unless the test explicitly says
so.

## Hot reload

```bash
cd apps/fp-companion
PYTHONPATH=src python -m fp_companion.autotest run lexpo-hotload \
  --non-interactive --report ../../firmware/out/reports
```

Steps: version, probe, reload, patch assertion, snapshot, ten reload cycles,
release, stock assertion, `load_one(path)`, final probe. The suite is
non-interactive and takes a few seconds.

## Read-only LExpo probe

```bash
PYTHONPATH=src python -m fp_companion.autotest run ../../firmware/out/lexpo-probe.yaml \
  --non-interactive --report ../../firmware/out/reports
```

Checks firmware 5.02, the shutter patch word, and the LV hook word.

## Physical capture suite

`lexpo-fp502` prompts for M mode, a 40 s exposure, the shutter press, and a
90 s wait. It is intentionally interactive; the harness cannot press the
shutter or mount a card.

## Offline test content

- `tests/test_hotload.py` — protocol, callfn, offsets, path scratch, release /
  load / reload / cycle with a fake camera.
- `tests/test_autotest_hotload.py` — the `hotload` step and suite validation.
- `tests/test_compat.py` — LOAD.LOG, FSB1/FSB4 headers, duplicate IDs, EXCL
  overlap, CHAIN/SHARED_UI, loader format mismatch.
- `tests/test_dark.py` — median dark stack, hot-pixel map, correction, plan.
- `tests/test_autotest_core.py`, `test_autotest_cli.py` — harness regression.

Run all of them from `apps/fp-companion`:

```bash
python -m pytest -q
```

## Verified results (2026-11-03)

| Check | Result |
|---|---|
| `lexpo-hotload` HIL suite | PASS 10/10 |
| `fp-hotload stress --n 20` | PASS 20/20 |
| 50-cycle prototype run | PASS 50/50 |
| release → stock, cold boot re-applies | PASS |
| bad path `\fpSup\NOPE.BIN` | returns `-3`, sites unchanged |
| full offline suite | 385 passed |
| compat solver | 33 passed |
| dark-frame library | 28 passed |

The live run that produced these numbers is in
[reports/2026-11-03-lexpo-hotload.md](reports/2026-11-03-lexpo-hotload.md)
(JSON beside it).

Real long-exposure DNGs are still needed to validate the dark-frame library
against the fp's raw plane, black level, and amp glow. The compat solver reads
claims from `LOAD.LOG`; headers do not contain a claim table.
