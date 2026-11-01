from __future__ import annotations

import hashlib
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from fp_companion.ui.lut_studio import LutStudioWidget

IDENTITY_DATA = (
    "0 0 0\n"
    "1 0 0\n"
    "0 1 0\n"
    "1 1 0\n"
    "0 0 1\n"
    "1 0 1\n"
    "0 1 1\n"
    "1 1 1\n"
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def make_identity_cube(tmp_path: Path) -> Path:
    cube = tmp_path / "identity.cube"
    cube.write_text(
        'TITLE "identity"\n'
        "LUT_3D_SIZE 2\n"
        "DOMAIN_MIN 0 0 0\n"
        "DOMAIN_MAX 1 1 1\n" + IDENTITY_DATA,
        encoding="utf-8",
    )
    return cube


def has_lut_card() -> bool:
    try:
        import fp_companion.core.lut_card  # noqa: F401
    except Exception:
        return False
    return True


def test_metadata_and_action_state(qapp: QApplication, tmp_path: Path) -> None:
    cube = make_identity_cube(tmp_path)
    widget = LutStudioWidget()
    try:
        assert not widget.validate_button.isEnabled()
        assert not widget.stage_button.isEnabled()

        widget.set_cube_path(cube)

        metadata = widget.metadata.text()
        assert "identity" in metadata
        assert "3D 2x2x2 (8 entries)" in metadata
        assert "Domain: (0, 0, 0) -> (1, 1, 1)" in metadata
        assert hashlib.sha256(cube.read_bytes()).hexdigest()[:16] in metadata
        assert "identity.cube" in metadata

        assert widget.validate_button.isEnabled()
        # No target directory yet: staging stays disabled.
        assert not widget.stage_button.isEnabled()

        target = tmp_path / "card"
        target.mkdir()
        widget.target_edit.setText(str(target))
        assert widget.stage_button.isEnabled()
    finally:
        widget.deleteLater()


def test_invalid_cube_disables_actions(qapp: QApplication, tmp_path: Path) -> None:
    broken = tmp_path / "broken.cube"
    broken.write_text("TITLE \"broken\"\nLUT_3D_SIZE 2\n0 0 0\n", encoding="utf-8")

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(broken)
        assert not widget.validate_button.isEnabled()
        assert not widget.stage_button.isEnabled()
        assert "Could not read" in widget.metadata.text()
        assert "Load failed" in widget.report.toPlainText()
    finally:
        widget.deleteLater()


def test_validate_reports_settings(qapp: QApplication, tmp_path: Path) -> None:
    cube = make_identity_cube(tmp_path)
    target = tmp_path / "card"
    target.mkdir()

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(cube)
        widget.target_edit.setText(str(target))
        widget.validate_button.click()

        report = widget.report.toPlainText()
        assert "validation" in report.lower()
        assert str(target) in report
        assert "Intensity: 100%" in report
        if not has_lut_card():
            assert "LUT core is not available yet" in report
        assert list(target.iterdir()) == []
    finally:
        widget.deleteLater()


def test_stage_or_reports_missing_core(qapp: QApplication, tmp_path: Path) -> None:
    cube = make_identity_cube(tmp_path)
    target = tmp_path / "card"
    target.mkdir()

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(cube)
        widget.target_edit.setText(str(target))
        widget.stage_button.click()

        report = widget.report.toPlainText()
        if has_lut_card():
            source = target / "LUT" / "source.cube"
            assert source.is_file(), report
            assert "Result: OK" in report
            assert "SHA-256:" in report
            assert source.read_bytes() == cube.read_bytes()
        else:
            assert "LUT core is not available yet" in report
            assert "No files were written." in report
            assert not (target / "LUT").exists()
    finally:
        widget.deleteLater()
