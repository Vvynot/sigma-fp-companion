"""Write a non-bootable LUT staging directory for the SIGMA fp companion app.

This module is deliberately limited to **staging files on a normal filesystem
directory**.  It is not a bootable fpSup LUT SUP card builder: it writes no
firmware, loader, ``AutoRun.txt``, ``LUT.BIN`` or any other camera-bootable
artifact, and it never touches a camera or a real SD card.  The layout is

    <target>/LUT/source.cube     original .cube bytes, unchanged
    <target>/LUT/settings.json   metadata + input settings + SHA-256

A later, separately-reviewed step would pack these staging files into an
fpSup LUT SUP card.  Until then ``settings.json`` records
``"bootable": false`` so the directory cannot be mistaken for a card image.

Safety rules, mirroring ``plan.py``:

* the target must already exist as a directory;
* the target must not be inside the project checkout;
* the target must not be the system drive root (``C:\\`` on Windows).  Other
  drive roots such as ``E:\\`` are normal SD-card mount points and stay
  allowed;
* the copied ``source.cube`` is re-hashed and must match the source exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .lut import CubeLUT, LutSettings, parse_cube, validate_settings
from .models import ValidationIssue
from .plan import PROJECT_ROOT

APP_NAME = "fp Companion"
APP_VERSION = "0.1.0"
LUT_DIR_NAME = "LUT"
SOURCE_NAME = "source.cube"
SETTINGS_NAME = "settings.json"
TMP_SUFFIX = ".fpcompanion-tmp"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class LutWriteReport:
    ok: bool
    target: Path
    cube_sha256: str = ""
    files: tuple[str, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    # This module only ever produces the explicit staging layout; it cannot
    # produce a bootable fpSup LUT SUP card.
    layout: str = "staging"
    bootable: bool = False


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _system_drive_root() -> Path | None:
    """Return the system drive root (``C:\\`` on Windows) or ``None``."""

    if os.name != "nt":
        return None
    drive = os.environ.get("SystemDrive") or Path.home().drive or "C:"
    drive = drive.rstrip("\\/")
    if not drive:
        return None
    try:
        return Path(f"{drive}\\")
    except (OSError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_unchanged(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` byte-for-byte via a temporary file."""

    tmp = destination.with_name(destination.name + TMP_SUFFIX)
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + TMP_SUFFIX)
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _settings_payload(lut: CubeLUT, settings: LutSettings, sha256: str) -> dict:
    return {
        "app": f"{APP_NAME} {APP_VERSION}",
        "layout": "staging",
        "bootable": False,
        "note": (
            "Staging layout only: not a bootable fpSup LUT SUP card. "
            "No firmware, loader or boot files are written."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": lut.title,
        "size_1d": lut.size_1d,
        "size_3d": lut.size_3d,
        "grid": {"1d": lut.size_1d, "3d": lut.size_3d},
        "domain_min": list(lut.domain_min),
        "domain_max": list(lut.domain_max),
        "settings": {
            "input_color_space": settings.input_color_space,
            "input_gamma": settings.input_gamma,
            "intensity": settings.intensity,
        },
        "sha256": sha256,
    }


def _failed(
    target: Path,
    issues: list[ValidationIssue],
    *,
    cube_sha256: str = "",
    files: tuple[str, ...] = (),
) -> LutWriteReport:
    return LutWriteReport(
        ok=False,
        target=target,
        cube_sha256=cube_sha256,
        files=files,
        issues=tuple(issues),
    )


def write_lut_staging(
    target: Path, cube_path: Path, settings: LutSettings
) -> LutWriteReport:
    """Stage ``cube_path`` and ``settings`` under ``target/LUT``.

    This is a staging layout, not a bootable fpSup LUT SUP card; see the module
    docstring.  The target directory must already exist.  The source ``.cube``
    is copied unchanged, then re-read and verified against its SHA-256.  A
    mismatch fails the report and removes the copied file.

    The cube is parsed for its title/grid/domain metadata; full LUT validation
    is available separately through :func:`fp_companion.core.lut.validate_cube`
    so that a caller can show problems before staging.
    """

    target = Path(target)
    cube_path = Path(cube_path)
    issues: list[ValidationIssue] = []

    resolved_target = _resolve(target)
    if (
        resolved_target is None
        or not resolved_target.exists()
        or not resolved_target.is_dir()
    ):
        issues.append(
            ValidationIssue(
                "error",
                "target",
                f"Target is not an existing directory: {target}",
            )
        )
        return _failed(target, issues)

    # The project checkout is source material, never a card or staging area.
    if resolved_target == PROJECT_ROOT or resolved_target.is_relative_to(PROJECT_ROOT):
        issues.append(
            ValidationIssue(
                "error",
                "project",
                f"Refusing to write inside the project tree: {resolved_target}",
            )
        )

    system_root = _system_drive_root()
    if system_root is not None and resolved_target == system_root:
        issues.append(
            ValidationIssue(
                "error",
                "root",
                f"Refusing to write to the system drive root: {resolved_target}",
            )
        )

    if issues:
        return _failed(target, issues)

    settings_issues = validate_settings(settings)
    issues.extend(settings_issues)
    if any(issue.severity == "error" for issue in issues):
        return _failed(target, issues)

    if not cube_path.is_file():
        issues.append(
            ValidationIssue("error", "cube", f"LUT file not found: {cube_path}")
        )
        return _failed(target, issues)

    try:
        lut = parse_cube(cube_path)
    except (OSError, ValueError) as exc:
        issues.append(
            ValidationIssue("error", "cube", f"Could not read .cube file: {exc}")
        )
        return _failed(target, issues)

    try:
        source_sha256 = _sha256_file(cube_path)
    except OSError as exc:
        issues.append(
            ValidationIssue(
                "error", "cube", f"Could not hash .cube file: {exc}"
            )
        )
        return _failed(target, issues)

    lut_dir = resolved_target / LUT_DIR_NAME
    cube_destination = lut_dir / SOURCE_NAME
    settings_destination = lut_dir / SETTINGS_NAME

    try:
        lut_dir.mkdir(parents=True, exist_ok=True)
        _copy_unchanged(cube_path, cube_destination)
    except OSError as exc:
        issues.append(
            ValidationIssue("error", "write", f"Could not write source.cube: {exc}")
        )
        return _failed(target, issues, cube_sha256=source_sha256)

    try:
        copied_sha256 = _sha256_file(cube_destination)
    except OSError as exc:
        cube_destination.unlink(missing_ok=True)
        issues.append(
            ValidationIssue(
                "error", "verify", f"Could not verify copied source.cube: {exc}"
            )
        )
        return _failed(target, issues, cube_sha256=source_sha256)

    if copied_sha256 != source_sha256:
        cube_destination.unlink(missing_ok=True)
        issues.append(
            ValidationIssue(
                "error",
                "hash",
                f"Copied source.cube does not match the original: "
                f"expected {source_sha256}, got {copied_sha256}.",
            )
        )
        return _failed(target, issues, cube_sha256=source_sha256)

    try:
        _write_json(
            settings_destination,
            _settings_payload(lut, settings, source_sha256),
        )
    except OSError as exc:
        issues.append(
            ValidationIssue(
                "error", "settings", f"Could not write settings.json: {exc}"
            )
        )
        return _failed(
            target,
            issues,
            cube_sha256=source_sha256,
            files=(f"{LUT_DIR_NAME}/{SOURCE_NAME}",),
        )

    return LutWriteReport(
        ok=True,
        target=target,
        cube_sha256=source_sha256,
        files=(
            f"{LUT_DIR_NAME}/{SOURCE_NAME}",
            f"{LUT_DIR_NAME}/{SETTINGS_NAME}",
        ),
        issues=tuple(issues),
    )
