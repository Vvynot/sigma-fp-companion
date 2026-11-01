from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from fp_companion.ui import monitor


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def test_widget_builds_without_probing_camera(qapp: QApplication) -> None:
    widget = monitor.MonitorWidget()
    try:
        assert widget.device_combo is not None
        assert widget.refresh_button is not None
        assert widget.start_button is not None
        assert widget.stop_button is not None
        assert widget.false_color_toggle is not None
        assert widget.lut_toggle is not None
        assert widget.intensity_slider is not None
        assert widget.histogram is not None
        # Construction must not block on a camera probe.
        assert widget.device_combo.count() == 0
        assert not widget.start_button.isEnabled()
        assert not widget.stop_button.isEnabled()
        assert not widget._timer.isActive()
    finally:
        widget.close()
        widget.deleteLater()


def test_construction_does_not_call_list_uvc_devices(
    qapp: QApplication, monkeypatch
) -> None:
    calls: list[None] = []

    def fake_list() -> list[str]:
        calls.append(None)
        return ["Camera 0"]

    monkeypatch.setattr(monitor, "list_uvc_devices", fake_list)
    widget = monitor.MonitorWidget()
    try:
        # A probe opens DSHOW indices, so it must never run during construction.
        assert calls == []
        assert widget.device_combo.count() == 0
        assert "refresh" in widget.status_label.text().lower()
    finally:
        widget.close()
        widget.deleteLater()


def test_refresh_with_no_devices(qapp: QApplication, monkeypatch) -> None:
    monkeypatch.setattr(monitor, "list_uvc_devices", lambda: [])
    widget = monitor.MonitorWidget()
    try:
        widget.refresh_button.click()

        assert widget.device_combo.count() == 0
        assert "no uvc devices" in widget.status_label.text().lower()
        assert not widget.start_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_refresh_populates_combo(qapp: QApplication, monkeypatch) -> None:
    monkeypatch.setattr(monitor, "list_uvc_devices", lambda: ["Camera 2"])
    widget = monitor.MonitorWidget()
    try:
        widget.refresh_button.click()

        assert widget.device_combo.count() == 1
        assert widget.device_combo.currentText() == "Camera 2"
        assert widget.device_combo.currentData() == 2
        assert widget.start_button.isEnabled()
        assert not widget.stop_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_false_color_toggle_is_safe_without_camera(qapp: QApplication) -> None:
    widget = monitor.MonitorWidget()
    try:
        widget.false_color_toggle.setChecked(True)
        assert widget.false_color_toggle.isChecked()

        frame = np.zeros((16, 24, 3), dtype=np.uint8)
        frame[:, :, 1] = 120
        widget.show_frame(frame)

        assert widget._last_image is not None
        assert int(widget.histogram._histogram.sum()) == 16 * 24
    finally:
        widget.close()
        widget.deleteLater()


def test_lut_and_intensity_pipeline(qapp: QApplication) -> None:
    widget = monitor.MonitorWidget()
    try:
        frame = np.full((16, 24, 3), 96, dtype=np.uint8)
        widget.show_frame(frame)

        widget.lut_toggle.setChecked(True)
        widget.intensity_slider.setValue(50)
        widget.false_color_toggle.setChecked(True)
        widget.false_color_toggle.setChecked(False)
        widget.intensity_slider.setValue(100)

        assert widget.intensity_value.text() == "100%"
        assert widget._last_image is not None
    finally:
        widget.close()
        widget.deleteLater()


def test_start_failure_is_reported(qapp: QApplication, monkeypatch) -> None:
    monkeypatch.setattr(monitor, "list_uvc_devices", lambda: ["Camera 0"])
    widget = monitor.MonitorWidget()
    try:
        widget.refresh_button.click()
        monkeypatch.setattr(widget, "_open_capture", lambda index: None)
        widget.start_button.click()

        assert not widget._timer.isActive()
        assert "could not open" in widget.status_label.text().lower()
        assert not widget.stop_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_device_disappearance_stops_preview(qapp: QApplication) -> None:
    class DeadCapture:
        def read(self):
            return False, None

        def release(self) -> None:
            pass

    widget = monitor.MonitorWidget()
    try:
        widget._capture = DeadCapture()
        widget._device_label = "Camera 0"
        widget._timer.start()

        for _ in range(monitor.MAX_READ_FAILURES):
            widget._grab_frame()

        assert not widget._timer.isActive()
        assert widget._capture is None
        assert "disconnected" in widget.status_label.text().lower()
    finally:
        widget.close()
        widget.deleteLater()


def test_float_frame_without_lut_is_mid_gray(qapp: QApplication) -> None:
    widget = monitor.MonitorWidget()
    try:
        assert not widget.lut_toggle.isChecked()

        widget.show_frame(np.full((8, 8, 3), 0.5, dtype=np.float32))
        color = widget._last_image.pixelColor(0, 0)
        assert (color.red(), color.green(), color.blue()) == (128, 128, 128)

        # uint8 frames keep their byte values.
        widget.show_frame(np.full((8, 8, 3), 96, dtype=np.uint8))
        color = widget._last_image.pixelColor(0, 0)
        assert (color.red(), color.green(), color.blue()) == (96, 96, 96)
    finally:
        widget.close()
        widget.deleteLater()


def test_to_display_uint8_normalises_float_and_clips() -> None:
    frame = np.array([[-1.0, 0.0, 0.5, 1.0, 2.0]], dtype=np.float32)

    assert monitor.to_display_uint8(frame).tolist() == [[0, 0, 128, 255, 255]]


def test_stop_releases_capture_and_is_idempotent(qapp: QApplication) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.release_count = 0

        def release(self) -> None:
            self.release_count += 1

    widget = monitor.MonitorWidget()
    try:
        capture = FakeCapture()
        widget._capture = capture
        widget._device_label = "Camera 0"
        widget._timer.start()

        widget.stop()

        assert not widget._timer.isActive()
        assert widget._capture is None
        assert capture.release_count == 1

        widget.stop()
        assert capture.release_count == 1
    finally:
        widget.close()
        widget.deleteLater()


def test_hide_pauses_and_show_resumes_running_preview(
    qapp: QApplication,
) -> None:
    widget = monitor.MonitorWidget()
    try:
        widget.show()
        qapp.processEvents()
        assert not widget._timer.isActive()

        widget._capture = object()
        widget._timer.start()

        widget.hide()
        qapp.processEvents()
        assert not widget._timer.isActive()

        widget.show()
        qapp.processEvents()
        assert widget._timer.isActive()

        widget.stop()
    finally:
        widget.close()
        widget.deleteLater()


def test_show_does_not_restart_preview_that_was_not_running(
    qapp: QApplication,
) -> None:
    widget = monitor.MonitorWidget()
    try:
        widget.show()
        qapp.processEvents()
        widget.hide()
        qapp.processEvents()
        widget.show()
        qapp.processEvents()

        assert not widget._timer.isActive()
    finally:
        widget.close()
        widget.deleteLater()


def test_main_window_stops_monitor_on_about_to_quit(
    qapp: QApplication,
) -> None:
    from fp_companion.app import MainWindow

    class FakeCapture:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    window = MainWindow()
    try:
        capture = FakeCapture()
        window.monitor._capture = capture
        window.monitor._timer.start()

        qapp.aboutToQuit.emit()

        assert capture.released
        assert window.monitor._capture is None
        assert not window.monitor._timer.isActive()
    finally:
        window.close()
        window.deleteLater()

