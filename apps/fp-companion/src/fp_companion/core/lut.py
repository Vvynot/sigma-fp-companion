"""Strict ``.cube`` parsing, validation and LUT settings.

The parser keeps every authored value: 1D or 3D grid, domain and the raw
triples are preserved in :class:`CubeLUT`.  Validation is a separate step so a
file can still be inspected (and its problems shown) even when it is rejected
for a card.  Nothing in this module writes to a card or talks to a camera.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .models import ValidationIssue

# fpSup LUT / Resolve compatibility.  The in-camera LUT SUP product contract
# documents 33 as the interoperability size; 17 and 65 parse and validate with
# a warning, other sizes are rejected.
ALLOWED_3D_SIZES: tuple[int, ...] = (17, 33, 65)
PREFERRED_3D_SIZE = 33
MAX_1D_SIZE = 4096
MAX_CUBE_BYTES = 4 * 1024 * 1024

COLOR_SPACES: tuple[str, ...] = (
    "ACES",
    "Adobe RGB",
    "Blackmagic Design",
    "P3 D60",
    "P3 D65",
    "Rec.2020",
    "Rec.709",
    "sRGB",
)

BLACKMAGIC_DESIGN = "Blackmagic Design"
BLACKMAGIC_GAMMAS: tuple[str, ...] = (
    "Blackmagic Design Film",
    "Blackmagic Extended Video",
)
STANDARD_GAMMAS: tuple[str, ...] = (
    "Adobe RGB",
    "Gamma 2.4",
    "Gamma 2.6",
    "Linear",
    "Rec.709",
    "sRGB",
)
GAMMAS: tuple[str, ...] = STANDARD_GAMMAS + BLACKMAGIC_GAMMAS


class CubeFormatError(ValueError):
    pass


@dataclass(frozen=True)
class CubeLUT:
    title: str
    size_1d: int
    size_3d: int
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    data: tuple[tuple[float, float, float], ...]
    # Size of the originating file, in bytes.  Optional so callers that build
    # a CubeLUT by hand remain source compatible.  0 means "unknown".
    source_bytes: int = 0


@dataclass(frozen=True)
class LutSettings:
    """Input-side settings for one LUT, matching the fpSup product contract.

    The camera output is fixed to Rec.709; only the input color space, input
    gamma and blend intensity are user choices.
    """

    input_color_space: str
    input_gamma: str
    intensity: float = 1.0


def parse_cube(path: str | Path) -> CubeLUT:
    source = Path(path)
    title = ""
    size_1d = 0
    size_3d = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    data: list[tuple[float, float, float]] = []

    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, rest = line.partition(" ")
        if key == "TITLE":
            title = rest.strip().strip('"')
        elif key == "LUT_1D_SIZE":
            size_1d = int(rest)
        elif key == "LUT_3D_SIZE":
            size_3d = int(rest)
        elif key == "DOMAIN_MIN":
            domain_min = tuple(float(v) for v in rest.split())
        elif key == "DOMAIN_MAX":
            domain_max = tuple(float(v) for v in rest.split())
        else:
            parts = line.split()
            if len(parts) != 3:
                raise CubeFormatError(f"Expected 3 values, got: {raw!r}")
            data.append(tuple(float(v) for v in parts))

    if size_1d and size_3d:
        raise CubeFormatError("A .cube file cannot be both 1D and 3D.")
    if size_1d and len(data) != size_1d:
        raise CubeFormatError(f"Expected {size_1d} 1D entries, got {len(data)}.")
    if size_3d and len(data) != size_3d**3:
        raise CubeFormatError(f"Expected {size_3d ** 3} 3D entries, got {len(data)}.")
    if not size_1d and not size_3d:
        raise CubeFormatError("Missing LUT_1D_SIZE or LUT_3D_SIZE.")

    try:
        source_bytes = source.stat().st_size
    except OSError:
        source_bytes = 0

    return CubeLUT(
        title=title,
        size_1d=size_1d,
        size_3d=size_3d,
        domain_min=domain_min,
        domain_max=domain_max,
        data=tuple(data),
        source_bytes=source_bytes,
    )


def validate_cube(lut: CubeLUT) -> tuple[ValidationIssue, ...]:
    """Return all problems found in ``lut``.

    ``error`` issues mean the LUT must not be used; ``warning`` issues are
    compatibility or size notes that do not block a staging copy.
    """

    issues: list[ValidationIssue] = []

    if lut.size_1d and lut.size_3d:
        issues.append(
            ValidationIssue(
                "error",
                "grid",
                "A .cube file cannot be both 1D and 3D.",
            )
        )
    elif not lut.size_1d and not lut.size_3d:
        issues.append(
            ValidationIssue(
                "error",
                "grid",
                "Missing LUT_1D_SIZE or LUT_3D_SIZE.",
            )
        )
    elif lut.size_1d:
        if not 1 <= lut.size_1d <= MAX_1D_SIZE:
            issues.append(
                ValidationIssue(
                    "error",
                    "grid",
                    f"Unsupported 1D LUT size {lut.size_1d}; "
                    f"supported range is 1..{MAX_1D_SIZE}.",
                )
            )
    elif lut.size_3d not in ALLOWED_3D_SIZES:
        issues.append(
            ValidationIssue(
                "error",
                "grid",
                f"Unsupported 3D LUT size {lut.size_3d}; "
                f"supported sizes are "
                f"{', '.join(str(size) for size in ALLOWED_3D_SIZES)}.",
            )
        )

    if lut.size_3d and lut.size_3d != PREFERRED_3D_SIZE:
        issues.append(
            ValidationIssue(
                "warning",
                "size",
                f"3D LUT size {lut.size_3d} is not the "
                f"{PREFERRED_3D_SIZE} Resolve/LUT SUP compatibility size.",
            )
        )

    if not lut.data:
        issues.append(
            ValidationIssue("error", "empty", "LUT contains no data entries.")
        )
    else:
        if lut.size_1d:
            expected = lut.size_1d
        elif lut.size_3d > 0:
            expected = lut.size_3d**3
        else:
            expected = 0
        if expected and len(lut.data) != expected:
            issues.append(
                ValidationIssue(
                    "error",
                    "length",
                    f"Data length mismatch: expected {expected} entries, "
                    f"got {len(lut.data)}.",
                )
            )

        non_finite = sum(
            1
            for row in lut.data
            for value in row
            if not math.isfinite(value)
        )
        if non_finite:
            issues.append(
                ValidationIssue(
                    "error",
                    "nonfinite",
                    f"LUT contains {non_finite} non-finite value(s).",
                )
            )

    domain_min = tuple(lut.domain_min)
    domain_max = tuple(lut.domain_max)
    if len(domain_min) != 3 or len(domain_max) != 3:
        issues.append(
            ValidationIssue(
                "error",
                "domain",
                "DOMAIN_MIN and DOMAIN_MAX must each have exactly 3 values.",
            )
        )
    else:
        domain_values = (*domain_min, *domain_max)
        if any(not math.isfinite(value) for value in domain_values):
            issues.append(
                ValidationIssue(
                    "error",
                    "nonfinite",
                    "DOMAIN_MIN/DOMAIN_MAX contain a non-finite value.",
                )
            )
        else:
            for channel, (low, high) in enumerate(zip(domain_min, domain_max)):
                if low >= high:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "domain",
                            f"DOMAIN_MIN >= DOMAIN_MAX on channel "
                            f"{'RGB'[channel]}: {low} >= {high}.",
                        )
                    )

    if lut.source_bytes > MAX_CUBE_BYTES:
        issues.append(
            ValidationIssue(
                "warning",
                "filesize",
                f"LUT file is {lut.source_bytes} bytes; files larger than "
                f"{MAX_CUBE_BYTES} bytes (4 MiB) may not fit the fpSup LUT "
                "storage.",
            )
        )

    return tuple(issues)


def validate_settings(settings: LutSettings) -> tuple[ValidationIssue, ...]:
    """Validate an input color space / gamma / intensity combination."""

    issues: list[ValidationIssue] = []

    color_space_known = settings.input_color_space in COLOR_SPACES
    gamma_known = settings.input_gamma in GAMMAS

    if not color_space_known:
        issues.append(
            ValidationIssue(
                "error",
                "color_space",
                f"Unsupported input color space: {settings.input_color_space!r}. "
                f"Supported values: {', '.join(COLOR_SPACES)}.",
            )
        )

    if not gamma_known:
        issues.append(
            ValidationIssue(
                "error",
                "gamma",
                f"Unsupported input gamma: {settings.input_gamma!r}. "
                f"Supported values: {', '.join(GAMMAS)}.",
            )
        )

    if color_space_known and gamma_known:
        # The product contract keeps the Blackmagic gammas in their own group.
        if settings.input_color_space == BLACKMAGIC_DESIGN:
            allowed = BLACKMAGIC_GAMMAS
        else:
            allowed = STANDARD_GAMMAS
        if settings.input_gamma not in allowed:
            issues.append(
                ValidationIssue(
                    "error",
                    "gamma_group",
                    f"Gamma {settings.input_gamma!r} is not available for "
                    f"input color space {settings.input_color_space!r}.",
                )
            )

    intensity = settings.intensity
    if (
        not isinstance(intensity, (int, float))
        or not math.isfinite(intensity)
        or not 0.0 <= intensity <= 1.0
    ):
        issues.append(
            ValidationIssue(
                "error",
                "intensity",
                f"LUT intensity must be between 0.0 and 1.0, got "
                f"{intensity!r}.",
            )
        )

    return tuple(issues)
