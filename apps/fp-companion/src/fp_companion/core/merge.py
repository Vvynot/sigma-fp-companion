"""Compatibility planning for combining several fpSup modules on one card.

This module is a *planner*, not a card writer.  It inspects an already-scanned
selection of :class:`~fp_companion.core.models.Release` objects and reports
whether the selection is coherent: same firmware target, same loader, distinct
modules, and no conflicting payload paths.  It never reads or writes a card and
it never modifies a release.

Assumptions
-----------
* One card can carry at most :data:`MAX_MODULES` distinct modules.  The fpSup
  loader was built around a small module set; eight is a conservative working
  limit and has **not** been verified on a camera.  Adjust ``MAX_MODULES`` when
  the real limit is known.
* Duplicate payload paths are only a warning.  Modules legitimately share
  ``AutoRun.txt``, ``fpSup/LOADER.BIN`` and ``FPSUPUI/0.BIN``; the eventual merge
  tool must reconcile their contents (for example by regenerating one shared
  ``AutoRun.txt``) rather than blindly copying each release in turn.
* Producing a merged ``AutoRun.txt``/loader image is out of scope.  Every plan
  therefore carries a note that on-camera validation is still required.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Release, ValidationIssue

#: Conservative maximum number of distinct modules assumed for one card.
#: Not yet verified against the camera; see the module docstring.
MAX_MODULES = 8

SUPPORTED_LOADERS = frozenset({3, 4})

#: Modes that record to an external USB-C SSD rather than the SD card.
SSD_ONLY_RELEASE_IDS = frozenset({"og3k", "og4k", "res-custom"})

MERGE_NOTE = (
    "Actual AutoRun.txt and loader merging is not implemented; the combined "
    "card still needs on-camera validation."
)

_ISSUE_ERROR = "error"
_ISSUE_WARNING = "warning"


@dataclass(frozen=True)
class MergePlan:
    """Result of :func:`plan_merge`.

    ``ok`` is true only when no issue has severity ``error``.  ``loader`` is the
    common loader version when the selection has exactly one supported loader,
    otherwise ``None``.  ``file_count`` and ``total_bytes`` always describe the
    selected releases as given, even when the plan is blocked.
    """

    releases: tuple[Release, ...]
    ok: bool
    issues: tuple[ValidationIssue, ...]
    loader: int | None
    file_count: int
    total_bytes: int
    notes: tuple[str, ...] = ()


def _ordered_unique(values: Sequence[object]) -> list[object]:
    """Return values in first-seen order, dropping repeats."""

    result: list[object] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _normalise_path(path: str) -> str | None:
    """Collapse ``path`` to a canonical card-relative POSIX path.

    Backslashes are treated as separators and :class:`PurePosixPath` removes
    redundant ``.``/empty segments and trailing slashes.  Parent traversal
    (``..``) is rejected: ``None`` is returned as a sentinel that can never
    compare equal to a real path, and :func:`_check_unsafe_paths` reports the
    offending release.
    """

    value = str(path).replace("\\", "/")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        return None
    if not parts:
        return "."
    return str(PurePosixPath(*parts))


def _path_key(path: str) -> str | None:
    """Normalise a release-relative path for case-insensitive comparison.

    Payload paths live on the camera card's case-insensitive filesystem, so
    keys are casefolded even when the planner runs on a case-sensitive host.
    ``None`` is returned for paths that :func:`_normalise_path` rejects.
    """

    normalised = _normalise_path(path)
    if normalised is None:
        return None
    return normalised.casefold()


def _error(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(_ISSUE_ERROR, code, message)


def _warning(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(_ISSUE_WARNING, code, message)


def _resolve_loader(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> int | None:
    loaders = _ordered_unique([release.loader for release in selected])
    if not loaders:
        return None

    if len(loaders) > 1:
        rendered = ", ".join(f"v{loader}" for loader in loaders)
        issues.append(
            _error(
                "mixed-loader",
                f"Mixed loader versions selected: {rendered}. "
                "All modules must use the same loader version.",
            )
        )
        return None

    loader = loaders[0]
    if loader not in SUPPORTED_LOADERS:
        supported = ", ".join(f"v{value}" for value in sorted(SUPPORTED_LOADERS))
        issues.append(
            _error(
                "unsupported-loader",
                f"Loader v{loader} is not supported; merge only supports {supported}.",
            )
        )
        return None
    return loader


def _check_firmware(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    firmwares = _ordered_unique([release.firmware for release in selected])
    if len(firmwares) <= 1:
        return

    details = ", ".join(
        f"{firmware} ({', '.join(r.directory for r in selected if r.firmware == firmware)})"
        for firmware in firmwares
    )
    issues.append(
        _error(
            "mixed-firmware",
            f"Mixed firmware targets in selection: {details}. "
            "All modules must target the same firmware.",
        )
    )


def _check_module_count(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    if len(selected) > MAX_MODULES:
        issues.append(
            _error(
                "too-many-modules",
                f"{len(selected)} modules selected but a merged card supports at "
                f"most {MAX_MODULES} (loader limit assumed, not yet verified on camera).",
            )
        )


def _check_unsafe_paths(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    """Reject release paths that try to escape the card root with ``..``."""

    for release in selected:
        candidates = [("release directory", release.directory)]
        candidates.extend(("payload path", entry.path) for entry in release.files)
        for label, value in candidates:
            if _normalise_path(value) is not None:
                continue
            issues.append(
                _error(
                    "unsafe-path",
                    f"Release {release.directory} has a {label} containing '..': "
                    f"{value}. Parent-directory segments are not allowed in a "
                    "merged payload.",
                )
            )


def _check_duplicate_directories(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    groups: dict[str, list[Release]] = {}
    for release in selected:
        key = _path_key(release.directory)
        if key is None:
            # Reported separately as an unsafe-path error.
            continue
        groups.setdefault(key, []).append(release)

    for group in groups.values():
        if len(group) <= 1:
            continue
        ids = ", ".join(_ordered_unique([release.id for release in group]))
        issues.append(
            _error(
                "duplicate-directory",
                f"Release directory selected more than once: {group[0].directory} "
                f"(modules: {ids}).",
            )
        )


def _check_duplicate_ids(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    groups: dict[str, list[Release]] = {}
    for release in selected:
        groups.setdefault(release.id, []).append(release)

    for release_id, group in groups.items():
        if len(group) <= 1:
            continue
        # Selecting the exact same directory twice is already reported as a
        # duplicate directory; only flag genuinely distinct versions here.
        keys = {
            key
            for release in group
            if (key := _path_key(release.directory)) is not None
        }
        if len(keys) <= 1:
            continue

        versions = ", ".join(
            f"{release.directory} (v{release.version})" for release in group
        )
        issues.append(
            _error(
                "duplicate-id",
                f"Module '{release_id}' is selected in several versions: "
                f"{versions}. Pick one version.",
            )
        )


def _check_duplicate_paths(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    # key -> first display path and the directories that ship it
    paths: dict[str, tuple[str, list[str]]] = {}
    for release in selected:
        seen_in_release: set[str] = set()
        for entry in release.files:
            key = _path_key(entry.path)
            if key is None:
                # Reported separately as an unsafe-path error; the sentinel
                # must never match another path.
                continue
            if key in seen_in_release:
                # A duplicate inside one release is plan.py's concern.
                continue
            seen_in_release.add(key)
            if key not in paths:
                paths[key] = (entry.path, [release.directory])
            elif release.directory not in paths[key][1]:
                paths[key][1].append(release.directory)

    for display_path, directories in paths.values():
        if len(directories) <= 1:
            continue
        issues.append(
            _warning(
                "duplicate-path",
                f"Payload path is provided by multiple modules: {display_path} "
                f"({', '.join(directories)}). The final merge tool must reconcile "
                "these files.",
            )
        )


def _check_risks(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    for release in selected:
        if release.risk != "release":
            issues.append(
                _warning(
                    "risk",
                    f"Release {release.directory} has risk level '{release.risk}'.",
                )
            )


def _check_ssd(
    selected: tuple[Release, ...], issues: list[ValidationIssue]
) -> None:
    for release in selected:
        if release.id.casefold() in SSD_ONLY_RELEASE_IDS:
            issues.append(
                _warning(
                    "ssd",
                    f"Module {release.directory} needs an external USB-C SSD, "
                    "not the SD card.",
                )
            )


def plan_merge(releases: Sequence[Release]) -> MergePlan:
    """Plan a compatibility merge for ``releases`` without touching a card.

    Errors block a merge; warnings are advisory.  The returned plan preserves
    the caller's module order and always includes :data:`MERGE_NOTE`.
    """

    selected = tuple(releases)
    issues: list[ValidationIssue] = []

    file_count = sum(len(release.files) for release in selected)
    total_bytes = sum(release.total_bytes for release in selected)

    if not selected:
        issues.append(
            _error("empty", "No modules selected; pick at least one release to merge.")
        )
        loader: int | None = None
    else:
        _check_module_count(selected, issues)
        _check_firmware(selected, issues)
        loader = _resolve_loader(selected, issues)
        _check_unsafe_paths(selected, issues)
        _check_duplicate_directories(selected, issues)
        _check_duplicate_ids(selected, issues)
        _check_duplicate_paths(selected, issues)
        _check_risks(selected, issues)
        _check_ssd(selected, issues)

    ok = not any(issue.severity == _ISSUE_ERROR for issue in issues)
    return MergePlan(
        releases=selected,
        ok=ok,
        issues=tuple(issues),
        loader=loader,
        file_count=file_count,
        total_bytes=total_bytes,
        notes=(MERGE_NOTE,),
    )


def describe_plan(plan: MergePlan) -> str:
    """Render a human-readable summary of ``plan`` for the UI."""

    lines = [f"Merge plan: {'OK' if plan.ok else 'BLOCKED'}"]

    if plan.releases:
        labels = ", ".join(
            f"{release.name} {release.version}" if release.version else release.name
            for release in plan.releases
        )
        lines.append(f"Modules ({len(plan.releases)}): {labels}")
    else:
        lines.append("Modules (0): none")

    lines.append(
        f"Loader: {'v' + str(plan.loader) if plan.loader is not None else 'unknown'}"
    )
    lines.append(f"Payload: {plan.file_count} files, {plan.total_bytes:,} bytes")

    if plan.issues:
        lines.append("Issues:")
        for issue in plan.issues:
            lines.append(f"  {issue.severity.upper()} [{issue.code}] {issue.message}")
    else:
        lines.append("Issues: none")

    if plan.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in plan.notes)

    return "\n".join(lines)
