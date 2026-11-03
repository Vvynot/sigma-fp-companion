"""Generate a dark-frame acquisition plan for long exposures on the fp.

The plan is data: a JSON document and/or a Markdown checklist listing every
ISO x exposure x temperature target and the camera settings that keep the
frames usable as calibration data.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_ISOS = (100, 800, 3200)
DEFAULT_EXPOSURES_S = (40.0, 120.0, 300.0, 500.0)
DEFAULT_TEMPERATURES_C = (20.0,)
DEFAULT_FRAMES_PER_TARGET = 2
SCHEMA = "fp-dark-plan/1"

CAMERA_SETTINGS: tuple[dict[str, str], ...] = (
    {
        "setting": "Exposure mode",
        "value": "M (manual)",
        "why": "locks ISO and shutter speed to each target",
    },
    {
        "setting": "Lens",
        "value": "Body cap or lens cap on; cover the viewfinder",
        "why": "no light may reach the sensor during a dark frame",
    },
    {
        "setting": "Long-exposure noise reduction",
        "value": "OFF",
        "why": "in-camera dark subtraction would alter the raw dark frame",
    },
    {
        "setting": "File format",
        "value": "DNG (RAW) only",
        "why": "this tool reads one 16-bit CFA plane per frame",
    },
    {
        "setting": "ISO and shutter speed",
        "value": "Per target below",
        "why": "the master is only valid at matching acquisition settings",
    },
    {
        "setting": "Temperature",
        "value": "Shoot all frames in one session at the same ambient temperature",
        "why": "dark current roughly doubles every 6-8 C, so drift invalidates the master",
    },
    {
        "setting": "Stabilisation",
        "value": "OFF; camera on a solid surface or tripod",
        "why": "removes vibration and accidental movement",
    },
    {
        "setting": "Between frames",
        "value": "Wait about one exposure interval",
        "why": "lets the sensor return to its baseline temperature",
    },
    {
        "setting": "Power and storage",
        "value": "Full battery or dummy power; free card space",
        "why": "no interruption during 40-500 s exposures",
    },
)

CHECKLIST: tuple[str, ...] = (
    "Charge the battery or connect dummy power; free enough card space for every target.",
    "Mount the camera on a solid surface and fit the body cap (or cap the lens).",
    "Set exposure mode to M (manual).",
    "Set the file format to DNG (RAW).",
    "Turn long-exposure noise reduction OFF.",
    "Turn image stabilisation OFF and cover the viewfinder/eyepiece.",
    "Let the camera settle to the target ambient temperature before starting.",
    "Wait about one exposure interval between frames so the sensor cools back to baseline.",
    "Record the ambient or sensor temperature next to each file.",
    "Shoot every frame listed under Targets; do not review or erase mid-session.",
)


def _positive_ints(values: Iterable[Any], name: str) -> list[int]:
    result: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain integers") from exc
        if number <= 0:
            raise ValueError(f"{name} values must be greater than zero")
        result.append(number)
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _positive_floats(values: Iterable[Any], name: str) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain numbers") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} values must be positive and finite")
        result.append(number)
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _finite_floats(values: Iterable[Any], name: str) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain numbers") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} values must be finite")
        result.append(number)
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _unique(values: Sequence[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def build_plan(
    *,
    isos: Iterable[int] = DEFAULT_ISOS,
    exposures_s: Iterable[float] = DEFAULT_EXPOSURES_S,
    temperatures_c: Iterable[float] = DEFAULT_TEMPERATURES_C,
    frames_per_target: int = DEFAULT_FRAMES_PER_TARGET,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serialisable acquisition plan.

    Defaults cover the community's usual long-exposure range: ISO 100/800/
    3200, 40/120/300/500 s, two frames per target, at 20 C.
    """
    iso_values = _unique(_positive_ints(isos, "isos"))
    exposure_values = _unique(_positive_floats(exposures_s, "exposures_s"))
    temperature_values = _unique(
        _finite_floats(temperatures_c, "temperatures_c")
    )
    try:
        frame_count = int(frames_per_target)
    except (TypeError, ValueError) as exc:
        raise ValueError("frames_per_target must be an integer") from exc
    if frame_count < 1:
        raise ValueError("frames_per_target must be at least 1")

    targets: list[dict[str, Any]] = []
    for temperature in temperature_values:
        for iso in iso_values:
            for exposure in exposure_values:
                for index in range(1, frame_count + 1):
                    targets.append(
                        {
                            "id": (
                                f"iso{iso}_exp{exposure:g}s_t{temperature:g}c_"
                                f"f{index:02d}"
                            ),
                            "iso": iso,
                            "exposure_s": float(exposure),
                            "temperature_c": float(temperature),
                            "frame_index": index,
                            "frames_per_target": frame_count,
                            "filename": (
                                f"dark_iso{iso}_{exposure:g}s_"
                                f"{temperature:g}C_{index:02d}.DNG"
                            ),
                        }
                    )

    group_count = (
        len(iso_values) * len(exposure_values) * len(temperature_values)
    )
    return {
        "schema": SCHEMA,
        "tool": "fp_companion.dark",
        "kind": "plan",
        "title": title or "SIGMA fp dark-frame acquisition plan",
        "camera": "SIGMA fp",
        "isos": iso_values,
        "exposures_s": exposure_values,
        "temperatures_c": temperature_values,
        "frames_per_target": frame_count,
        "counts": {
            "group_targets": group_count,
            "frames_per_target": frame_count,
            "targets": len(targets),
        },
        "camera_settings": [dict(item) for item in CAMERA_SETTINGS],
        "checklist": list(CHECKLIST),
        "targets": targets,
    }


