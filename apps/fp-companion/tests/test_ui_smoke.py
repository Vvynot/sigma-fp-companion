from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from fp_companion.core.models import Catalog, Release, ReleaseFile
from fp_companion.ui.card_studio import CardStudioWidget

DIRECTORY = "fpsup-og3k-v0.2.8a"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def make_release(
    version: str = "0.2.8a",
    directory: str = DIRECTORY,
    summary: str = "Open gate 3K format module.",
) -> Release:
    return Release(
        id="og3k",
        name="OG 3K",
        version=version,
        directory=directory,
        firmware="5.02",
        loader=4,
        min_svc=3,
        files=(
            ReleaseFile(path="OG3K.BIN", size=4, sha256="0" * 64),
            ReleaseFile(path="MODULE.TXT", size=2, sha256="1" * 64),
        ),
        category="format",
        summary=summary,
        risk="alpha",
    )


def make_catalog() -> Catalog:
    return Catalog((make_release(),))


def make_source_root(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    payload = root / DIRECTORY
    payload.mkdir(parents=True)
    (payload / "OG3K.BIN").write_bytes(b"abcd")
    (payload / "MODULE.TXT").write_bytes(b"ef")
    return root


def test_details_and_dry_run(qapp: QApplication, tmp_path: Path) -> None:
    source_root = make_source_root(tmp_path)
    target = tmp_path / "card"
    target.mkdir()

    widget = CardStudioWidget(catalog=make_catalog(), source_root=source_root)
    try:
        assert widget.release_list.count() == 1
        assert widget.release_list.item(0).text() == "OG 3K — v0.2.8a (alpha)"
        assert not widget.dry_run_button.isEnabled()
        assert not widget.write_button.isEnabled()

        widget.release_list.setCurrentRow(0)

        details = widget.details.text()
        assert "OG 3K" in details
        assert "Open gate 3K format module." in details
        assert "Firmware: 5.02" in details
        assert "Loader: v4" in details
        assert "Min service: 3" in details
        assert "Category: format" in details
        assert "Files: 2" in details
        assert "Total size: 6 B (6 bytes)" in details
        assert "Risk: alpha" in details

        warnings = widget.warnings.text()
        assert "alpha" in warnings
        assert "external USB-C SSD" in warnings
        assert "stock color mode" in warnings

        assert widget.dry_run_button.isEnabled()
        assert widget.write_button.isEnabled()

        widget.target_edit.setText(str(target))
        widget.dry_run_button.click()

        report = widget.report.toPlainText()
        assert report.strip()
        assert "Dry run" in report
        assert "Status: OK" in report
        assert "No files were written." in report
        assert list(target.iterdir()) == []
    finally:
        widget.deleteLater()


def test_duplicate_ids_resolve_by_directory(qapp: QApplication, tmp_path: Path) -> None:
    older = make_release(
        version="0.2.1a",
        directory="fpsup-og3k-v0.2.1a",
        summary="Older OG 3K module.",
    )
    newer = make_release()
    source_root = tmp_path / "releases"
    for release in (older, newer):
        payload = source_root / release.directory
        payload.mkdir(parents=True)
        (payload / "OG3K.BIN").write_bytes(b"abcd")
        (payload / "MODULE.TXT").write_bytes(b"ef")
    target = tmp_path / "card"
    target.mkdir()

    widget = CardStudioWidget(catalog=Catalog((newer, older)), source_root=source_root)
    try:
        assert widget.release_list.count() == 2

        # Ascending version sort puts the older release first.
        widget.release_list.setCurrentRow(0)
        details = widget.details.text()
        assert "Older OG 3K module." in details
        assert "Version: 0.2.1a" in details
        assert "Version: 0.2.8a" not in details

        widget.target_edit.setText(str(target))
        widget.dry_run_button.click()
        report = widget.report.toPlainText()
        assert "Dry run — OG 3K v0.2.1a" in report
        assert "v0.2.8a" not in report
        assert list(target.iterdir()) == []

        widget.release_list.setCurrentRow(1)
        assert "Version: 0.2.8a" in widget.details.text()
    finally:
        widget.deleteLater()


def test_missing_releases_folder(qapp: QApplication, tmp_path: Path) -> None:
    widget = CardStudioWidget(catalog=make_catalog(), source_root=tmp_path / "missing")
    try:
        assert widget.release_list.count() == 0
        assert "not found" in widget.list_status.text().lower()
        assert "not found" in widget.report.toPlainText().lower()
        assert not widget.dry_run_button.isEnabled()
        assert not widget.write_button.isEnabled()
    finally:
        widget.deleteLater()
