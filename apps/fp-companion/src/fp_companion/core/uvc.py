"""Camera-free UVC helpers: device probing, LUTs, false color, scopes.

Everything in this module is pure numpy except :func:`list_uvc_devices`, which
probes OpenCV video device indices but never raises.  No function here opens a
camera for streaming; probing opens an index only long enough to check it.
"""

from __future__ import annotations

import sys

import numpy as np

__all__ = [
    "list_uvc_devices",
    "apply_lut",
    "false_color",
    "luma_histogram",
    "waveform",
]

# Rec. 601 luma weights in BGR order.
_LUMA_WEIGHTS_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float32)

# Six false-color bands, dark to bright.  Values are BGR swatches:
# purple, blue, cyan, green, yellow, red.
_FALSE_COLOR_BANDS = np.array(
    [
        (255, 0, 128),  # purple
        (255, 0, 0),  # blue
        (255, 255, 0),  # cyan
        (0, 255, 0),  # green
        (0, 255, 255),  # yellow
        (0, 0, 255),  # red
    ],
    dtype=np.uint8,
)

# 256-entry lookup so a luma byte maps directly to a band colour.
_FALSE_COLOR_LUT = np.repeat(
    _FALSE_COLOR_BANDS, (43, 43, 43, 43, 42, 42), axis=0
)


def list_uvc_devices(max_index: int = 10) -> list[str]:
    """Return labels such as ``"Camera 0"`` for OpenCV devices that open.

    Indices ``0 .. max_index - 1`` are probed briefly.  Any failure (OpenCV
    missing, backend error, or no device) is swallowed, so the result is always
    a list and this is safe to call from the UI.
    """

    try:
        import cv2
    except Exception:
        return []

    devices: list[str] = []
    backend = getattr(cv2, "CAP_DSHOW", 0) if sys.platform == "win32" else 0
    for index in range(max_index):
        capture = None
        try:
            if backend:
                capture = cv2.VideoCapture(index, backend)
            else:
                capture = cv2.VideoCapture(index)
            if capture is not None and capture.isOpened():
                devices.append(f"Camera {index}")
        except Exception:
            continue
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
    return devices


def _as_float01(frame: np.ndarray) -> np.ndarray:
    """Convert an integer image to [0, 1] float32; floats pass through."""

    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        return arr.astype(np.float32) / float(info.max)
    return arr.astype(np.float32, copy=False)


def apply_lut(
    frame_bgr: np.ndarray,
    lut: np.ndarray,
    intensity: float = 1.0,
) -> np.ndarray:
    """Apply a 3D LUT to a BGR frame with trilinear interpolation.

    ``lut`` must be an array shaped ``(size, size, size, 3)`` with axes
    ``(blue, green, red)`` inside the grid and RGB values in ``[0, 1]``.  This
    matches ``np.reshape`` of a standard ``.cube`` table (red varies fastest).
    Grid sizes 17, 33 and 65 are typical; any size >= 1 is accepted.

    ``frame_bgr`` may be uint8 (0..255) or float (0..1).  ``intensity`` blends
    the LUT result with the original frame.  The result is float32 BGR in
    ``[0, 1]``.
    """

    frame = np.asarray(frame_bgr)
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError("frame_bgr must have shape (height, width, 3)")

    palette = np.asarray(lut, dtype=np.float32)
    if palette.ndim != 4 or palette.shape[3] != 3:
        raise ValueError("lut must have shape (size, size, size, 3)")
    if not (palette.shape[0] == palette.shape[1] == palette.shape[2]):
        raise ValueError("lut grid must be cubic")
    size = palette.shape[0]
    if size < 1:
        raise ValueError("lut grid must not be empty")

    strength = float(np.clip(intensity, 0.0, 1.0))
    rgb = _as_float01(frame[..., :3])[..., ::-1]  # BGR -> RGB

    if strength == 0.0:
        return np.ascontiguousarray(rgb[..., ::-1], dtype=np.float32)

    if size == 1:
        sampled = np.broadcast_to(palette[0, 0, 0], rgb.shape).astype(np.float32)
    else:
        # Coordinates are in grid units, ordered R, G, B (matching ``rgb``).
        coords = np.clip(rgb, 0.0, 1.0) * float(size - 1)
        red_f = coords[..., 0]
        green_f = coords[..., 1]
        blue_f = coords[..., 2]

        red0 = np.clip(np.floor(red_f), 0, size - 2).astype(np.intp)
        green0 = np.clip(np.floor(green_f), 0, size - 2).astype(np.intp)
        blue0 = np.clip(np.floor(blue_f), 0, size - 2).astype(np.intp)
        red1 = red0 + 1
        green1 = green0 + 1
        blue1 = blue0 + 1

        fr = (red_f - red0)[..., None]
        fg = (green_f - green0)[..., None]
        fb = (blue_f - blue0)[..., None]

        # LUT axes are (blue, green, red); values are RGB.
        c000 = palette[blue0, green0, red0]
        c001 = palette[blue0, green0, red1]
        c010 = palette[blue0, green1, red0]
        c011 = palette[blue0, green1, red1]
        c100 = palette[blue1, green0, red0]
        c101 = palette[blue1, green0, red1]
        c110 = palette[blue1, green1, red0]
        c111 = palette[blue1, green1, red1]

        c00 = c000 + (c001 - c000) * fr
        c01 = c010 + (c011 - c010) * fr
        c10 = c100 + (c101 - c100) * fr
        c11 = c110 + (c111 - c110) * fr
        c0 = c00 + (c01 - c00) * fg
        c1 = c10 + (c11 - c10) * fg
        sampled = c0 + (c1 - c0) * fb

    result = rgb * (1.0 - strength) + sampled * strength
    result = np.clip(result, 0.0, 1.0)
    return np.ascontiguousarray(result[..., ::-1], dtype=np.float32)


