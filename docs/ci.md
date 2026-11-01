# Continuous integration

The repository uses one GitHub Actions workflow,
`.github/workflows/tests.yml`, to test `apps/fp-companion` on Windows.

## When it runs

- Every push to `main` and to any `feat/**` branch.
- Every pull request targeting `main`.

## What runs

The `tests` workflow has a single job, `pytest (windows)`, on
`windows-latest` with a 30 minute timeout:

1. Check out the repository (`actions/checkout@v4`).
2. Create the `fp-companion` conda environment from `environment.yml` with
   `conda-incubator/setup-miniconda@v3` (conda-forge channel, Python 3.11,
   auto-activated). The environment file is the single source of truth for
   dependencies and for the Python version; the workflow does not repeat the
   pip list and does not pass `python-version`, which setup-miniconda ignores
   whenever `environment-file` is set.
3. Install the app itself in editable mode:
   `python -m pip install -e apps/fp-companion`.
4. Run the test suite from `apps/fp-companion`: `python -m pytest -q`.
5. Run the Qt smoke test from `apps/fp-companion`:
   `python ../../scripts/verify_qt.py`, which opens and closes `MainWindow`
   without a display.

`QT_QPA_PLATFORM=offscreen` is set for the whole job so no window server or
visible desktop is required.

## Shell and conda activation

Every `run` step uses an explicit Bash login shell:

```yaml
defaults:
  run:
    shell: bash -el {0}
```

On `windows-latest`, this is the Git Bash that ships with the runner. The
workflow does not rely on setup-miniconda's default PowerShell
auto-activation. With `auto-activate: true`, setup-miniconda appends its
activation snippet to the shell profile (`.bash_profile`/`.bashrc` in the Git
Bash home directory). The `-l` (login) flag makes Bash source that profile
before running the step, so the `fp-companion` environment is active and its
`python`, `pip`, and `pytest` resolve to the conda environment. `-e` makes
Bash exit on the first failed command, so a failing step still fails the job.

Because the shell is pinned at workflow level, the `working-directory` and
`QT_QPA_PLATFORM` settings on the pytest and Qt steps behave as before:
`working-directory: apps/fp-companion` selects the app folder and the
workflow-level `env` block supplies the offscreen platform plugin.

## Reproducing locally

Use the same steps on Windows. On the development machine conda lives at
`E:\Softwares\miniconda`; use its full path if `conda` is not on `PATH`.

PowerShell:

```powershell
conda env create -f environment.yml      # first time only
conda activate fp-companion
python -m pip install -e apps/fp-companion
cd apps/fp-companion
python -m pytest -q
$env:QT_QPA_PLATFORM = "offscreen"
python ../../scripts/verify_qt.py
```

If the environment already exists, refresh it instead of recreating it:

```powershell
conda env update -f environment.yml --prune
```

Or run individual commands without activating, using `conda run`:

```powershell
conda run -n fp-companion python -m pip install -e apps/fp-companion
cd apps/fp-companion
conda run -n fp-companion python -m pytest -q
```

## No camera or vendor tree needed

CI needs neither the camera nor the `vendor/` checkouts:

- No test opens a USB device or talks to the fp; hardware work is not
  exercised by the suite.
- `vendor/*` is gitignored except `vendor/README.md` and
  `vendor/VERSIONS.md`, so the vendor tree is absent in CI. Synthetic release
  fixtures built in `tmp_path` cover catalogue scanning. The one test that
  reads the real vendor releases,
  `test_real_vendor_releases_parse_without_exceptions`, is skipped
  automatically when `vendor/fpSup/fpSup/releases` is missing.
- The Qt smoke test builds `MainWindow`, which tolerates a missing releases
  folder, and runs with the offscreen platform plugin.

To supply real releases locally without the vendor checkout, point
`FPSUP_RELEASES` at a releases directory; CI does not set it.
