# Card Studio MVP contract

Scope: prepare a single-module fpSup card from `vendor/fpSup/fpSup/releases/`.
Multi-module merge, LUT packing, and recipe packing are out of scope for this
milestone.

## Data

Each release directory contains `MANIFEST.txt`, `ABOUT.txt`, `README.txt`,
`AutoRun.txt`, and payload files. `MANIFEST.txt` lists each payload file with
size and SHA-256.

Shared dataclasses live in `src/fp_companion/core/models.py`:
`ReleaseFile`, `Release`, `Catalog`, `CardPlan`, `ValidationIssue`,
`ValidationResult`, `WriteReport`.

## APIs

### catalog.py

```python
def scan_releases(root: Path) -> Catalog: ...
def load_builtin() -> Catalog: ...
def save_catalog(catalog: Catalog, path: Path) -> None: ...
```

`scan_releases` parses every subdirectory with a `MANIFEST.txt`.

Fields:
- `id`: short id derived from the directory (`fpsup-og3k-v0.2.8a` -> `og3k`).
- `name`: first line of `MANIFEST.txt` after the camera prefix.
- `version`: version part of the directory name.
- `directory`: release directory name.
- `firmware`: `5.02` (parse `SIGMA fp Ver.5.02 only`).
- `loader`: 3 or 4 from `Loader v3` / `Loader v4` or `LOADER.BIN gives services v3`.
- `min_svc`: integer from `min_svc=N` in the `*BIN header` line, else `None`.
- `files`: payload entries from `MANIFEST.txt` (path, size, sha256).
- `category`: one of `format`, `codec`, `gyro`, `monitor`, `utility`.
- `summary`: first non-empty line of `ABOUT.txt`.
- `risk`: `test` if directory/version contains `test`, else `alpha` if contains
  `a` suffix, else `release`.

`load_builtin` reads `src/fp_companion/data/releases.json`.

### plan.py

```python
def make_plan(release: Release, target: Path, source_root: Path, dry_run: bool = False) -> CardPlan: ...
def validate_plan(plan: CardPlan) -> ValidationResult: ...
```

Rules:
- firmware must be `5.02`;
- target must exist and be a directory;
- refuse `C:\` and the project root;
- refuse if target is inside `source_root`;
- free space must be at least `release.total_bytes + 8 MiB`;
- every `ReleaseFile` must exist under `source_root/release.directory`;
- warning: `risk != "release"`;
- warning: OG3K/OG4K/Res-Custom requires external SSD, not the SD card;
- warning: restore a stock color mode before removing the card.

### writer.py

```python
def write_card(plan: CardPlan, *, backup: bool = True) -> WriteReport: ...
```

- Validate first; return a failed report on validation errors.
- Dry run: compute files/bytes, do not touch the card.
- Backup existing target contents to a timestamped zip next to the target.
- Copy only `release.files` (do not copy `MANIFEST.txt`/`ABOUT.txt`/`README.txt`).
- Verify each copied file's SHA-256; a mismatch is an error.
- Write `fpsup-manifest.json` with release id/version, firmware, loader, hashes,
  timestamp, and app version.
- Never delete the source or the backup.

### ui/card_studio.py

`CardStudioWidget`:
- release list with name/version/risk;
- details pane (summary, firmware, loader, size, files, warnings);
- target directory picker;
- Dry run and Write buttons;
- report pane.

`app.py` mounts it in the Card Studio tab.

## Tests

- Each module gets its own test file.
- Use temp directories and synthetic release trees; the real vendor tree is
  read-only.
- Run:
  `/e/Softwares/miniconda/Scripts/conda.exe run --no-capture-output -n fp-companion pytest -q tests/test_<name>.py`

## Ownership

Do not edit files outside your assigned scope. `app.py` is owned by the UI
agent. `models.py` and this spec are owned by the coordinator.