def _luma(frame_bgr: np.ndarray) -> np.ndarray:
    """Return Rec. 601 luma in 0..255 as float32."""

    arr = np.asarray(frame_bgr)
    if arr.ndim == 2:
        if arr.dtype == np.uint8:
            return arr.astype(np.float32)
        if np.issubdtype(arr.dtype, np.integer):
            return _as_float01(arr) * 255.0
        return np.clip(arr.astype(np.float32), 0.0, 1.0) * 255.0
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(
            "frame_bgr must have shape (height, width, 3) or (height, width)"
        )
    bgr = _as_float01(arr[..., :3])
    return np.clip(bgr @ _LUMA_WEIGHTS_BGR, 0.0, 1.0) * 255.0


def false_color(frame_bgr: np.ndarray) -> np.ndarray:
    """Map luma to six false-color bands and return a BGR uint8 image."""

    luma = np.clip(_luma(frame_bgr), 0.0, 255.0)
    index = luma.astype(np.uint8)
    return _FALSE_COLOR_LUT[index].copy()


def luma_histogram(frame_bgr: np.ndarray, bins: int = 256) -> np.ndarray:
    """Return a luma histogram whose counts sum to the number of pixels."""

    bins = int(bins)
    if bins <= 0:
        raise ValueError("bins must be positive")
    values = np.clip(_luma(frame_bgr), 0.0, 255.0).ravel()
    if values.size == 0:
        return np.zeros(bins, dtype=np.int64)
    counts, _ = np.histogram(values, bins=bins, range=(0.0, 256.0))
    return counts.astype(np.int64)


def waveform(
    frame_bgr: np.ndarray,
    width: int = 256,
    height: int = 128,
) -> np.ndarray:
    """Build a luma waveform image (uint8, shape ``(height, width)``).

    The horizontal axis follows the source image columns scaled to ``width``;
    the vertical axis is luma (bright values at the top).  Pixel counts are
    accumulated into bins and normalised to the busiest bin.
    """

    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    luma = np.clip(_luma(frame_bgr), 0.0, 255.0)
    if luma.ndim != 2:
        luma = luma.reshape(luma.shape[0], -1)
    src_h, src_w = luma.shape
    if src_h == 0 or src_w == 0:
        return np.zeros((height, width), dtype=np.uint8)

    # Bright pixels land at the top row.
    rows = height - 1 - np.clip(
        (luma / 255.0 * float(height - 1)).astype(np.int32), 0, height - 1
    )
    cols = np.clip((np.arange(src_w) * width) // src_w, 0, width - 1)
    cols = np.broadcast_to(cols, (src_h, src_w))

    flat = rows.astype(np.int64).ravel() * width + cols.astype(np.int64).ravel()
    accum = np.bincount(flat, minlength=height * width).reshape(height, width)

    peak = int(accum.max())
    if peak > 0:
        accum = (accum.astype(np.float32) / float(peak) * 255.0)
    return accum.astype(np.uint8)
