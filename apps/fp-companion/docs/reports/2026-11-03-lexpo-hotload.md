# lexpo-hotload

LExpo hot reload, ten reload cycles, release and reload (fp 5.02)

- Result: PASS
- Started: 2026-11-02T18:48:18+00:00
- Ended: 2026-11-02T18:48:20+00:00
- Steps: 10

| # | Kind | Result | Detail | Observation |
|---|------|--------|--------|-------------|
| 0 | shell | PASS | reply contained '5.02.0' | model       = w71c1 <br>xc version  = V91 <br>rel version = 5.02.0.V91  <br>git hash    = 13aa621060efff11aaba9b21ad925005302605cf <br> |
| 1 | hotload | PASS | hotload probe: observed success (loader_ok=True svc_version=6 patch=patched lv=disarmed); expected success | loader_ok=True svc_version=6 patch=patched lv=disarmed |
| 2 | hotload | PASS | hotload reload: observed success (return=0 patch=patched lv=armed); expected success | return=0 patch=patched lv=armed |
| 3 | mem_expect | PASS | addr 0xC007F324: expected 0xE309C6FC, observed 0xE309C6FC | 0xE309C6FC |
| 4 | hotload | PASS | hotload snapshot: observed success (loader_ok=True patch=patched lv=armed lv_done=0); expected success | loader_ok=True patch=patched lv=armed lv_done=0 |
| 5 | hotload | PASS | hotload cycle: observed success (10/10 iterations passed); expected success | 10/10 iterations passed |
| 6 | hotload | PASS | hotload release: observed success (return=0 patch=stock lv=disarmed); expected success | return=0 patch=stock lv=disarmed |
| 7 | mem_expect | PASS | addr 0xC007F324: expected 0xE309C6F4, observed 0xE309C6F4 | 0xE309C6F4 |
| 8 | hotload | PASS | hotload load: observed success (return=0 path_addr=0xC072FC90 patch=patched lv=armed); expected success | return=0 path_addr=0xC072FC90 patch=patched lv=armed |
| 9 | hotload | PASS | hotload probe: observed success (loader_ok=True svc_version=6 patch=patched lv=armed); expected success | loader_ok=True svc_version=6 patch=patched lv=armed |
