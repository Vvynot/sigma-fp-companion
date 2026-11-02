"""Read-only model of a SIGMA fp dark frame.

A :class:`DarkFrame` holds the raw CFA plane (``uint16``), its dimensions and
the exposure metadata that matters for calibration.  Loading never rewrites
the source DNG.

EXIF is read with ``exifread`` when it is installed.  When it is not, or when
a tag is missing, metadata falls back to what ``rawpy`` can report (decoded
dimensions, black/white levels and, on rawpy builds that expose it, the
``RawPy.metadata`` object).  Temperature is not part of the DNG baseline, so
it is ``None`` unless a camera or maker-note tag provides it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

RAW_SUFFIXES = frozenset({".dng"})

_ISO_TAGS = (
    "EXIF ISOSpeedRatings",
    "EXIF PhotographicSensitivity",
    "EXIF ISOSpeed",
)
_EXPOSURE_TAGS = ("EXIF ExposureTime", "EXIF ShutterSpeedValue")
_DATE_TAGS = ("EXIF DateTimeOriginal", "Image DateTime")
_WIDTH_TAGS = ("EXIF ExifImageWidth", "Image ImageWidth")
_HEIGHT_TAGS = ("EXIF ExifImageLength", "Image ImageLength")
_TEMPERATURE_TAGS = (
    "EXIF Temperature",
    "Temperature",
    "EXIF AmbientTemperature",
    "EXIF SensorTemperature",
    "MakerNote AmbientTemperature",
    "MakerNote SensorTemperature",
    "MakerNote Temperature",
)


def _import_rawpy(*, required: bool = False):
    """Import rawpy lazily so the numeric core works without it."""
    try:
        import rawpy  # type: ignore[import-not-found]
    except ImportError as exc:
        if required:
            raise RuntimeError(
                "rawpy is required to read DNG files; install rawpy"
            ) from exc
        return None
    return rawpy


def _tag_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("ascii", "replace")
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    """Convert an EXIF rational/number (or its string form) to float.

    exifread returns ISO as a list and rational tags as ``Ratio`` objects.
    A list/tuple is treated as a value list (first element wins) rather than
    a numerator/denominator pair.
    """
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)

    num = getattr(value, "num", None)
    den = getattr(value, "den", None)
    if num is not None and den is not None:
        return None if den == 0 else float(num) / float(den)

    num = getattr(value, "numerator", None)
    den = getattr(value, "denominator", None)
    if num is not None and den is not None:
        return None if den == 0 else float(num) / float(den)

    if isinstance(value, (tuple, list)):
        if not value:
            return None
        return _to_float(value[0])

    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("ascii", "replace")

    text = str(value).strip()
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            denominator = float(right)
            return None if denominator == 0 else float(left) / denominator
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    try:
        return int(round(number))
    except (OverflowError, ValueError):
        return None


def json_default(value: Any) -> Any:
    """JSON fallback for numpy scalars/arrays, paths and frames."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, DarkFrame):
        return value.metadata()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def is_raw_path(path: str | Path) -> bool:
    """Return True for suffixes this package loads (``.DNG``, case-insensitive)."""
    return Path(path).suffix.lower() in RAW_SUFFIXES


@dataclass
class DarkFrame:
    """One raw dark frame and its acquisition metadata.

    ``raw`` is a 2-D ``uint16`` CFA plane.  The width/height fields are filled
    from the array when omitted and are validated when supplied.
    """

    path: Path
    raw: np.ndarray
    width: int = 0
    height: int = 0
    iso: int | None = None
    exposure_s: float | None = None
    temperature_c: float | None = None
    black_level: float = 0.0
    white_level: int = 65535

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        array = np.asarray(self.raw)
        if array.ndim != 2:
            raise ValueError(
                f"{self.path}: raw must be a 2-D array, got shape {array.shape}"
            )
        if array.dtype != np.uint16:
            array = np.clip(
                np.rint(np.asarray(array, dtype=np.float64)), 0, 65535
            ).astype(np.uint16)
        self.raw = array

        height, width = array.shape
        if self.height and int(self.height) != height:
            raise ValueError(
                f"{self.path}: height {self.height} does not match raw "
                f"array height {height}"
            )
        if self.width and int(self.width) != width:
            raise ValueError(
                f"{self.path}: width {self.width} does not match raw "
                f"array width {width}"
            )
        self.height = int(height)
        self.width = int(width)

        if self.iso is not None:
            self.iso = _to_int(self.iso)
        if self.exposure_s is not None:
            self.exposure_s = _to_float(self.exposure_s)
        if self.temperature_c is not None:
            self.temperature_c = _to_float(self.temperature_c)

    @classmethod
    def from_path(
        cls, path: str | Path, *, metadata: Mapping[str, Any] | None = None
    ) -> "DarkFrame":
        """Load one DNG through rawpy without modifying the source file."""
        target = Path(path)
        rawpy = _import_rawpy(required=True)

        with rawpy.imread(str(target)) as handle:
            image = getattr(handle, "raw_image_visible", None)
            if image is None:
                image = getattr(handle, "raw_image", None)
            if image is None:
                raise ValueError(
                    f"{target}: rawpy did not expose a raw image plane"
                )
            # Always copy: raw_image is a view into rawpy-owned memory.
            array = np.array(image, dtype=np.uint16, copy=True)
            if array.ndim != 2:
                raise ValueError(
                    f"{target}: raw plane must be 2-D, got shape {array.shape}"
                )
            info = (
                dict(metadata)
                if metadata is not None
                else read_metadata(target, raw=handle, decode=False)
            )

        height, width = array.shape
        return cls(
            path=target,
            raw=array,
            width=width,
            height=height,
            iso=info.get("iso"),
            exposure_s=info.get("exposure_s"),
            temperature_c=info.get("temperature_c"),
            black_level=float(info.get("black_level") or 0.0),
            white_level=int(info.get("white_level") or 65535),
        )

    def metadata(self) -> dict[str, Any]:
        """Return the JSON-safe metadata (never the pixel array)."""
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "iso": self.iso,
            "exposure_s": self.exposure_s,
            "temperature_c": self.temperature_c,
            "black_level": self.black_level,
            "white_level": self.white_level,
        }


