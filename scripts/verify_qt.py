import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fp_companion.app import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    print(f"{window.windowTitle()} tabs={window.centralWidget().count()}")
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
