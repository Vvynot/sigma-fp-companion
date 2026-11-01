# Vendor sources

Read-only upstream references. Do not edit these checkouts; keep local work in
`apps/fp-companion/`.

| Directory | Upstream | Purpose | Notes |
|---|---|---|---|
| `fpSup/` | https://github.com/ijigen/fpSup | Card format, loader, USB shell, LUT SUP, CDNG Image, RAW-View | Sparse checkout; no firmware blobs |
| `sigma-ptpy/` | https://github.com/makanikai/sigma-ptpy | Python PTP library | MIT; stale since 2021 |
| `sigma-fp-bridge/` | https://github.com/ijigen/sigma-fp-bridge | PTP control, live view, recording, HTTP API | Research tool; protocol docs |
| `fp-l-tether/` | https://github.com/iochan-ship-it/fp-l-tether | fp L tether reference | macOS-first |
| `SigmaFpAlpaca/` | https://github.com/markjamesford-telescope/SigmaFpAlpaca | Windows .NET PTP/libusb reference | ASCOM Alpaca |
| `fp-lab/` | https://github.com/aguillerot/fp-lab | QR recipe decode/edit | Web app; MIT |
| `indi-sigma/` | https://github.com/ValNyz/indi-sigma | Linux INDI astro driver | Astro reference |
| `sigma-fp-raw/` | https://github.com/julienacquaviva/sigma-fp-raw | Resolve OpenFX and gyro/color reference | MIT |
| `Jhih-FilmLab/` | https://github.com/jhihhe/Jhih-FilmLab | 82 in-camera film recipes | Unverified; license caveat |
| `fp-codec-lab/` | https://github.com/charlatanshost/fp-codec-lab | Codec research | Pre-alpha |

## License caution

Each checkout keeps its own license. fpSup's license terms are unverified; some
community modules have no license. Do not redistribute firmware, decompiled
output, or bundled LUT/recipe assets without permission.