def read_exif_tags(path: str | Path) -> dict[str, Any] | None:
    """Return exifread tags, ``None`` if exifread is absent, ``{}`` on error."""
    try:
        import exifread  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        with Path(path).open("rb") as handle:
            return exifread.process_file(handle, details=False)
    except Exception:
        # A corrupt or non-EXIF file is reported as an empty tag set.
        return {}


def metadata_from_exif_tags(tags: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the calibration-relevant fields from an exifread tag mapping."""
    if not tags:
        return {}

    def first(names: tuple[str, ...]) -> Any:
        for name in names:
            value = tags.get(name)
            if value is not None:
                return value
        return None

    meta: dict[str, Any] = {}
    iso = _to_int(first(_ISO_TAGS))
    exposure = _to_float(first(_EXPOSURE_TAGS))
    temperature = _to_float(first(_TEMPERATURE_TAGS))
    width = _to_int(first(_WIDTH_TAGS))
    height = _to_int(first(_HEIGHT_TAGS))
    date = _tag_text(first(_DATE_TAGS))

    if iso is not None:
        meta["iso"] = iso
    if exposure is not None:
        meta["exposure_s"] = exposure
    if temperature is not None:
        meta["temperature_c"] = temperature
    if width is not None:
        meta["width"] = width
    if height is not None:
        meta["height"] = height
    if date is not None:
        meta["date"] = date
    return meta


def metadata_from_rawpy(raw: Any) -> dict[str, Any]:
    """Extract dimensions and levels from an open rawpy handle.

    Older rawpy builds have no ``metadata`` attribute; whatever is missing
    simply stays out of the result, letting EXIF values win.
    """
    info: dict[str, Any] = {}

    sizes = getattr(raw, "sizes", None)
    if sizes is not None:
        width = getattr(sizes, "width", None)
        height = getattr(sizes, "height", None)
        if width:
            info["width"] = int(width)
        if height:
            info["height"] = int(height)

    levels = getattr(raw, "black_level_per_channel", None)
    if levels is not None:
        try:
            info["black_level"] = float(
                np.mean(np.asarray(levels, dtype=np.float64))
            )
        except (TypeError, ValueError):
            pass

    white = getattr(raw, "white_level", None)
    if white is not None:
        try:
            info["white_level"] = int(white)
        except (TypeError, ValueError):
            pass

    metadata = getattr(raw, "metadata", None)
    if metadata is not None:
        iso = getattr(metadata, "iso_speed", None)
        if iso is None:
            iso = getattr(metadata, "iso", None)
        exposure = getattr(metadata, "shutter", None)
        if exposure is None:
            exposure = getattr(metadata, "exposure_time", None)
        temperature = getattr(metadata, "temperature", None)
        if iso is not None:
            info["iso"] = _to_int(iso)
        if exposure is not None:
            info["exposure_s"] = _to_float(exposure)
        if temperature is not None:
            info["temperature_c"] = _to_float(temperature)

    return {key: value for key, value in info.items() if value is not None}


def merge_metadata(
    exif_meta: Mapping[str, Any], raw_meta: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge EXIF and rawpy metadata; decoded dimensions/levels are authoritative."""
    merged = dict(exif_meta)
    for key, value in raw_meta.items():
        if value is None:
            continue
        if key in ("width", "height", "black_level", "white_level"):
            merged[key] = value
        elif merged.get(key) is None:
            merged[key] = value
    return merged


def read_metadata(
    path: str | Path,
    *,
    tags: Mapping[str, Any] | None = None,
    raw: Any = None,
    decode: bool = True,
) -> dict[str, Any]:
    """Read calibration metadata for ``path``.

    ``tags`` lets callers supply an exifread mapping (useful for tests).
    ``raw`` is an already-open rawpy handle.  With ``decode=False`` rawpy is
    not opened at all, so only EXIF values are returned.
    """
    target = Path(path)

    if tags is None:
        tags = read_exif_tags(target)
        if tags is None:
            exif_state = "unavailable"
            tags = {}
        else:
            exif_state = "ok" if tags else "empty"
    else:
        exif_state = "provided"

    exif_meta = metadata_from_exif_tags(tags)
    raw_meta: dict[str, Any] = {}
    raw_state = False
    decode_error: str | None = None

    if raw is not None:
        raw_meta = metadata_from_rawpy(raw)
        raw_state = True
    elif decode:
        rawpy = _import_rawpy()
        if rawpy is not None:
            try:
                with rawpy.imread(str(target)) as handle:
                    raw_meta = metadata_from_rawpy(handle)
                raw_state = True
            except Exception as exc:  # rawpy raises many exception types
                decode_error = str(exc)

    merged = merge_metadata(exif_meta, raw_meta)
    merged["path"] = str(target)
    merged["exifread"] = exif_state
    merged["rawpy"] = raw_state
    if decode_error is not None:
        merged["decode_error"] = decode_error
    return merged
