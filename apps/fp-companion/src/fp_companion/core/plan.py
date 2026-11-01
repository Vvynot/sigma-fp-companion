"""Plan construction and validation for Card Studio.

This module is the CPU-side, camera-free half of the write pipeline.  It builds
a :class:`~fp_companion.core.models.CardPlan` from a release and a target
directory, then validates that plan purely by inspecting filesystem metadata
(existence, directory type and free space).  ``writer.py`` performs the real
copy only after :func:`validate_plan` reports no ``error`` issues.

Severities are intentionally simple: ``error`` blocks a write, ``warning`` is
shown to the user but does not.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .models import CardPlan, Release, ValidationIssue, ValidationResult

REQUIRED_FIRMWARE = "5.02"
MIN_FREE_BYTES = 8 * 1024 * 1024
SSD_ONLY_RELEASE_IDS = frozenset({"og3k", "og4k", "res-custom"})


def _find_project_root() -> Path:
    """Locate the repository root so writes inside the checkout are refused.

    The app lives at ``<root>/apps/fp-companion``; ``vendor/fpSup`` must sit
    beside it.  If the layout is not recognised (e.g. an installed wheel), fall
    back to the known source depth so the check still has a sensible bound.
    """

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "apps" / "fp-companion").is_dir() and (parent / "vendor" / "fpSup").is_dir():
            return parent
    # <root>/apps/fp-companion/src/fp_companion/core/plan.py
    return here.parents[5] if len(here.parents) > 5 else here.parent


PROJECT_ROOT = _find_project_root()


def _system_drive_root() -> Path | None:
    """Return the system drive root (``C:\\`` on Windows) or ``None``.

    Only the *system* drive root is dangerous; SD cards are frequently mounted
    at another drive root such as ``E:\\`` or ``F:\\``, which is a perfectly
    normal target and must stay allowed.
    """

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


def _resolve(path: Path) -> Path | None:
    """Resolve defensively: broken links or odd mounts can raise ``OSError``."""

    try:
        return path.resolve()
    except OSError:
        return None


def _entry_key(path: str) -> str:
    """Normalise a release entry path for duplicate detection.

    ``normcase`` makes the comparison case-insensitive on Windows, where two
    entries differing only by case would overwrite each other on the card.
    """

    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return os.path.normcase(value)


def make_plan(release: Release, target: Path, source_root: Path, dry_run: bool = False) -> CardPlan:
    """Build a plan describing a release to be written to ``target``.

    Nothing is read or written here; validation happens separately so callers
    can display a plan before committing to it.
    """

    return CardPlan(
        release=release,
        target=Path(target),
        source_root=Path(source_root),
        dry_run=bool(dry_run),
    )


def _check_duplicates(release: Release, issues: list[ValidationIssue]) -> None:
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for entry in release.files:
        key = _entry_key(entry.path)
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, entry.path)
    for key, count in counts.items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    "error",
                    "duplicate",
                    f"Duplicate file path in release: {first_seen[key]} ({count} entries).",
                )
            )


def _check_release_files(release: Release, release_dir: Path, issues: list[ValidationIssue]) -> None:
    reported: set[str] = set()
    for entry in release.files:
        key = _entry_key(entry.path)
        if key in reported:
            continue
        reported.add(key)
        try:
            present = (release_dir / entry.path).is_file()
        except OSError:
            present = False
        if not present:
            issues.append(ValidationIssue("error", "missing", f"Missing source file: {entry.path}"))


def _check_target_location(
    release: Release,
    resolved_target: Path,
    source_root: Path | None,
    issues: list[ValidationIssue],
) -> None:
    # The project checkout is read-only source material, never a card.
    if resolved_target == PROJECT_ROOT or resolved_target.is_relative_to(PROJECT_ROOT):
        issues.append(
            ValidationIssue(
                "error",
                "project",
                f"Refusing to write inside the project tree: {resolved_target}",
            )
        )

    # Only the system drive root is refused; other drive roots (E:\, F:\) are
    # normal SD card mount points.
    system_root = _system_drive_root()
    if system_root is not None and resolved_target == system_root:
        issues.append(
            ValidationIssue(
                "error",
                "root",
                f"Refusing to write to the system drive root: {resolved_target}",
            )
        )

    if source_root is not None:
        resolved_source = _resolve(source_root)
        if resolved_source is not None and resolved_target.is_relative_to(resolved_source):
            issues.append(
                ValidationIssue("error", "source", "Target is inside the source tree.")
            )

    try:
        free = shutil.disk_usage(resolved_target).free
    except OSError as exc:
        issues.append(
            ValidationIssue(
                "error",
                "space",
                f"Could not determine free space at {resolved_target}: {exc}",
            )
        )
    else:
        required = release.total_bytes + MIN_FREE_BYTES
        if free < required:
            issues.append(
                ValidationIssue(
                    "error",
                    "space",
                    f"Not enough free space at {resolved_target}: "
                    f"{free} bytes available, {required} bytes required.",
                )
            )


def validate_plan(plan: CardPlan) -> ValidationResult:
    """Validate a card plan and return all issues found.

    ``error`` issues block a write; ``warning`` issues do not.
    """

    issues: list[ValidationIssue] = []
    release = plan.release

    if release.firmware != REQUIRED_FIRMWARE:
        issues.append(
            ValidationIssue(
                "error",
                "firmware",
                f"Only SIGMA fp firmware {REQUIRED_FIRMWARE} is supported "
                f"(release targets {release.firmware}).",
            )
        )

    try:
        target: Path | None = Path(plan.target)
    except TypeError:
        target = None
        issues.append(
            ValidationIssue("error", "target", f"Invalid target path: {plan.target!r}")
        )

    try:
        source_root: Path | None = Path(plan.source_root)
    except TypeError:
        source_root = None
        issues.append(
            ValidationIssue("error", "source", f"Invalid source root: {plan.source_root!r}")
        )

    if target is not None:
        resolved_target = _resolve(target)
        if resolved_target is None:
            issues.append(
                ValidationIssue("error", "target", f"Could not resolve target path: {target}")
            )
        elif not resolved_target.exists() or not resolved_target.is_dir():
            issues.append(ValidationIssue("error", "target", f"Target is not a directory: {target}"))
        else:
            _check_target_location(release, resolved_target, source_root, issues)

    _check_duplicates(release, issues)

    if source_root is not None:
        resolved_source = _resolve(source_root)
        base = resolved_source if resolved_source is not None else source_root
        _check_release_files(release, base / release.directory, issues)

    if release.risk != "release":
        issues.append(ValidationIssue("warning", "risk", f"Release risk level: {release.risk}."))

    if release.id in SSD_ONLY_RELEASE_IDS:
        issues.append(
            ValidationIssue(
                "warning",
                "ssd",
                "This mode needs an external USB-C SSD, not the SD card.",
            )
        )

    issues.append(
        ValidationIssue(
            "warning",
            "stock",
            "Before removing the card, restore a stock color mode and power off cleanly.",
        )
    )

    return ValidationResult(tuple(issues))
