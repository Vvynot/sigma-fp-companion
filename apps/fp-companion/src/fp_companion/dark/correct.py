"""Apply dark-frame calibration and export a corrected image.

The corrected output is a 16-bit grayscale TIFF or PNG (the raw CFA plane
preserved, not demosaiced).  DNG is deliberately not written: producing a
valid DNG safely needs tag/IFD surgery that is out of scope for this first
version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .model import DarkFrame, json_default

IMAGE_SUFFIXES = frozenset({".tif", ".tiff", ".png"})
DEFAULT_REPAIR_RADIUS = 1
MAX_UINT16 = 65535


def _as_2d(value: Any, name: str) -> np.ndarray:
    array = value.raw if isinstance(value, DarkFrame) else np.asarray(value)
    if array.ndim != 2:
        raise ValueError(f"{name} must be 2-D, got shape {array.shape}")
    return array


def subtract_master(
    raw: Any,
    master: Any,
    *,
    clip_min: int = 0,
    clip_max: int = MAX_UINT16,
) -> np.ndarray:
    """Subtract ``master`` from ``raw``, rounding and clipping to uint16.

    The master carries the fixed black offset (plus dark current at the
    master's exposure), so this removes the bias directly.  Negative results
    are clipped to ``clip_min`` rather than wrapped.
    """
    raw_array = _as_2d(raw, "raw")
    master_array = np.asarray(master, dtype=np.float32)
    if master_array.ndim != 2:
        raise ValueError(
            f"master must be 2-D, got shape {master_array.shape}"
        )
    if master_array.shape != raw_array.shape:
        raise ValueError(
            f"master shape {master_array.shape} does not match raw shape "
            f"{raw_array.shape}"
        )

    difference = raw_array.astype(np.float32) - master_array
    return np.clip(
        np.rint(difference), clip_min, clip_max
    ).astype(np.uint16)


def repair_hot_pixels(
    raw: Any, mask: Any, *, radius: int = DEFAULT_REPAIR_RADIUS
) -> np.ndarray:
    """Replace masked pixels with the median of their unmasked neighbours.

    ``radius=1`` is a 3x3 neighbourhood; masked neighbours are skipped so a
    small cluster is filled from its clean rim.  The input is not modified.
    """
    if radius < 1:
        raise ValueError("radius must be at least 1")

    array = _as_2d(raw, "raw")
    if array.dtype != np.uint16:
        array = np.clip(
            np.rint(np.asarray(array, dtype=np.float64)), 0, MAX_UINT16
        ).astype(np.uint16)
    else:
        array = array.copy()

    mask_array = np.asarray(mask, dtype=bool)
    if mask_array.ndim != 2 or mask_array.shape != array.shape:
        raise ValueError(
            f"hot-pixel mask shape {mask_array.shape} does not match raw "
            f"shape {array.shape}"
        )

    height, width = array.shape
    ys, xs = np.nonzero(mask_array)
    for y, x in zip(ys.tolist(), xs.tolist()):
        y0 = max(0, y - radius)
        y1 = min(height, y + radius + 1)
        x0 = max(0, x - radius)
        x1 = min(width, x + radius + 1)
        window = array[y0:y1, x0:x1]
        window_mask = mask_array[y0:y1, x0:x1]
        good = window[~window_mask]
        if good.size:
            array[y, x] = np.rint(np.median(good)).astype(np.uint16)
    return array


def write_image(path: str | Path, array: Any) -> Path:
    """Write a 2-D uint16 array as an uncompressed 16-bit TIFF or PNG."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(
            f"unsupported output format {suffix!r}; use .tif, .tiff or .png"
        )

    data = _as_2d(array, "image")
    if data.dtype != np.uint16:
        data = np.clip(
            np.rint(np.asarray(data, dtype=np.float64)), 0, MAX_UINT16
        ).astype(np.uint16)

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required to export TIFF/PNG images"
        ) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    # Pillow 13 rejects an explicit mode for uint16 input but infers I;16.
    Image.fromarray(np.ascontiguousarray(data)).save(str(target))
    return target


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    return target


def correct_frame(
    path: str | Path,
    master: Any,
    mask: Any = None,
    out_path: str | Path | None = None,
    *,
    report_path: str | Path | None = None,
    radius: int = DEFAULT_REPAIR_RADIUS,
) -> dict[str, Any]:
    """Correct one dark/flat-light frame and export a 16-bit image + JSON report.

    Dimensions are preserved exactly.  ``mask`` may be ``None`` to skip hot
    pixel repair.  When ``out_path`` is omitted the corrected image lands
    beside the source with ``_corrected.tif`` appended to the stem.
    """
    source = Path(path)
    frame = DarkFrame.from_path(source)

    master_array = np.asarray(master, dtype=np.float32)
    if master_array.ndim != 2:
        raise ValueError(
            f"master must be 2-D, got shape {master_array.shape}"
        )
    if master_array.shape != frame.raw.shape:
        raise ValueError(
            f"master shape {master_array.shape} does not match frame shape "
            f"{frame.raw.shape}"
        )

    difference = frame.raw.astype(np.float32) - master_array
    clipped_low = int(np.count_nonzero(difference < 0))
    clipped_high = int(np.count_nonzero(difference > MAX_UINT16))

    corrected = subtract_master(frame.raw, master_array)
    repaired = 0
    if mask is not None:
        mask_array = np.asarray(mask, dtype=bool)
        if mask_array.ndim != 2 or mask_array.shape != corrected.shape:
            raise ValueError(
                f"hot-pixel mask shape {mask_array.shape} does not match "
                f"frame shape {corrected.shape}"
            )
        repaired = int(mask_array.sum())
        corrected = repair_hot_pixels(corrected, mask_array, radius=radius)

    target = (
        Path(out_path)
        if out_path is not None
        else source.with_name(source.stem + "_corrected.tif")
    )
    report_target = (
        Path(report_path)
        if report_path is not None
        else target.with_suffix(".json")
    )

    write_image(target, corrected)

    report: dict[str, Any] = {
        "tool": "fp_companion.dark",
        "kind": "correct",
        "source": str(source),
        "output": str(target),
        "report": str(report_target),
        "format": target.suffix.lower(),
        "width": frame.width,
        "height": frame.height,
        "pixels": int(frame.raw.size),
        "iso": frame.iso,
        "exposure_s": frame.exposure_s,
        "temperature_c": frame.temperature_c,
        "black_level": frame.black_level,
        "white_level": frame.white_level,
        "master": {
            "shape": list(master_array.shape),
            "median": float(np.median(master_array)),
            "mean": float(np.mean(master_array)),
        },
        "hot_pixels_repaired": repaired,
        "hot_pixel_radius": radius if mask is not None else None,
        "clipped_low_pixels": clipped_low,
        "clipped_high_pixels": clipped_high,
        "note": (
            "Master subtraction removes the fixed offset. Residual dark "
            "current and amp glow remain when the frame's ISO, exposure or "
            "temperature differ from the master's."
        ),
    }
    write_report(report_target, report)
    return report
