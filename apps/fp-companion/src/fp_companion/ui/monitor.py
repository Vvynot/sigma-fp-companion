"""UVC monitor page: live preview, luma histogram, false color and LUT.

The MVP captures frames with OpenCV on a QTimer in the UI thread.  There is no
worker thread yet; keep capture resolution modest until a threaded pipeline
lands.
"""

from __future__ import annotations

import sys
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.uvc import apply_lut, false_color, list_uvc_devices, luma_histogram

PREVIEW_INTERVAL_MS = 33  # ~30 fps
MAX_READ_FAILURES = 3


class HistogramWidget(QWidget):
    """Small filled-area luma histogram drawn with QPainter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._histogram = np.zeros(256, dtype=np.float64)
        self.setMinimumSize(160, 96)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def set_histogram(self, histogram: np.ndarray) -> None:
        self._histogram = np.asarray(histogram, dtype=np.float64).ravel()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor("#141414"))
            histogram = self._histogram
            if histogram.size == 0 or histogram.max() <= 0:
                return

            left, top, right, bottom = 2, 2, self.width() - 2, self.height() - 2
            if right <= left or bottom <= top:
                return

            normalised = histogram / histogram.max()
            path = QPainterPath()
            path.moveTo(float(left), float(bottom))
            last = max(1, histogram.size - 1)
            for index, value in enumerate(normalised):
                x = left + (right - left) * (index / last)
                y = bottom - (bottom - top) * float(value)
                path.lineTo(float(x), float(y))
            path.lineTo(float(right), float(bottom))
            path.closeSubpath()

            painter.fillPath(path, QColor("#4f8cff"))
            painter.setPen(QPen(QColor("#3a3a3a"), 1))
            painter.drawRect(left, top, right - left, bottom - top)
        finally:
            painter.end()


class MonitorWidget(QWidget):
    """UVC live view with luma scope, false color and a demo LUT."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._capture = None
        self._device_label = ""
        self._read_failures = 0
        self._fps_estimate: float | None = None
        self._last_tick: float | None = None
        self._last_raw_frame: np.ndarray | None = None
        self._last_image: QImage | None = None
        self._demo_lut_cache: np.ndarray | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(PREVIEW_INTERVAL_MS)
        self._timer.timeout.connect(self._grab_frame)

        self._build_ui()
        self._update_controls()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("UVC device:"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        controls.addWidget(self.device_combo)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_devices)
        controls.addWidget(self.refresh_button)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_preview)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_preview)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        body = QHBoxLayout()
        self.preview_label = QLabel("No signal")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(320, 180)
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_label.setStyleSheet(
            "background: #000000; color: #777777; border: 1px solid #2a2a2a;"
        )
        body.addWidget(self.preview_label, 3)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(QLabel("Luma histogram"))
        self.histogram = HistogramWidget()
        side.addWidget(self.histogram, 1)

        self.false_color_toggle = QCheckBox("False color")
        self.false_color_toggle.toggled.connect(self._on_display_option_changed)
        side.addWidget(self.false_color_toggle)

        self.lut_toggle = QCheckBox("LUT (demo look)")
        self.lut_toggle.toggled.connect(self._on_display_option_changed)
        side.addWidget(self.lut_toggle)

        intensity_row = QHBoxLayout()
        intensity_row.addWidget(QLabel("Intensity"))
        self.intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self.intensity_slider.setRange(0, 100)
        self.intensity_slider.setValue(100)
        self.intensity_slider.valueChanged.connect(self._on_intensity_changed)
        intensity_row.addWidget(self.intensity_slider, 1)
        self.intensity_value = QLabel("100%")
        intensity_row.addWidget(self.intensity_value)
        side.addLayout(intensity_row)

        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setMinimumWidth(200)
        side_widget.setMaximumWidth(280)
        body.addWidget(side_widget, 1)

        layout.addLayout(body, 1)
        self.status_label = QLabel(
            "Idle. Click Refresh to list UVC devices, then Start."
        )
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------- devices

    def refresh_devices(self) -> None:
        """Probe UVC devices and repopulate the device combo."""

        if self._capture is not None:
            return
        previous = self.device_combo.currentData()
        try:
            devices = list_uvc_devices()
        except Exception:  # pragma: no cover - defensive UI guard
            devices = []

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for label in devices:
            try:
                index = int(label.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                index = None
            self.device_combo.addItem(label, index)
        if previous is not None:
            match = self.device_combo.findData(previous)
            if match >= 0:
                self.device_combo.setCurrentIndex(match)
        self.device_combo.blockSignals(False)

        if devices:
            self.status_label.setText(f"Found {len(devices)} UVC device(s).")
        else:
            self.status_label.setText(
                "No UVC devices found. Check the camera connection."
            )
        self._update_controls()

    def _on_device_changed(self, _index: int = 0) -> None:
        self._update_controls()

    def _update_controls(self) -> None:
        running = self._capture is not None
        has_device = self.device_combo.currentData() is not None
        self.start_button.setEnabled(has_device and not running)
        self.stop_button.setEnabled(running)
        self.device_combo.setEnabled(not running)
        self.refresh_button.setEnabled(not running)

    def _open_capture(self, index: int):
        """Open a UVC index; returns None instead of raising."""

        try:
            import cv2
        except Exception:
            return None
        backend = getattr(cv2, "CAP_DSHOW", None) if sys.platform == "win32" else None
        capture = None
        try:
            if backend is not None:
                capture = cv2.VideoCapture(index, backend)
            else:
                capture = cv2.VideoCapture(index)
        except Exception:
            capture = None
        if capture is not None:
            try:
                if capture.isOpened():
                    return capture
            except Exception:
                pass
            try:
                capture.release()
            except Exception:
                pass
        return None

    # -------------------------------------------------------------- capture

    def start_preview(self) -> None:
        if self._capture is not None:
            return
        index = self.device_combo.currentData()
        if index is None:
            self.status_label.setText(
                "No UVC device selected. Click Refresh first."
            )
            return

        capture = self._open_capture(int(index))
        if capture is None:
            self.status_label.setText(
                f"Could not open Camera {index}. Check the USB connection."
            )
            self._update_controls()
            return

        self._capture = capture
        self._device_label = self.device_combo.currentText()
        self._read_failures = 0
        self._fps_estimate = None
        self._last_tick = None
        self._timer.start()
        self._update_controls()
        self.status_label.setText(f"Previewing {self._device_label}...")

    def stop_preview(self) -> None:
        was_running = self._capture is not None
        self._timer.stop()
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        self._last_raw_frame = None
        self._last_tick = None
        if was_running:
            self.status_label.setText("Stopped.")
        self._update_controls()

    def _grab_frame(self) -> None:
        capture = self._capture
        if capture is None:
            self._timer.stop()
            return

        try:
            ok, frame = capture.read()
        except Exception:
            ok, frame = False, None

        if not ok or frame is None:
            self._read_failures += 1
            if self._read_failures >= MAX_READ_FAILURES:
                device = self._device_label or "Camera"
                self.stop_preview()
                self.status_label.setText(
                    f"{device} disconnected or stopped sending frames."
                )
            return

        self._read_failures = 0
        now = time.monotonic()
        if self._last_tick is not None:
            delta = now - self._last_tick
            if delta > 0:
                instant = 1.0 / delta
                if self._fps_estimate is None:
                    self._fps_estimate = instant
                else:
                    self._fps_estimate = (
                        0.85 * self._fps_estimate + 0.15 * instant
                    )
        self._last_tick = now
        self.show_frame(frame)

    # --------------------------------------------------------------- frames

    def show_frame(self, frame_bgr: np.ndarray) -> None:
        """Process and display a BGR frame (also used by tests)."""

        frame = np.asarray(frame_bgr)
        if frame.ndim not in (2, 3):
            raise ValueError("frame must be a 2D or 3D image array")
        self._last_raw_frame = frame
        display, histogram = self._render(frame)
        self.histogram.set_histogram(histogram)
        self._set_preview_image(display)
        self._update_status(frame)

    def _render(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        base = np.asarray(frame)
        if base.ndim == 3 and base.shape[2] > 3:
            base = base[..., :3]

        if self.lut_toggle.isChecked() and base.ndim == 3:
            lut = self._demo_lut()
            intensity = self.intensity_slider.value() / 100.0
            base = apply_lut(base, lut, intensity)
            base = np.clip(base * 255.0, 0.0, 255.0).astype(np.uint8)

        histogram = luma_histogram(base)
        if self.false_color_toggle.isChecked():
            display = false_color(base)
        else:
            display = base

        if display.dtype != np.uint8:
            display = np.clip(display, 0.0, 255.0).astype(np.uint8)
        return np.ascontiguousarray(display), histogram

    def _set_preview_image(self, display: np.ndarray) -> None:
        if display.ndim == 2:
            image = QImage(
                display.data,
                display.shape[1],
                display.shape[0],
                display.strides[0],
                QImage.Format.Format_Grayscale8,
            ).copy()
        else:
            rgb = np.ascontiguousarray(display[..., ::-1])
            image = QImage(
                rgb.data,
                rgb.shape[1],
                rgb.shape[0],
                rgb.strides[0],
                QImage.Format.Format_RGB888,
            ).copy()

        self._last_image = image
        self._update_preview_pixmap()

    def _update_preview_pixmap(self) -> None:
        if self._last_image is None:
            return
        target = self.preview_label.size()
        if target.width() <= 1 or target.height() <= 1:
            return
        pixmap = QPixmap.fromImage(self._last_image).scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pixmap)

    def _update_status(self, frame: np.ndarray) -> None:
        height, width = frame.shape[:2]
        device = self._device_label or self.device_combo.currentText() or "preview"
        fps = (
            f"{self._fps_estimate:.0f}"
            if self._fps_estimate is not None
            else "--"
        )
        self.status_label.setText(f"{width}x{height} | {fps} fps | {device}")

    # --------------------------------------------------------------- options

    def _demo_lut(self) -> np.ndarray:
        """A small 17^3 look used until a .cube file picker exists."""

        if self._demo_lut_cache is None:
            size = 17
            axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
            blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")

            def curve(values: np.ndarray) -> np.ndarray:
                # Mild smoothstep contrast.
                return 0.7 * values + 0.3 * values * values * (3.0 - 2.0 * values)

            red_out = np.clip(curve(red) * 1.02, 0.0, 1.0)
            green_out = curve(green)
            blue_out = np.clip(curve(blue) * 0.98, 0.0, 1.0)
            self._demo_lut_cache = np.stack(
                (red_out, green_out, blue_out), axis=-1
            ).astype(np.float32)
        return self._demo_lut_cache

    def _on_display_option_changed(self, _checked: bool = False) -> None:
        frame = self._last_raw_frame
        if frame is None:
            return
        display, histogram = self._render(frame)
        self.histogram.set_histogram(histogram)
        self._set_preview_image(display)

    def _on_intensity_changed(self, value: int) -> None:
        self.intensity_value.setText(f"{int(value)}%")
        if self._last_raw_frame is not None:
            self._on_display_option_changed()

    # ---------------------------------------------------------------- events

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_preview_pixmap()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.stop_preview()
        super().closeEvent(event)
