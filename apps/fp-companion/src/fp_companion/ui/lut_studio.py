from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.lut import CubeLUT, parse_cube, validate_cube
from ..core.models import ValidationIssue
from ..core.plan import PROJECT_ROOT

# Product contract from vendor/fpSup/projects/lut-sup/README.md.  The output is
# fixed at Rec.709 / Gamma 2.4, so only the input side is selectable here.
COLOR_SPACES = (
    "ACES",
    "Adobe RGB",
    "Blackmagic Design",
    "P3 D60",
    "P3 D65",
    "Rec.2020",
    "Rec.709",
    "sRGB",
)
NON_BLACKMAGIC_GAMMAS = (
    "Adobe RGB",
    "Gamma 2.4",
    "Gamma 2.6",
    "Linear",
    "Rec.709",
    "sRGB",
)
BLACKMAGIC_GAMMAS = (
    "Blackmagic Design Film",
    "Blackmagic Extended Video",
)
BLACKMAGIC_COLOR_SPACE = "Blackmagic Design"
DEFAULT_COLOR_SPACE = "Rec.709"
DEFAULT_GAMMA = "Gamma 2.4"

# Files/directories that suggest the user pointed at an existing camera card.
CARD_MARKERS = ("AutoRun.txt", "LOADER.BIN", "SUP", "LUT", "DCIM", "MISC")
CUBE_FILTER = "Cube LUT (*.cube);;All files (*)"


def _human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TiB"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_tuple(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Path)):
        return (value,)
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return (value,)


def _format_issues(issues: tuple[object, ...]) -> str:
    if not issues:
        return "Issues: none."
    lines = ["Issues:"]
    for issue in issues:
        severity = getattr(issue, "severity", "info")
        code = getattr(issue, "code", "")
        message = getattr(issue, "message", str(issue))
        lines.append(f"- [{severity}] {code}: {message}")
    return "\n".join(lines)


def _has_errors(issues: tuple[object, ...]) -> bool:
    return any(str(getattr(issue, "severity", "")).lower() == "error" for issue in issues)


