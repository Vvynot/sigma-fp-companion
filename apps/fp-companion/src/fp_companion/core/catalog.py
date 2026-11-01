from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .models import Catalog, Release, ReleaseFile

_RELEASE_DIR_RE = re.compile(r"^fpsup-(?P<stem>.+)-v(?P<version>.+)$")
_CAMERA_PREFIX_RE = re.compile(r"^SIGMA\s+fp\s*(?:—|--)\s*")
_TRAILING_VERSION_RE = re.compile(r"\s*\(v[^)]*\)(?:\s*,.*)?\s*$")
_FIRMWARE_RE = re.compile(r"Ver\.([0-9.]+)")
_LOADER_RE = re.compile(
    r"(?:Loader v([34])|LOADER\.BIN gives services v([34]))"
)
_MIN_SVC_RE = re.compile(r"min_svc=(\d+)")
_FILE_RE = re.compile(
    r"^\s*(\S+)\s+([\d,]+)\s*B\s+sha256=([0-9a-fA-F]{64})"
)

# Longest/most specific prefixes first so "gyro2"/"gyro-base" win over "gyro".
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("og2k", "format"),
    ("og3k", "format"),
    ("og4k", "format"),
    ("res-custom", "format"),
    ("open-gate", "format"),
    ("lossless", "codec"),
    ("gyro2", "gyro"),
    ("gyro-base", "gyro"),
    ("gyro", "gyro"),
    ("raw-view", "monitor"),
)
_DEFAULT_CATEGORY = "utility"
_DEFAULT_FIRMWARE = "5.02"
_DEFAULT_LOADER = 3
_NON_PAYLOAD_NAMES = {"about.txt", "readme.txt", "manifest.txt"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _prettify_id(release_id: str) -> str:
    return " ".join(part.capitalize() for part in release_id.split("-") if part)


def _categorise(release_id: str) -> str:
    for prefix, category in _CATEGORY_PREFIXES:
        if release_id.startswith(prefix):
            return category
    return _DEFAULT_CATEGORY


def _risk(version: str, directory: str) -> str:
    if "test" in version.lower() or "test" in directory.lower():
        return "test"
    if version and version[-1].isalpha():
        return "alpha"
    return "release"


def _parse_name(first_line: str) -> str:
    name = _CAMERA_PREFIX_RE.sub("", first_line.strip())
    name = _TRAILING_VERSION_RE.sub("", name).strip()
    return name


def _parse_release(directory: Path, manifest: Path) -> Release | None:
    match = _RELEASE_DIR_RE.match(directory.name)
    if match is None:
        return None

    release_id = match.group("stem")
    version = match.group("version")
    text = _read_text(manifest)
    lines = text.splitlines()

    first_line = lines[0] if lines else ""
    name = _parse_name(first_line)
    if not name:
        name = release_id

    firmware_match = _FIRMWARE_RE.search(text)
    firmware = firmware_match.group(1) if firmware_match else _DEFAULT_FIRMWARE

    loader_match = _LOADER_RE.search(text)
    if loader_match is not None:
        loader = int(loader_match.group(1) or loader_match.group(2))
    else:
        loader = _DEFAULT_LOADER

    min_svc_match = _MIN_SVC_RE.search(text)
    min_svc = int(min_svc_match.group(1)) if min_svc_match else None

    files: list[ReleaseFile] = []
    for line in lines:
        file_match = _FILE_RE.match(line)
        if file_match is None:
            continue
        files.append(
            ReleaseFile(
                path=file_match.group(1),
                size=int(file_match.group(2).replace(",", "")),
                sha256=file_match.group(3).lower(),
            )
        )
    if not files:
        return None

    about = directory / "ABOUT.txt"
    summary = ""
    if about.is_file():
        for line in _read_text(about).splitlines():
            if line.strip():
                summary = line.strip()
                break
    if not summary:
        summary = name

    return Release(
        id=release_id,
        name=name,
        version=version,
        directory=directory.name,
        firmware=firmware,
        loader=loader,
        min_svc=min_svc,
        files=tuple(files),
        category=_categorise(release_id),
        summary=summary,
        risk=_risk(version, directory.name),
    )


def _scan_release_without_manifest(directory: Path) -> Release | None:
    """Build a Release for a directory that ships no MANIFEST.txt.

    The card payload is hashed directly. Directories with no AutoRun.txt or
    no payload file are not releases and are skipped.
    """
    match = _RELEASE_DIR_RE.match(directory.name)
    if match is None:
        return None

    auto_run = directory / "AutoRun.txt"
    if not auto_run.is_file():
        return None

    payloads = [auto_run]
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if path == auto_run or not path.is_file():
            continue
        if path.name.lower() in _NON_PAYLOAD_NAMES:
            continue
        payloads.append(path)
    if len(payloads) < 2:
        # AutoRun.txt alone is not a payload.
        return None

    files: list[ReleaseFile] = []
    for path in payloads:
        size, sha256 = _hash_file(path)
        files.append(
            ReleaseFile(
                path=path.relative_to(directory).as_posix(),
                size=size,
                sha256=sha256,
            )
        )

    release_id = match.group("stem")
    version = match.group("version")

    notes = ""
    for name in ("README.txt", "ABOUT.txt"):
        candidate = directory / name
        if candidate.is_file():
            notes += _read_text(candidate) + "\n"

    firmware_match = _FIRMWARE_RE.search(notes)
    firmware = firmware_match.group(1) if firmware_match else _DEFAULT_FIRMWARE

    loader_match = _LOADER_RE.search(notes)
    if loader_match is not None:
        loader = int(loader_match.group(1) or loader_match.group(2))
    else:
        loader = _DEFAULT_LOADER

    name = _prettify_id(release_id)
    summary = ""
    for source in (directory / "ABOUT.txt", directory / "README.txt"):
        if not source.is_file():
            continue
        for line in _read_text(source).splitlines():
            if line.strip():
                summary = line.strip()
                break
        if summary:
            break
    if not summary:
        summary = name

    return Release(
        id=release_id,
        name=name,
        version=version,
        directory=directory.name,
        firmware=firmware,
        loader=loader,
        min_svc=None,
        files=tuple(files),
        category=_categorise(release_id),
        summary=summary,
        risk=_risk(version, directory.name),
    )


def scan_releases(root: Path) -> Catalog:
    root = Path(root)
    if not root.is_dir():
        return Catalog(())

    releases: list[Release] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        manifest = directory / "MANIFEST.txt"
        if manifest.is_file():
            release = _parse_release(directory, manifest)
        else:
            release = _scan_release_without_manifest(directory)
        if release is not None:
            releases.append(release)
    return Catalog(tuple(releases))


def _builtin_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "releases.json"


def load_builtin() -> Catalog:
    path = _builtin_path()
    if not path.is_file():
        return Catalog(())
    payload = json.loads(path.read_text(encoding="utf-8"))
    releases = tuple(
        Release(
            id=item["id"],
            name=item["name"],
            version=item["version"],
            directory=item["directory"],
            firmware=item["firmware"],
            loader=int(item["loader"]),
            min_svc=item.get("min_svc"),
            files=tuple(ReleaseFile(**entry) for entry in item["files"]),
            category=item["category"],
            summary=item["summary"],
            risk=item["risk"],
        )
        for item in payload
    )
    return Catalog(releases)


def save_catalog(catalog: Catalog, path: Path) -> None:
    payload = [
        {
            "id": release.id,
            "name": release.name,
            "version": release.version,
            "directory": release.directory,
            "firmware": release.firmware,
            "loader": release.loader,
            "min_svc": release.min_svc,
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in release.files
            ],
            "category": release.category,
            "summary": release.summary,
            "risk": release.risk,
        }
        for release in catalog.releases
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
