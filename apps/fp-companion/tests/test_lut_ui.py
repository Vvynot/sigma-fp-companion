from __future__ import annotations

import hashlib
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

# The UI stages through the real core; import it eagerly so a missing or
# broken core fails the tests loudly instead of being silently tolerated.
from fp_companion.core import lut_card  # noqa: F401
from fp_companion.ui.lut_studio import LutStudioWidget

CUBE_SIZE = 17
CUBE_ENTRIES = CUBE_SIZE**3


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def _cube_text(size: int, nan_index: int | None = None) -> str:
    """Return a valid identity .cube of ``size`` cubed entries.

    ``nan_index``, when given, replaces that entry's values with NaN while
    keeping the grid size and entry count valid.
    """

    scale = size - 1
    lines = [
        'TITLE "identity"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0 0 0",
        "DOMAIN_MAX 1 1 1",
    ]
    index = 0
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                if index == nan_index:
                    lines.append("nan 0 0")
                else:
                    lines.append(
                        f"{red / scale:.6f} {green / scale:.6f} {blue / scale:.6f}"
                    )
                index += 1
    return "\n".join(lines) + "\n"


def make_cube(
    tmp_path: Path,
    name: str = "identity.cube",
    *,
    size: int = CUBE_SIZE,
    nan_index: int | None = None,
) -> Path:
    cube = tmp_path / name
    cube.write_text(_cube_text(size, nan_index), encoding="utf-8")
    return cube


@pytest.fixture(scope="module")
def identity_cube(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("cube")
    return make_cube(directory)


def test_metadata_and_action_state(
    qapp: QApplication, identity_cube: Path, tmp_path: Path
) -> None:
    widget = LutStudioWidget()
    try:
        assert not widget.validate_button.isEnabled()
        assert not widget.stage_button.isEnabled()

        widget.set_cube_path(identity_cube)

        metadata = widget.metadata.text()
        assert "identity" in metadata
        assert f"3D {CUBE_SIZE}x{CUBE_SIZE}x{CUBE_SIZE} ({CUBE_ENTRIES} entries)" in metadata
        assert "Domain: (0, 0, 0) -> (1, 1, 1)" in metadata
        assert (
            hashlib.sha256(identity_cube.read_bytes()).hexdigest()[:16] in metadata
        )
        assert identity_cube.name in metadata

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


def test_validate_reports_settings(
    qapp: QApplication, identity_cube: Path, tmp_path: Path
) -> None:
    target = tmp_path / "card"
    target.mkdir()

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(identity_cube)
        widget.target_edit.setText(str(target))
        widget.validate_button.click()

        report = widget.report.toPlainText()
        assert "validation" in report.lower()
        assert str(target) in report
        assert "Intensity: 100%" in report
        # A 17^3 cube is valid but not the preferred 33^3 size: warning only.
        assert "Status: OK" in report
        assert "BLOCKED" not in report
        assert "[warning] size:" in report
        assert list(target.iterdir()) == []
    finally:
        widget.deleteLater()


@pytest.mark.parametrize(
    ("size", "nan_index", "expected"),
    [
        (2, None, "Unsupported 3D LUT size 2"),
        (CUBE_SIZE, 0, "non-finite"),
    ],
)
def test_invalid_cube_blocks_validate_and_stage(
    qapp: QApplication,
    tmp_path: Path,
    size: int,
    nan_index: int | None,
    expected: str,
) -> None:
    cube = make_cube(tmp_path, f"invalid-{size}-{nan_index}.cube", size=size, nan_index=nan_index)
    target = tmp_path / "card"
    target.mkdir()

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(cube)
        assert widget.validate_button.isEnabled()

        widget.target_edit.setText(str(target))
        widget.validate_button.click()
        validate_report = widget.report.toPlainText()
        assert expected in validate_report
        assert "Status: BLOCKED" in validate_report

        widget.stage_button.click()
        stage_report = widget.report.toPlainText()
        assert expected in stage_report
        assert "Result: FAILED" in stage_report
        assert not (target / "LUT").exists()
    finally:
        widget.deleteLater()


def test_stage_copies_valid_cube_unchanged(
    qapp: QApplication, identity_cube: Path, tmp_path: Path
) -> None:
    target = tmp_path / "card"
    target.mkdir()

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(identity_cube)
        widget.target_edit.setText(str(target))
        widget.stage_button.click()

        report = widget.report.toPlainText()
        source = target / "LUT" / "source.cube"
        assert "Result: OK" in report, report
        assert "SHA-256:" in report
        assert source.is_file(), report
        assert source.read_bytes() == identity_cube.read_bytes()
        assert (target / "LUT" / "settings.json").is_file()
    finally:
        widget.deleteLater()


def test_stage_refuses_symlinked_lut_entry(
    qapp: QApplication, identity_cube: Path, tmp_path: Path
) -> None:
    target = tmp_path / "card"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    lut_link = target / "LUT"
    try:
        os.symlink(outside, lut_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This platform cannot create directory symlinks without privileges")

    widget = LutStudioWidget()
    try:
        widget.set_cube_path(identity_cube)
        widget.target_edit.setText(str(target))

        widget.validate_button.click()
        assert "symlink or junction" in widget.report.toPlainText()

        widget.stage_button.click()
        report = widget.report.toPlainText()
        assert "symlink or junction" in report
        assert "Result: FAILED" in report
        assert not (outside / "source.cube").exists()
    finally:
        widget.deleteLater()
