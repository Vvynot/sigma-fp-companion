from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from fp_companion.core import uvc


def identity_lut(size: int) -> np.ndarray:
    """A ``.cube``-ordered identity LUT: ``lut[b, g, r] == (r, g, b)``."""

    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack((red, green, blue), axis=-1)


@pytest.mark.parametrize("size", [2, 17, 33, 65])
def test_identity_lut_preserves_frame(size: int) -> None:
    frame = np.array(
        [
            [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            [[0.25, 0.5, 0.75], [0.1, 0.9, 0.4]],
        ],
        dtype=np.float32,
    )

    out = uvc.apply_lut(frame, identity_lut(size))

    assert out.dtype == np.float32
    assert out.shape == frame.shape
    np.testing.assert_allclose(out, frame, atol=1e-5)


def test_apply_lut_zero_intensity_returns_original() -> None:
    frame = np.random.default_rng(3).random((6, 7, 3), dtype=np.float32)
    lut = np.flip(identity_lut(5), axis=-1)  # deliberately not identity

    out = uvc.apply_lut(frame, lut, intensity=0.0)

    np.testing.assert_allclose(out, frame, atol=1e-6)


def test_apply_lut_keeps_bgr_channel_order() -> None:
    # LUT swaps R and B, so a BGR input should come out with R and B swapped.
    axis = np.linspace(0.0, 1.0, 2, dtype=np.float32)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    lut = np.stack((blue, green, red), axis=-1)
    frame = np.array([[[0.2, 0.4, 0.6]]], dtype=np.float32)  # B, G, R

    out = uvc.apply_lut(frame, lut)

    np.testing.assert_allclose(out, [[[0.6, 0.4, 0.2]]], atol=1e-5)


def test_apply_lut_accepts_uint8_and_returns_float32() -> None:
    frame = np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8)

    out = uvc.apply_lut(frame, identity_lut(17))

    assert out.dtype == np.float32
    np.testing.assert_allclose(
        out, frame.astype(np.float32) / 255.0, atol=1e-5
    )


def test_apply_lut_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        uvc.apply_lut(np.zeros((4, 4), dtype=np.float32), identity_lut(2))
    with pytest.raises(ValueError):
        uvc.apply_lut(np.zeros((4, 4, 3), dtype=np.float32), np.zeros((2, 2, 3)))


def test_false_color_shape_dtype_and_bands() -> None:
    frame = np.zeros((4, 5, 3), dtype=np.uint8)

    out = uvc.false_color(frame)

    assert out.shape == (4, 5, 3)
    assert out.dtype == np.uint8
    # Shadows are purple; highlights are red (swatches stored as BGR).
    assert tuple(out[0, 0]) == (255, 0, 128)

    mid_gray = np.full((1, 1, 3), 150, dtype=np.uint8)
    assert tuple(uvc.false_color(mid_gray)[0, 0]) == (0, 255, 0)

    white = np.full((1, 1, 3), 255, dtype=np.uint8)
    assert tuple(uvc.false_color(white)[0, 0]) == (0, 0, 255)


def test_luma_histogram_counts_every_pixel() -> None:
    frame = np.random.default_rng(7).integers(
        0, 256, size=(10, 12, 3), dtype=np.uint8
    )

    histogram = uvc.luma_histogram(frame)

    assert histogram.shape == (256,)
    assert int(histogram.sum()) == 10 * 12

    coarse = uvc.luma_histogram(frame, bins=64)
    assert coarse.shape == (64,)
    assert int(coarse.sum()) == 10 * 12


def test_waveform_shape_dtype_and_content() -> None:
    flat = np.zeros((32, 32, 3), dtype=np.uint8)
    empty = uvc.waveform(flat, width=64, height=48)
    assert empty.shape == (48, 64)
    assert empty.dtype == np.uint8

    gradient = np.zeros((32, 32, 3), dtype=np.uint8)
    gradient[:, :, :] = np.linspace(0, 255, 32, dtype=np.uint8)[None, :, None]
    trace = uvc.waveform(gradient, width=64, height=48)
    assert trace.shape == (48, 64)
    assert trace.dtype == np.uint8
    assert int(trace.max()) > 0


def test_list_uvc_devices_labels_open_indices(monkeypatch) -> None:
    fake_cv2 = types.ModuleType("cv2")
    fake_cv2.CAP_DSHOW = 700

    class FakeCapture:
        def __init__(self, index, *args) -> None:
            self.index = index
            self.released = False

        def isOpened(self) -> bool:
            return self.index == 2

        def release(self) -> None:
            self.released = True

    fake_cv2.VideoCapture = FakeCapture
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    assert uvc.list_uvc_devices() == ["Camera 2"]


def test_list_uvc_devices_without_opencv(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)

    assert uvc.list_uvc_devices() == []


def test_list_uvc_devices_swallows_backend_errors(monkeypatch) -> None:
    fake_cv2 = types.ModuleType("cv2")
    fake_cv2.CAP_DSHOW = 700

    def raise_error(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    fake_cv2.VideoCapture = raise_error
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    assert uvc.list_uvc_devices() == []
