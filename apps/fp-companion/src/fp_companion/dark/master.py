"""Master dark frames, hot-pixel maps and sensor profile summaries.

Everything here works on plain ``numpy`` arrays as well as on
:class:`~fp_companion.dark.model.DarkFrame` objects, so the numeric core is
testable without rawpy or a camera.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .model import DarkFrame, json_default

DEFAULT_BLOCK = 64
DEFAULT_THRESHOLD_SIGMA = 6.0


def _frame_array(item: Any) -> np.ndarray:
    array = item.raw if isinstance(item, DarkFrame) else np.asarray(item)
    if array.ndim != 2:
        raise ValueError(
            f"each frame must be a 2-D array or DarkFrame, got shape {array.shape}"
        )
    return array


def _frame_list(frames: Any) -> list[Any]:
    """Normalise one frame, a list/tuple of frames, or an (N,H,W) stack."""
    if isinstance(frames, DarkFrame):
        return [frames]
    if isinstance(frames, np.ndarray):
        if frames.ndim == 2:
            return [frames]
        if frames.ndim == 3:
            return list(frames)
        raise ValueError(
            f"a frame stack must be 3-D (N,H,W), got shape {frames.shape}"
        )
    try:
        return list(frames)
    except TypeError as exc:
        raise ValueError("frames must be a DarkFrame, an array or an iterable") from exc


def _stack(frames: Any) -> np.ndarray:
    items = _frame_list(frames)
    if not items:
        raise ValueError("at least one frame is required")

    arrays = [_frame_array(item) for item in items]
    shape = arrays[0].shape
    for index, array in enumerate(arrays):
        if array.shape != shape:
            raise ValueError(
                f"frame {index} has shape {array.shape}, expected {shape}"
            )
    return np.stack([array.astype(np.float32) for array in arrays], axis=0)


def build_master(frames: Any) -> np.ndarray:
    """Median-stack ``frames`` into a ``float32`` master dark frame.

    The median rejects cosmic rays, hot-pixel transients and occasional
    readout glitches better than a mean while keeping fixed hot pixels.
    """
    return np.median(_stack(frames), axis=0).astype(np.float32)


def _block_baseline(image: np.ndarray, block: int) -> np.ndarray:
    """Per-pixel baseline from non-overlapping block medians.

    A block median follows smooth amp glow and vignetting, so the hot-pixel
    test compares each photosite against its local neighbourhood instead of a
    single whole-sensor threshold.
    """
    height, width = image.shape
    if block <= 1:
        return image.astype(np.float32)

    baseline = np.empty((height, width), dtype=np.float32)
    for y0 in range(0, height, block):
        y1 = min(height, y0 + block)
        for x0 in range(0, width, block):
            x1 = min(width, x0 + block)
            baseline[y0:y1, x0:x1] = np.median(image[y0:y1, x0:x1])
    return baseline


@dataclass
class HotPixelMap:
    """Sparse hot/stuck-pixel map derived from repeated dark frames."""

    mask: np.ndarray
    values: list[tuple[int, int, int]] = field(default_factory=list)
    count: int = 0
    total_pixels: int = 0
    threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA
    threshold_value: float = 0.0
    noise_sigma: float = 0.0
    stack_depth: int = 1

    @property
    def fraction(self) -> float:
        if not self.total_pixels:
            return 0.0
        return self.count / self.total_pixels

    def to_dict(self, *, limit: int | None = None) -> dict[str, Any]:
        pixels = self.values if limit is None else self.values[:limit]
        return {
            "count": self.count,
            "total_pixels": self.total_pixels,
            "fraction": self.fraction,
            "threshold_sigma": self.threshold_sigma,
            "threshold_value": self.threshold_value,
            "noise_sigma": self.noise_sigma,
            "stack_depth": self.stack_depth,
            "pixels": [
                {"x": x, "y": y, "value": value} for x, y, value in pixels
            ],
            "truncated": limit is not None and limit < self.count,
        }


def hot_pixel_map(
    frames: Any,
    threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA,
    *,
    block: int = DEFAULT_BLOCK,
) -> HotPixelMap:
    """Find pixels that stand ``threshold_sigma`` above their local baseline.

    A robust (median/MAD) noise estimate is made from the residual between
    the median stack and block-median baseline.  Returned values are the
    median-stack level at each hot pixel, as ``(x, y, value)``.
    """
    if not np.isfinite(threshold_sigma) or threshold_sigma <= 0:
        raise ValueError("threshold_sigma must be a positive, finite number")
    if block < 1:
        raise ValueError("block must be at least 1")

    stack = _stack(frames)
    master = np.median(stack, axis=0).astype(np.float32)
    residual = master - _block_baseline(master, int(block))

    centre = float(np.median(residual))
    mad = float(np.median(np.abs(residual - centre)))
    noise = 1.4826 * mad
    if noise <= 0:
        noise = float(np.std(residual))
    if noise <= 0:
        noise = 1.0

    threshold = float(centre + threshold_sigma * noise)
    mask = residual > threshold
    ys, xs = np.nonzero(mask)
    values = [
        (int(x), int(y), int(round(float(master[y, x]))))
        for y, x in zip(ys, xs)
    ]

    return HotPixelMap(
        mask=mask,
        values=values,
        count=int(mask.sum()),
        total_pixels=int(mask.size),
        threshold_sigma=float(threshold_sigma),
        threshold_value=threshold,
        noise_sigma=noise,
        stack_depth=int(stack.shape[0]),
    )


def _as_2d(image: Any) -> np.ndarray:
    array = image.raw if isinstance(image, DarkFrame) else np.asarray(image)
    if array.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {array.shape}")
    return array


def amp_glow_profile(
    master: Any, *, edge_fraction: float = 0.1
) -> dict[str, Any]:
    """Summarise amp glow with row and column medians.

    ``row_medians``/``column_medians`` are full-length lists (row-major
    profile of the median stack).  Peaks and edge/centre values are included
    so a report can describe glow without embedding every sample twice.
    """
    if not 0 < edge_fraction <= 0.5:
        raise ValueError("edge_fraction must be in (0, 0.5]")

    image = _as_2d(master).astype(np.float32)
    height, width = image.shape
    row_medians = np.median(image, axis=1).astype(np.float64)
    column_medians = np.median(image, axis=0).astype(np.float64)

    def peak(profile: np.ndarray) -> dict[str, Any]:
        index = int(np.argmax(profile))
        return {"index": index, "value": float(profile[index])}

    edge_rows = max(1, int(round(height * edge_fraction)))
    edge_columns = max(1, int(round(width * edge_fraction)))
    top = float(np.median(row_medians[:edge_rows]))
    bottom = float(np.median(row_medians[-edge_rows:]))
    left = float(np.median(column_medians[:edge_columns]))
    right = float(np.median(column_medians[-edge_columns:]))
    edge_median = float(np.median([top, bottom, left, right]))

    centre_rows = row_medians[edge_rows : height - edge_rows] if height > 2 * edge_rows else row_medians
    centre_columns = (
        column_medians[edge_columns : width - edge_columns]
        if width > 2 * edge_columns
        else column_medians
    )
    centre_median = float(np.median([np.median(centre_rows), np.median(centre_columns)]))
    ratio = None if centre_median == 0 else edge_median / centre_median

    return {
        "height": int(height),
        "width": int(width),
        "row_medians": row_medians.tolist(),
        "column_medians": column_medians.tolist(),
        "row_peak": peak(row_medians),
        "column_peak": peak(column_medians),
        "edges": {
            "top": top,
            "bottom": bottom,
            "left": left,
            "right": right,
            "median": edge_median,
        },
        "centre_median": centre_median,
        "edge_to_centre_ratio": ratio,
    }


def _frame_items(frames: Any) -> list[tuple[np.ndarray, dict[str, Any]]]:
    items: list[tuple[np.ndarray, dict[str, Any]]] = []
    for item in _frame_list(frames):
        if isinstance(item, DarkFrame):
            meta = {
                "iso": item.iso,
                "exposure_s": item.exposure_s,
                "temperature_c": item.temperature_c,
                "black_level": float(item.black_level),
                "white_level": int(item.white_level),
            }
            array = item.raw
        else:
            array = _frame_array(item)
            meta = {
                "iso": None,
                "exposure_s": None,
                "temperature_c": None,
                "black_level": None,
                "white_level": None,
            }
        items.append((array, meta))
    if not items:
        raise ValueError("at least one frame is required")
    return items


def _clean_mean(values: Sequence[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else None


def black_level_curve(frames: Any) -> dict[str, Any]:
    """Per-frame and per-(ISO, exposure) dark-level summary.

    The observed level is the median of each raw plane, which is robust to
    isolated hot pixels.  Grouping exposes offset drift across ISO and
    exposure so it can be compared with the camera's declared black level.
    """
    rows: list[dict[str, Any]] = []
    for index, (array, meta) in enumerate(_frame_items(frames)):
        data = array.astype(np.float32)
        rows.append(
            {
                "index": index,
                "iso": meta["iso"],
                "exposure_s": meta["exposure_s"],
                "temperature_c": meta["temperature_c"],
                "width": int(data.shape[1]),
                "height": int(data.shape[0]),
                "observed_black_level": float(np.median(data)),
                "mean": float(np.mean(data)),
                "std": float(np.std(data)),
                "declared_black_level": meta["black_level"],
                "white_level": meta["white_level"],
            }
        )

    grouped: dict[tuple[int | None, float | None], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["iso"], row["exposure_s"]), []).append(row)

    def sort_key(key: tuple[int | None, float | None]) -> tuple[Any, ...]:
        iso, exposure = key
        return (
            iso is None,
            iso if iso is not None else 0,
            exposure is None,
            exposure if exposure is not None else 0.0,
        )

    groups: list[dict[str, Any]] = []
    for key in sorted(grouped, key=sort_key):
        members = grouped[key]
        observed = [member["observed_black_level"] for member in members]
        declared = [member["declared_black_level"] for member in members]
        temperatures = [member["temperature_c"] for member in members]
        groups.append(
            {
                "iso": key[0],
                "exposure_s": key[1],
                "count": len(members),
                "observed_black_level": float(np.median(observed)),
                "observed_min": float(np.min(observed)),
                "observed_max": float(np.max(observed)),
                "declared_black_level": _clean_mean(declared),
                "temperature_c": _clean_mean(temperatures),
            }
        )

    return {
        "frame_count": len(rows),
        "groups": groups,
        "frames": rows,
    }


def save_master(
    path: str | Path,
    master: Any,
    hot_map: Any = None,
    *,
    metadata: dict[str, Any] | None = None,
    extra: dict[str, np.ndarray] | None = None,
) -> Path:
    """Write a compressed ``.npz`` with the master frame and optional mask.

    ``hot_map`` may be a :class:`HotPixelMap` or a boolean mask.  The returned
    path is the file actually written; ``.npz`` is appended when missing.
    """
    master_array = np.asarray(master, dtype=np.float32)
    if master_array.ndim != 2:
        raise ValueError(f"master must be 2-D, got shape {master_array.shape}")

    target = Path(path)
    if target.suffix.lower() != ".npz":
        target = target.with_name(target.name + ".npz")

    payload: dict[str, Any] = {
        "master": master_array,
        "metadata_json": np.array(
            json.dumps(metadata or {}, default=json_default, sort_keys=True)
        ),
    }

    if hot_map is not None:
        if isinstance(hot_map, HotPixelMap):
            mask = hot_map.mask
            values = np.asarray(hot_map.values, dtype=np.int64)
            if values.size == 0:
                values = np.empty((0, 3), dtype=np.int64)
            payload["hot_values"] = values.reshape(-1, 3)
            payload["hot_stats_json"] = np.array(
                json.dumps(
                    hot_map.to_dict(limit=0), default=json_default, sort_keys=True
                )
            )
        else:
            mask = np.asarray(hot_map)
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != master_array.shape:
            raise ValueError(
                f"hot-pixel mask shape {mask.shape} does not match master "
                f"shape {master_array.shape}"
            )
        payload["hot_mask"] = mask

    for key, value in (extra or {}).items():
        payload[key] = np.asarray(value)

    target.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(target, **payload)
    return target


def load_master(path: str | Path) -> dict[str, Any]:
    """Load a master ``.npz`` written by :func:`save_master`."""
    target = Path(path)
    with np.load(target, allow_pickle=False) as data:
        if "master" not in data:
            raise ValueError(f"{target}: no 'master' array in archive")
        master = np.asarray(data["master"], dtype=np.float32)
        mask = (
            np.asarray(data["hot_mask"], dtype=bool)
            if "hot_mask" in data
            else None
        )
        values = (
            np.asarray(data["hot_values"], dtype=np.int64).reshape(-1, 3)
            if "hot_values" in data
            else np.empty((0, 3), dtype=np.int64)
        )
        metadata = (
            json.loads(str(data["metadata_json"]))
            if "metadata_json" in data
            else {}
        )
        stats = (
            json.loads(str(data["hot_stats_json"]))
            if "hot_stats_json" in data
            else {}
        )

    return {
        "master": master,
        "hot_mask": mask,
        "hot_values": values,
        "metadata": metadata,
        "hot_stats": stats,
    }