def _is_link_or_junction(path: Path) -> bool:
    """True when ``path`` is a symlink or a Windows junction/reparse point."""

    try:
        if path.is_symlink():
            return True
    except OSError:
        pass

    is_junction = getattr(path, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            pass

    if os.name == "nt":
        try:
            attributes = os.lstat(path).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _format_vector(vector: tuple[float, float, float]) -> str:
    return "(" + ", ".join(f"{value:g}" for value in vector) + ")"


class LutStudioWidget(QWidget):
    """Import a .cube LUT, choose its input transform, and stage it on a card."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cube_path: Path | None = None
        self._cube: CubeLUT | None = None
        self._build_ui()
        self._refresh_gamma_options()
        self._update_actions()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("LUT file"))

        cube_row = QHBoxLayout()
        self.cube_edit = QLineEdit()
        self.cube_edit.setReadOnly(True)
        self.cube_edit.setPlaceholderText("Select a .cube file")
        cube_row.addWidget(self.cube_edit, 1)
        self.cube_browse_button = QPushButton("Browse...")
        self.cube_browse_button.clicked.connect(self._browse_cube)
        cube_row.addWidget(self.cube_browse_button)
        left_layout.addLayout(cube_row)

        left_layout.addWidget(QLabel("Input settings"))
        form = QFormLayout()
        self.color_space_combo = QComboBox()
        self.color_space_combo.addItems(COLOR_SPACES)
        self.color_space_combo.setCurrentText(DEFAULT_COLOR_SPACE)
        form.addRow("Input color space:", self.color_space_combo)

        self.gamma_combo = QComboBox()
        form.addRow("Input gamma:", self.gamma_combo)

        intensity_row = QHBoxLayout()
        self.intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self.intensity_slider.setRange(0, 100)
        self.intensity_slider.setValue(100)
        self.intensity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.intensity_slider.setTickInterval(25)
        intensity_row.addWidget(self.intensity_slider, 1)
        self.intensity_label = QLabel()
        self.intensity_label.setMinimumWidth(40)
        self.intensity_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        intensity_row.addWidget(self.intensity_label)
        form.addRow("Intensity:", intensity_row)
        left_layout.addLayout(form)
        left_layout.addStretch(1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Metadata"))
        self.metadata = QLabel()
        self.metadata.setWordWrap(True)
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata.setText("Load a .cube file to see its metadata.")
        right_layout.addWidget(self.metadata)
        right_layout.addStretch(1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target directory:"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("Select the mounted SD card folder")
        self.target_edit.textChanged.connect(self._update_actions)
        target_row.addWidget(self.target_edit, 1)
        self.target_browse_button = QPushButton("Browse...")
        self.target_browse_button.clicked.connect(self._browse_target)
        target_row.addWidget(self.target_browse_button)
        layout.addLayout(target_row)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.validate_button = QPushButton("Validate")
        self.validate_button.clicked.connect(self._on_validate)
        button_row.addWidget(self.validate_button)
        self.stage_button = QPushButton("Stage LUT")
        self.stage_button.clicked.connect(self._on_stage)
        button_row.addWidget(self.stage_button)
        layout.addLayout(button_row)

        layout.addWidget(QLabel("Report"))
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Validation and staging results appear here.")
        layout.addWidget(self.report, 1)

        self.color_space_combo.currentTextChanged.connect(self._on_color_space_changed)
        self.intensity_slider.valueChanged.connect(self._update_intensity_label)
        self._update_intensity_label()

    def _update_intensity_label(self, value: int | None = None) -> None:
        if value is None:
            value = self.intensity_slider.value()
        self.intensity_label.setText(f"{value}%")

    def _on_color_space_changed(self, _text: str) -> None:
        self._refresh_gamma_options()

    def _refresh_gamma_options(self) -> None:
        choices = (
            BLACKMAGIC_GAMMAS
            if self.color_space_combo.currentText() == BLACKMAGIC_COLOR_SPACE
            else NON_BLACKMAGIC_GAMMAS
        )
        previous = self.gamma_combo.currentText()
        self.gamma_combo.blockSignals(True)
        self.gamma_combo.clear()
        self.gamma_combo.addItems(choices)
        if previous in choices:
            self.gamma_combo.setCurrentText(previous)
        elif DEFAULT_GAMMA in choices:
            self.gamma_combo.setCurrentText(DEFAULT_GAMMA)
        self.gamma_combo.blockSignals(False)

    def _update_actions(self) -> None:
        valid_cube = self._cube is not None and self._cube_path is not None
        self.validate_button.setEnabled(valid_cube)
        self.stage_button.setEnabled(valid_cube and bool(self.target_edit.text().strip()))

    # ------------------------------------------------------------- loading

    def _browse_cube(self) -> None:
        start = self.cube_edit.text().strip() or str(Path.home())
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Select .cube LUT", start, CUBE_FILTER
        )
        if path:
            self.load_cube(path)

    def _browse_target(self) -> None:
        start = self.target_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "Select card target", start)
        if directory:
            self.target_edit.setText(directory)

    def set_cube_path(self, path: str | Path) -> None:
        """Load ``path`` as the active LUT (programmatic equivalent of Browse)."""

        self.load_cube(path)

    def load_cube(self, path: str | Path) -> None:
        cube_path = Path(path)
        self.cube_edit.setText(str(cube_path))
        self._cube_path = None
        self._cube = None

        try:
            cube = parse_cube(cube_path)
        except (OSError, ValueError) as exc:
            self.metadata.setText(
                "Could not read .cube file:\n"
                f"{cube_path}\n\n"
                f"{exc}"
            )
            self.report.setPlainText(
                f"Load failed — {cube_path.name}\n{exc}\n\nNo files were written."
            )
            self._update_actions()
            return

        self._cube_path = cube_path
        self._cube = cube
        self.metadata.setText(self._metadata_text(cube_path, cube))
        self.report.setPlainText(
            "\n".join(
                [
                    f"Loaded — {cube_path.name}",
                    f"Type: {self._type_text(cube)}",
                    "",
                    "Choose the input settings and a target directory, then Validate or Stage LUT.",
                ]
            )
        )
        self._update_actions()

    def _metadata_text(self, cube_path: Path, cube: CubeLUT) -> str:
        try:
            size = cube_path.stat().st_size
            digest = _sha256_file(cube_path)
            size_text = f"{_human_bytes(size)} ({size} bytes)"
            sha_text = f"{digest[:16]}..."
        except OSError as exc:
            size_text = f"unavailable ({exc})"
            sha_text = "unavailable"

        return "\n".join(
            [
                f"Title: {cube.title or '(untitled)'}",
                f"Type: {self._type_text(cube)}",
                f"Domain: {_format_vector(cube.domain_min)} -> {_format_vector(cube.domain_max)}",
                f"File: {cube_path.name}",
                f"Size: {size_text}",
                f"SHA-256: {sha_text}",
            ]
        )

    @staticmethod
    def _type_text(cube: CubeLUT) -> str:
        if cube.size_3d:
            return f"3D {cube.size_3d}x{cube.size_3d}x{cube.size_3d} ({cube.size_3d ** 3} entries)"
        return f"1D {cube.size_1d} ({cube.size_1d} entries)"

    # --------------------------------------------------------------- core

    @staticmethod
    def _load_core():
        """Import ``core.lut_card`` lazily so the widget loads without it."""

        try:
            from ..core import lut_card
        except Exception:  # pragma: no cover - depends on the other module
            return None
        return lut_card

    def _settings_from(self, core):
        return core.LutSettings(
            input_color_space=self.color_space_combo.currentText(),
            input_gamma=self.gamma_combo.currentText(),
            intensity=self.intensity_slider.value() / 100.0,
        )

    def _cube_name(self) -> str:
        if self._cube_path is not None:
            return self._cube_path.name
        return "(no cube)"

    # ---------------------------------------------------------- validation

    def _target_path(self) -> Path | None:
        text = self.target_edit.text().strip()
        return Path(text) if text else None

    def _target_issues(self, target: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        try:
            resolved = target.resolve()
        except OSError as exc:
            return [ValidationIssue("error", "target", f"Could not resolve target: {exc}")]

        if resolved == PROJECT_ROOT or resolved.is_relative_to(PROJECT_ROOT):
            issues.append(
                ValidationIssue(
                    "error",
                    "project",
                    f"Refusing to write inside the project tree: {resolved}",
                )
            )
        if not resolved.exists() or not resolved.is_dir():
            issues.append(
                ValidationIssue("error", "target", f"Target is not a directory: {target}")
            )

        lut_entry = resolved / "LUT"
        try:
            lut_exists = os.path.lexists(lut_entry)
        except OSError:
            lut_exists = False
        if lut_exists and _is_link_or_junction(lut_entry):
            issues.append(
                ValidationIssue(
                    "error",
                    "symlink",
                    "Refusing to stage: the target's LUT entry is a "
                    f"symlink or junction: {lut_entry}",
                )
            )
        return issues

    def _on_validate(self) -> None:
        if self._cube is None or self._cube_path is None:
            self.report.setPlainText("Load a .cube file first.")
            return

        core = self._load_core()
        if core is None:
            self.report.setPlainText(
                "\n".join(
                    [
                        f"Validation — {self._cube_name()}",
                        "Result: unavailable",
                        "LUT core is not available yet: fp_companion.core.lut_card is missing.",
                        "",
                        "No files were written.",
                    ]
                )
            )
            return

        target = self._target_path()
        issues = list(validate_cube(self._cube))
        issues.extend(core.validate_settings(self._settings_from(core)))
        if target is None:
            issues.append(
                ValidationIssue("error", "target", "Choose a target directory first (Browse...).")
            )
        else:
            issues.extend(self._target_issues(target))

        ok = not _has_errors(tuple(issues))
        self.report.setPlainText(
            "\n".join(
                [
                    f"Validation — {self._cube_name()}",
                    f"Input: {self.color_space_combo.currentText()} / "
                    f"{self.gamma_combo.currentText()}",
                    f"Intensity: {self.intensity_slider.value()}%",
                    f"Target: {target if target is not None else '(none)'}",
                    "",
                    f"Status: {'OK — ready to stage.' if ok else 'BLOCKED — fix errors before staging.'}",
                    "",
                    _format_issues(tuple(issues)),
                    "",
                    "No files were written.",
                ]
            )
        )

    # -------------------------------------------------------------- staging

    def _looks_like_card_root(self, target: Path) -> bool:
        try:
            if not target.is_dir():
                return False
            names = {entry.name.upper() for entry in target.iterdir()}
        except OSError:
            return False
        return any(marker.upper() in names for marker in CARD_MARKERS)

    def _on_stage(self) -> None:
        if self._cube is None or self._cube_path is None:
            self.report.setPlainText("Load a .cube file first.")
            return

        target = self._target_path()
        if target is None:
            self.report.setPlainText("Choose a target directory first (Browse...).")
            return

        core = self._load_core()
        if core is None:
            self.report.setPlainText(
                "\n".join(
                    [
                        f"Staging — {self._cube_name()}",
                        "Result: unavailable",
                        "LUT core is not available yet: fp_companion.core.lut_card is missing.",
                        "",
                        "No files were written.",
                    ]
                )
            )
            return

        settings = self._settings_from(core)
        issues = list(validate_cube(self._cube))
        issues.extend(core.validate_settings(settings))
        issues.extend(self._target_issues(target))
        if _has_errors(tuple(issues)):
            self.report.setPlainText(
                "\n".join(
                    [
                        f"Staging — {self._cube_name()}",
                        f"Target: {target}",
                        "Result: FAILED — fix errors before staging.",
                        "",
                        _format_issues(tuple(issues)),
                        "",
                        "No files were written.",
                    ]
                )
            )
            return

        if self._looks_like_card_root(target):
            answer = QMessageBox.question(
                self,
                "Stage LUT",
                (
                    "This target looks like an existing camera card root.\n\n"
                    f"Stage {self._cube_name()} under:\n{target / 'LUT'}\n\nContinue?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.report.setPlainText("Staging cancelled.")
                return

        try:
            result = core.write_lut_staging(target, self._cube_path, settings)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self.report.setPlainText(f"Staging failed: {exc}")
            return

        self._show_stage_result(result, target, settings)

    def _show_stage_result(self, result: object, target: Path, settings: object) -> None:
        ok = bool(getattr(result, "ok", False))
        result_target = Path(getattr(result, "target", target))
        files = _as_tuple(getattr(result, "files", ()))
        issues = _as_tuple(getattr(result, "issues", ()))

        digest = None
        for attribute in ("sha256", "cube_sha256", "hash", "source_sha256"):
            value = getattr(result, attribute, None)
            if value:
                digest = str(value)
                break

        source_cube = result_target / "LUT" / "source.cube"
        if digest is None:
            try:
                if source_cube.is_file():
                    digest = _sha256_file(source_cube)
            except OSError:
                digest = None
        if not files and source_cube.is_file():
            files = ("LUT/source.cube",)

        lines = [
            f"Staging — {self._cube_name()}",
            f"Result: {'OK' if ok else 'FAILED'}",
            f"Target: {result_target}",
            f"Input: {getattr(settings, 'input_color_space', self.color_space_combo.currentText())} / "
            f"{getattr(settings, 'input_gamma', self.gamma_combo.currentText())}",
            f"Intensity: {self.intensity_slider.value()}%",
            f"Files: {len(files)}",
        ]
        if files:
            lines.append("Written:")
            lines.extend(f"- {path}" for path in files)
        lines.extend(["", f"SHA-256: {digest if digest is not None else 'unknown'}", ""])
        lines.append(_format_issues(issues))
        self.report.setPlainText("\n".join(lines))
