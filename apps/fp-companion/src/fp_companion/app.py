from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .ui.card_studio import CardStudioWidget
from .ui.lut_studio import LutStudioWidget
from .ui.monitor import MonitorWidget


def _placeholder(title: str, text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    heading = QLabel(title)
    heading.setStyleSheet("font-size: 22px; font-weight: 600;")
    layout.addWidget(heading)
    layout.addWidget(QLabel(text))
    layout.addStretch(1)
    return page


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("fp Companion")
        self.resize(1100, 720)

        tabs = QTabWidget()
        tabs.addTab(_placeholder("Dashboard", "Camera, card, and firmware status will appear here."), "Dashboard")
        tabs.addTab(CardStudioWidget(), "Card Studio")
        tabs.addTab(LutStudioWidget(), "LUT Studio")
        tabs.addTab(MonitorWidget(), "Monitor")
        tabs.addTab(_placeholder("Tether", "PTP control, capture, and recording."), "Tether")
        tabs.addTab(_placeholder("Offload", "Checksum copy, proxies, and CinemaDNG packing."), "Offload")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("fp Companion 0.1.0 — no camera connected")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("fp Companion")
    window = MainWindow()
    window.show()
    return app.exec()
