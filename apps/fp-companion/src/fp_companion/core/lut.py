from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


def parse_cube(path: str | Path) -> CubeLUT:
    title = ""
    size_1d = 0
    size_3d = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    data: list[tuple[float, float, float]] = []

    for raw in Path(path).read_text(encoding="utf-8").splitlines():
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

    return CubeLUT(
        title=title,
        size_1d=size_1d,
        size_3d=size_3d,
        domain_min=domain_min,
        domain_max=domain_max,
        data=tuple(data),
    )
