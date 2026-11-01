from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from fp_companion.core.models import CardPlan, Release, ReleaseFile
from fp_companion.core.writer import MANIFEST_NAME, write_card

DIRECTORY = "fpsup-test-v1.0.0"
PAYLOADS = {
    "AutoRun.txt": b"fpSup test release\n",
    "fpSup.BIN": bytes(range(256)) * 4,
}
DOCS = {
    "MANIFEST.txt": "SIGMA fp - Test (v1.0.0)\n",
    "ABOUT.txt": "A synthetic release used by tests.\n",
    "README.txt": "Do not copy me to the card.\n",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_release(source_root: Path, *, corrupt: bool = False, firmware: str = "5.02") -> Release:
    release_dir = source_root / DIRECTORY
    release_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, data in PAYLOADS.items():
        content = data + b"-corrupted" if corrupt else data
        (release_dir / name).write_bytes(content)
        files.append(ReleaseFile(path=name, size=len(data), sha256=sha256_bytes(data)))
    for name, text in DOCS.items():
        (release_dir / name).write_text(text, encoding="utf-8")
    return Release(
        id="og2k",
        name="SIGMA fp - Test release",
        version="v1.0.0",
        directory=DIRECTORY,
        firmware=firmware,
        loader=4,
        min_svc=None,
        files=tuple(files),
        category="format",
        summary="Synthetic release.",
        risk="release",
    )


@pytest.fixture()
def card(tmp_path: Path) -> tuple[Release, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = tmp_path / "card"
    target.mkdir()
    return make_release(source_root), target, source_root


def test_happy_path_copies_verifies_and_writes_manifest(card) -> None:
    release, target, source_root = card
    report = write_card(CardPlan(release=release, target=target, source_root=source_root))

    assert report.ok
    assert report.files == tuple(PAYLOADS)
    assert report.verified == tuple(PAYLOADS)
    assert report.bytes_written == sum(len(data) for data in PAYLOADS.values())
    assert report.backup is None
    assert all(issue.severity == "warning" for issue in report.issues)

    for name, data in PAYLOADS.items():
        destination = target / name
        assert destination.read_bytes() == data
        assert sha256_bytes(destination.read_bytes()) == sha256_bytes(data)

    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["app"] == "fp Companion 0.1.0"
    assert manifest["release"]["id"] == "og2k"
    assert manifest["release"]["version"] == "v1.0.0"
    assert manifest["release"]["directory"] == DIRECTORY
    assert manifest["firmware"] == "5.02"
    assert manifest["loader"] == 4
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None
    assert manifest["files"] == [
        {"path": name, "size": len(data), "sha256": sha256_bytes(data)}
        for name, data in PAYLOADS.items()
    ]


def test_docs_are_not_copied(card) -> None:
    release, target, source_root = card
    report = write_card(CardPlan(release=release, target=target, source_root=source_root))

    assert report.ok
    for doc in ("MANIFEST.txt", "ABOUT.txt", "README.txt"):
        assert doc not in report.files
        assert not (target / doc).exists()


def test_dry_run_writes_nothing(card) -> None:
    release, target, source_root = card
    report = write_card(
        CardPlan(release=release, target=target, source_root=source_root, dry_run=True)
    )

    assert report.ok
    assert report.files == tuple(PAYLOADS)
    assert report.bytes_written == sum(len(data) for data in PAYLOADS.values())
    assert report.verified == ()
    assert report.backup is None
    assert list(target.iterdir()) == []


def test_backup_zip_contains_preexisting_files(card) -> None:
    release, target, source_root = card
    (target / "OLD.DAT").write_bytes(b"existing card content")
    (target / "DCIM").mkdir()
    (target / "DCIM" / "PHOTO.JPG").write_bytes(b"jpeg bytes")

    report = write_card(
        CardPlan(release=release, target=target, source_root=source_root), backup=True
    )

    assert report.ok
    assert report.backup is not None
    assert report.backup.parent == target.parent
    assert re.fullmatch(r"card-backup-\d{8}-\d{6}\.zip", report.backup.name)
    assert report.backup.is_file()

    with zipfile.ZipFile(report.backup) as archive:
        names = set(archive.namelist())
        assert {"OLD.DAT", "DCIM/PHOTO.JPG"} <= names
        # The backup was taken before the new payloads were written.
        assert "fpSup.BIN" not in names
        assert archive.read("OLD.DAT") == b"existing card content"


def test_corrupt_source_fails_without_verified_file(card) -> None:
    source_root = card[2]
    release = make_release(source_root, corrupt=True)
    target = card[1]

    report = write_card(CardPlan(release=release, target=target, source_root=source_root))

    assert not report.ok
    assert report.verified == ()
    assert any(issue.code == "hash" and issue.severity == "error" for issue in report.issues)
    for name in PAYLOADS:
        assert not (target / name).exists()
    assert not (target / MANIFEST_NAME).exists()
    assert not list(target.glob("*" + ".fpcompanion-tmp"))


def test_validation_error_writes_nothing(card) -> None:
    source_root = card[2]
    release = make_release(source_root, firmware="5.00")
    target = card[1]

    report = write_card(CardPlan(release=release, target=target, source_root=source_root))

    assert not report.ok
    assert any(issue.code == "firmware" for issue in report.issues)
    assert list(target.iterdir()) == []