def plan_to_markdown(plan: dict[str, Any]) -> str:
    """Render a plan as a Markdown checklist with a target table."""
    counts = plan.get("counts", {})
    lines: list[str] = [
        f"# {plan.get('title', 'Dark-frame acquisition plan')}",
        "",
        f"- Camera: {plan.get('camera', 'SIGMA fp')}",
        (
            f"- Targets: {counts.get('group_targets', 0)} "
            f"(ISO x exposure x temperature), "
            f"{counts.get('targets', 0)} frames total "
            f"({counts.get('frames_per_target', 0)} per target)"
        ),
        (
            f"- ISO: {', '.join(str(value) for value in plan.get('isos', []))}"
        ),
        (
            "- Exposures (s): "
            + ", ".join(
                f"{value:g}" for value in plan.get("exposures_s", [])
            )
        ),
        (
            "- Temperatures (C): "
            + ", ".join(
                f"{value:g}" for value in plan.get("temperatures_c", [])
            )
        ),
        "",
        "## Camera settings",
        "",
        "| Setting | Value | Why |",
        "| --- | --- | --- |",
    ]
    for item in plan.get("camera_settings", []):
        lines.append(
            f"| {item.get('setting', '')} | {item.get('value', '')} | "
            f"{item.get('why', '')} |"
        )

    lines.extend(["", "## Checklist", ""])
    for item in plan.get("checklist", []):
        lines.append(f"- [ ] {item}")

    lines.extend(
        [
            "",
            "## Targets",
            "",
            "| # | ISO | Exposure (s) | Temperature (C) | Frame | File |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for number, target in enumerate(plan.get("targets", []), 1):
        lines.append(
            f"| {number} | {target.get('iso')} | "
            f"{target.get('exposure_s', 0):g} | "
            f"{target.get('temperature_c', 0):g} | "
            f"{target.get('frame_index')}/{target.get('frames_per_target')} | "
            f"`{target.get('filename', '')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_plan(plan: dict[str, Any], out_path: str | Path) -> Path:
    """Write the plan as JSON or Markdown, inferred from the file suffix."""
    target = Path(out_path)
    suffix = target.suffix.lower()
    if suffix in (".md", ".markdown"):
        text = plan_to_markdown(plan)
    elif suffix in ("", ".json"):
        if not suffix:
            target = target.with_name(target.name + ".json")
        text = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    else:
        raise ValueError(
            f"unsupported plan format {suffix!r}; use .json, .md or .markdown"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
