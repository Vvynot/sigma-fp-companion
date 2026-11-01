from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleaseFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    id: str
    name: str
    version: str
    directory: str
    firmware: str
    loader: int
    min_svc: int | None
    files: tuple[ReleaseFile, ...]
    category: str
    summary: str
    risk: str

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


@dataclass(frozen=True)
class Catalog:
    releases: tuple[Release, ...] = ()

    def by_id(self, release_id: str) -> Release:
        for release in self.releases:
            if release.id == release_id:
                return release
        raise KeyError(release_id)


@dataclass(frozen=True)
class CardPlan:
    release: Release
    target: Path
    source_root: Path
    dry_run: bool = False


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class WriteReport:
    ok: bool
    target: Path
    files: tuple[str, ...] = ()
    bytes_written: int = 0
    verified: tuple[str, ...] = ()
    backup: Path | None = None
    issues: tuple[ValidationIssue, ...] = ()
