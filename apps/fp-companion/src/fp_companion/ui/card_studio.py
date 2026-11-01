from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import load_builtin
from ..core.models import Catalog, Release, ValidationIssue
from ..core.plan import PROJECT_ROOT, make_plan, validate_plan

SSD_RELEASE_IDS = {"og3k", "og4k", "res-custom"}
CONFIRM_RISKS = {"test", "alpha"}
STOCK_WARNING = "Before removing the card, restore a stock color mode and power off cleanly."

DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "vendor" / "fpSup" / "fpSup" / "releases"


def default_source_root() -> Path:
    """Releases folder from ``FPSUP_RELEASES`` or the in-repo dev location."""
    override = os.environ.get("FPSUP_RELEASES", "").strip()
    return Path(override) if override else DEFAULT_SOURCE_ROOT


def _version_key(version: str) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"(\d+)", version)
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
        if part
    )


def _human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TiB"


def _format_issues(issues: tuple[ValidationIssue, ...]) -> str:
    if not issues:
        return "Issues: none."
    lines = ["Issues:"]
    lines.extend(f"- [{issue.severity}] {issue.code}: {issue.message}" for issue in issues)
    return "\n".join(lines)


class CardStudioWidget(QWidget):
    """Prepare a single fpSup module card: inspect, dry-run, then write."""

    def __init__(
        self,
        catalog: Catalog | None = None,
        source_root: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog if catalog is not None else load_builtin()
        self.source_root = Path(source_root) if source_root is not None else default_source_root()
        self._release_by_directory: dict[str, Release] = {}
        self._selected: Release | None = None
        self._build_ui()
        self._populate_releases()

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
        left_layout.addWidget(QLabel("Releases"))
        self.release_list = QListWidget()
        self.release_list.currentItemChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.release_list, 1)
        self.list_status = QLabel()
        self.list_status.setWordWrap(True)
        self.list_status.setTextFormat(Qt.TextFormat.PlainText)
        self.list_status.setStyleSheet("color: #8a6d3b;")
        self.list_status.hide()
        left_layout.addWidget(self.list_status)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Details"))
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.details)
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings.setStyleSheet("color: #b36b00;")
        right_layout.addWidget(self.warnings)
        right_layout.addStretch(1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target directory:"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("Select the mounted SD card folder")
        target_row.addWidget(self.target_edit, 1)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse)
        target_row.addWidget(self.browse_button)
        layout.addLayout(target_row)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.dry_run_button = QPushButton("Dry run")
        self.dry_run_button.clicked.connect(self._on_dry_run)
        button_row.addWidget(self.dry_run_button)
        self.write_button = QPushButton("Write card")
        self.write_button.clicked.connect(self._on_write)
        button_row.addWidget(self.write_button)
        layout.addLayout(button_row)

        layout.addWidget(QLabel("Report"))
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Dry-run and write results appear here.")
        layout.addWidget(self.report, 1)

        self._set_actions_enabled(False)

    def _populate_releases(self) -> None:
        self.release_list.clear()
        self._release_by_directory.clear()
        self._selected = None
        self._set_actions_enabled(False)
        self.list_status.hide()

        if not self.source_root.is_dir():
            message = (
                f"Releases folder not found:\n{self.source_root}\n\n"
                "Set FPSUP_RELEASES or place the fpSup releases in vendor/fpSup/fpSup/releases."
            )
            self.list_status.setText(message)
            self.list_status.show()
            self.details.setText("Card Studio could not find the fpSup releases folder.")
            self.warnings.clear()
            self.report.setPlainText(
                f"Releases folder not found: {self.source_root}\n"
                "No card can be prepared until the releases are available."
            )
            return

        releases = sorted(
            self.catalog.releases,
            key=lambda release: (release.id, _version_key(release.version)),
        )
        if not releases:
            self.list_status.setText("No releases found in the catalog.")
            self.list_status.show()
            self.details.setText("No releases are available.")
            self.warnings.clear()
            return

        for release in releases:
            item = QListWidgetItem(f"{release.name} — v{release.version} ({release.risk})")
            item.setData(Qt.ItemDataRole.UserRole, release.directory)
            self.release_list.addItem(item)
            self._release_by_directory[release.directory] = release

        self.details.setText("Select a release to see its details.")

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.dry_run_button.setEnabled(enabled)
        self.write_button.setEnabled(enabled)

    # ------------------------------------------------------------- selection

    def _on_selection_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None = None,
    ) -> None:
        release = None
        if current is not None:
            release = self._release_by_directory.get(current.data(Qt.ItemDataRole.UserRole))

        self._selected = release
        self._set_actions_enabled(release is not None)
        if release is None:
            self.details.setText("Select a release to see its details.")
            self.warnings.clear()
            return

        self.details.setText(self._details_text(release))
        self.warnings.setText(self._warnings_text(release))

    def _details_text(self, release: Release) -> str:
        min_svc = "unknown" if release.min_svc is None else str(release.min_svc)
        return "\n".join(
            [
                release.name,
                release.summary or "No summary available.",
                "",
                f"Id: {release.id}",
                f"Version: {release.version}",
                f"Directory: {release.directory}",
                f"Firmware: {release.firmware}",
                f"Loader: v{release.loader}",
                f"Min service: {min_svc}",
                f"Category: {release.category}",
                f"Files: {len(release.files)}",
                f"Total size: {_human_bytes(release.total_bytes)} ({release.total_bytes} bytes)",
                f"Risk: {release.risk}",
            ]
        )

    def _warnings_text(self, release: Release) -> str:
        warnings: list[str] = []
        if release.risk != "release":
            warnings.append(
                f"Risk level: {release.risk}. Test this module on a spare card first."
            )
        if release.id in SSD_RELEASE_IDS:
            warnings.append("This mode requires an external USB-C SSD, not the SD card.")
        warnings.append(STOCK_WARNING)
        return "Warnings:\n" + "\n".join(f"- {warning}" for warning in warnings)

    # ---------------------------------------------------------------- actions

    def _browse(self) -> None:
        start = self.target_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "Select card target", start)
        if directory:
            self.target_edit.setText(directory)

    def _target_path(self) -> Path | None:
        text = self.target_edit.text().strip()
        return Path(text) if text else None

    def _on_dry_run(self) -> None:
        release = self._selected
        if release is None:
            self.report.setPlainText("Select a release first.")
            return
        target = self._target_path()
        if target is None:
            self.report.setPlainText("Choose a target directory first (Browse...).")
            return

        try:
            plan = make_plan(release, target, self.source_root, dry_run=True)
            result = validate_plan(plan)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self.report.setPlainText(f"Dry run failed: {exc}")
            return

        status = "OK — ready to write." if result.ok else "BLOCKED — fix errors before writing."
        self.report.setPlainText(
            "\n".join(
                [
                    f"Dry run — {release.name} v{release.version}",
                    f"Target: {target}",
                    f"Source: {self.source_root}",
                    "",
                    f"Status: {status}",
                    "",
                    _format_issues(result.issues),
                    "",
                    "No files were written.",
                ]
            )
        )

    def _on_write(self) -> None:
        release = self._selected
        if release is None:
            self.report.setPlainText("Select a release first.")
            return
        target = self._target_path()
        if target is None:
            self.report.setPlainText("Choose a target directory first (Browse...).")
            return

        if release.risk in CONFIRM_RISKS:
            answer = QMessageBox.question(
                self,
                "Write card",
                (
                    f"Release risk level is '{release.risk}'.\n\n"
                    f"Write {release.name} v{release.version} to:\n{target}\n\nContinue?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.report.setPlainText("Write cancelled.")
                return

        try:
            from ..core.writer import write_card
        except ImportError:
            self.report.setPlainText(
                "Write is not available yet: the writer module is missing."
            )
            return

        try:
            plan = make_plan(release, target, self.source_root, dry_run=False)
            result = write_card(plan)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self.report.setPlainText(f"Write failed: {exc}")
            return

        files = tuple(getattr(result, "files", ()))
        written = int(getattr(result, "bytes_written", 0))
        verified = tuple(getattr(result, "verified", ()))
        backup = getattr(result, "backup", None)
        issues = tuple(getattr(result, "issues", ()))
        self.report.setPlainText(
            "\n".join(
                [
                    f"Write — {release.name} v{release.version}",
                    f"Result: {'OK' if result.ok else 'FAILED'}",
                    f"Target: {getattr(result, 'target', target)}",
                    f"Files: {len(files)}",
                    f"Bytes written: {_human_bytes(written)} ({written} bytes)",
                    f"Verified: {len(verified)}",
                    f"Backup: {backup if backup is not None else 'none'}",
                    "",
                    _format_issues(issues),
                ]
            )
        )
