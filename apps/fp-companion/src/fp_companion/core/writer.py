from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .models import CardPlan, ValidationIssue, WriteReport
from .plan import validate_plan

APP_NAME = "fp Companion"
APP_VERSION = "0.1.0"
MANIFEST_NAME = "fpsup-manifest.json"
TMP_SUFFIX = ".fpcompanion-tmp"
DOC_NAMES = frozenset({"MANIFEST.txt", "ABOUT.txt", "README.txt"})
CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: str) -> Path | None:
    """Return a relative path that cannot escape the target directory."""
    candidate = Path(path)
    if candidate.is_absolute() or candidate.drive:
        return None
    if not candidate.parts or any(part == ".." for part in candidate.parts):
        return None
    return candidate


def _copy_to_tmp(source: Path, tmp: Path) -> tuple[int, str]:
    """Copy source to tmp while hashing, returning (bytes, sha256)."""
    digest = hashlib.sha256()
    written = 0
    with source.open("rb") as src, tmp.open("wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
            dst.write(chunk)
            digest.update(chunk)
            written += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return written, digest.hexdigest()


def _existing_files(target: Path) -> list[Path]:
    if not target.is_dir():
        return []
    return sorted(path for path in target.rglob("*") if path.is_file())


def _make_backup(target: Path, existing: list[Path]) -> Path:
    label = target.name or "card"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target.parent / f"{label}-backup-{stamp}.zip"
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in existing:
            archive.write(path, arcname=path.relative_to(target).as_posix())
    return backup_path


def _manifest_payload(plan: CardPlan) -> dict:
    release = plan.release
    return {
        "app": f"{APP_NAME} {APP_VERSION}",
        "release": {
            "id": release.id,
            "version": release.version,
            "directory": release.directory,
        },
        "firmware": release.firmware,
        "loader": release.loader,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in release.files
        ],
    }


def _write_manifest(target: Path, payload: dict) -> None:
    manifest = target / MANIFEST_NAME
    tmp = manifest.with_name(manifest.name + TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, manifest)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_card(plan: CardPlan, *, backup: bool = True) -> WriteReport:
    validation = validate_plan(plan)
    issues = list(validation.issues)

    if not validation.ok:
        return WriteReport(ok=False, target=plan.target, issues=tuple(issues))

    release = plan.release
    target = plan.target
    source_dir = plan.source_root / release.directory
    planned = tuple(item.path for item in release.files)

    # Refuse release entries that would write outside the target directory.
    relatives: list[tuple[str, Path, str, int]] = []
    for item in release.files:
        relative = _safe_relative(item.path)
        if relative is None:
            issues.append(
                ValidationIssue("error", "path", f"Unsafe release file path: {item.path}")
            )
            continue
        relatives.append((item.path, relative, item.sha256, item.size))
    if any(issue.severity == "error" for issue in issues):
        return WriteReport(ok=False, target=target, issues=tuple(issues))

    if plan.dry_run:
        return WriteReport(
            ok=True,
            target=target,
            files=planned,
            bytes_written=release.total_bytes,
            issues=tuple(issues),
        )

    existing = _existing_files(target)
    backup_path: Path | None = None
    if backup and existing:
        try:
            backup_path = _make_backup(target, existing)
        except OSError as exc:
            issues.append(ValidationIssue("error", "backup", f"Backup failed: {exc}"))
            return WriteReport(ok=False, target=target, issues=tuple(issues))

    files_written: list[str] = []
    verified: list[str] = []
    bytes_written = 0

    for path, relative, expected_sha, _expected_size in relatives:
        source = source_dir / relative
        destination = target / relative
        tmp = destination.with_name(destination.name + TMP_SUFFIX)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied, copied_sha = _copy_to_tmp(source, tmp)

            if copied_sha != expected_sha:
                tmp.unlink(missing_ok=True)
                issues.append(
                    ValidationIssue(
                        "error",
                        "hash",
                        f"Source hash mismatch for {path}: expected {expected_sha}, got {copied_sha}.",
                    )
                )
                return WriteReport(
                    ok=False,
                    target=target,
                    files=tuple(files_written),
                    bytes_written=bytes_written,
                    verified=tuple(verified),
                    backup=backup_path,
                    issues=tuple(issues),
                )

            os.replace(tmp, destination)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            issues.append(ValidationIssue("error", "write", f"Could not write {path}: {exc}"))
            return WriteReport(
                ok=False,
                target=target,
                files=tuple(files_written),
                bytes_written=bytes_written,
                verified=tuple(verified),
                backup=backup_path,
                issues=tuple(issues),
            )

        files_written.append(path)
        bytes_written += copied

        final_sha = _sha256_file(destination)
        if final_sha != expected_sha:
            issues.append(
                ValidationIssue(
                    "error",
                    "verify",
                    f"Verification failed for {path}: expected {expected_sha}, got {final_sha}.",
                )
            )
            return WriteReport(
                ok=False,
                target=target,
                files=tuple(files_written),
                bytes_written=bytes_written,
                verified=tuple(verified),
                backup=backup_path,
                issues=tuple(issues),
            )
        verified.append(path)

    try:
        _write_manifest(target, _manifest_payload(plan))
    except OSError as exc:
        issues.append(ValidationIssue("error", "manifest", f"Could not write manifest: {exc}"))
        return WriteReport(
            ok=False,
            target=target,
            files=tuple(files_written),
            bytes_written=bytes_written,
            verified=tuple(verified),
            backup=backup_path,
            issues=tuple(issues),
        )

    return WriteReport(
        ok=True,
        target=target,
        files=tuple(files_written),
        bytes_written=bytes_written,
        verified=tuple(verified),
        backup=backup_path,
        issues=tuple(issues),
    )
