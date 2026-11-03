"""Command-line interface for dark-frame / hot-pixel calibration.

Usage::

    python -m fp_companion.dark build <dir> --out <dir> [--recursive]
                                              [--threshold-sigma N] [--json]
    python -m fp_companion.dark correct <dng> --master <npz> --out <tiff>
                                              [--report <json>] [--no-repair]
                                              [--json]
    python -m fp_companion.dark plan --out <json|md> [--iso N] [--exposure S]
                                              [--temperature C] [--frames N]
                                              [--json]
    python -m fp_companion.dark inspect <dng> [--json]

Exit codes are ``0`` (success), ``1`` (processing failure) and ``2`` (usage
or missing input).  Nothing here operates the camera, writes an SD card or
modifies a source DNG.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .correct import IMAGE_SUFFIXES, correct_frame
from .master import (
    DEFAULT_BLOCK,
    DEFAULT_THRESHOLD_SIGMA,
    amp_glow_profile,
    black_level_curve,
    build_master,
    hot_pixel_map,
    load_master,
    save_master,
)
from .model import (
    RAW_SUFFIXES,
    DarkFrame,
    json_default,
    read_metadata,
)
from .plan import DEFAULT_FRAMES_PER_TARGET, build_plan, write_plan

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MASTER_FILENAME = "master.npz"
BUILD_REPORT_FILENAME = "build-report.json"
HOT_PIXELS_IN_REPORT = 50


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return value


def _finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def _sorted_unique(values: Sequence[Any]) -> list[Any]:
    return sorted({value for value in values if value is not None})


def _collect_dngs(directory: Path, recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in RAW_SUFFIXES
    )


def _load_frame(path: Path) -> DarkFrame:
    """Indirection point so tests can supply synthetic frames."""
    return DarkFrame.from_path(path)


# -- build ---------------------------------------------------------------


def _build_metadata(
    frames: list[DarkFrame], master: Any, args: argparse.Namespace
) -> dict[str, Any]:
    return {
        "tool": "fp_companion.dark",
        "kind": "master",
        "frame_count": len(frames),
        "sources": [str(frame.path) for frame in frames],
        "width": int(master.shape[1]),
        "height": int(master.shape[0]),
        "isos": _sorted_unique([frame.iso for frame in frames]),
        "exposures_s": _sorted_unique(
            [frame.exposure_s for frame in frames]
        ),
        "temperatures_c": _sorted_unique(
            [frame.temperature_c for frame in frames]
        ),
        "threshold_sigma": args.threshold_sigma,
        "block": args.block,
        "master_median": float(np.median(master)),
    }


def _cmd_build(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"dark: not a directory: {directory}", file=sys.stderr)
        return EXIT_USAGE

    try:
        paths = _collect_dngs(directory, args.recursive)
    except OSError as exc:
        print(f"dark: cannot scan {directory}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if not paths:
        print(f"dark: no .DNG files found in {directory}", file=sys.stderr)
        return EXIT_FAILURE

    frames: list[DarkFrame] = []
    for path in paths:
        try:
            frames.append(_load_frame(path))
        except Exception as exc:
            print(f"dark: cannot read {path}: {exc}", file=sys.stderr)
            return EXIT_FAILURE

    try:
        master = build_master(frames)
        hot = hot_pixel_map(
            frames, threshold_sigma=args.threshold_sigma, block=args.block
        )
        glow = amp_glow_profile(master)
        curve = black_level_curve(frames)
    except (ValueError, RuntimeError) as exc:
        print(f"dark: calibration failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    out_dir = Path(args.out)
    metadata = _build_metadata(frames, master, args)

    try:
        master_path = save_master(
            out_dir / MASTER_FILENAME,
            master,
            hot,
            metadata=metadata,
            extra={
                "row_medians": glow["row_medians"],
                "column_medians": glow["column_medians"],
            },
        )
    except (OSError, ValueError) as exc:
        print(f"dark: cannot write master: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    report: dict[str, Any] = {
        **metadata,
        "master": {
            "path": str(master_path),
            "width": int(master.shape[1]),
            "height": int(master.shape[0]),
            "median": float(np.median(master)),
            "mean": float(np.mean(master)),
            "min": float(np.min(master)),
            "max": float(np.max(master)),
        },
        "hot_pixels": hot.to_dict(limit=HOT_PIXELS_IN_REPORT),
        "amp_glow": {
            "row_peak": glow["row_peak"],
            "column_peak": glow["column_peak"],
            "edges": glow["edges"],
            "centre_median": glow["centre_median"],
            "edge_to_centre_ratio": glow["edge_to_centre_ratio"],
        },
        "black_level_curve": curve,
    }

    report_path = out_dir / BUILD_REPORT_FILENAME
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"dark: cannot write report: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.json:
        print(json.dumps(report, indent=2, default=json_default))
    else:
        print(f"dark: {len(frames)} frame(s) -> {master_path}")
        print(
            f"dark: hot pixels: {hot.count} / {hot.total_pixels} "
            f"({hot.fraction * 100:.4f}%)"
        )
        print(f"dark: report: {report_path}")
    return EXIT_OK


# -- correct -------------------------------------------------------------


def _cmd_correct(args: argparse.Namespace) -> int:
    source = Path(args.dng)
    master_path = Path(args.master)
    target = Path(args.out)

    if not source.is_file():
        print(f"dark: DNG not found: {source}", file=sys.stderr)
        return EXIT_USAGE
    if not master_path.is_file():
        print(f"dark: master not found: {master_path}", file=sys.stderr)
        return EXIT_USAGE
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        print(
            "dark: --out must end in .tif, .tiff or .png (DNG is not written)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        loaded = load_master(master_path)
    except Exception as exc:
        print(f"dark: cannot read master {master_path}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        report = correct_frame(
            source,
            loaded["master"],
            None if args.no_repair else loaded["hot_mask"],
            out_path=target,
            report_path=args.report,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"dark: correction failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.json:
        print(json.dumps(report, indent=2, default=json_default))
    else:
        print(f"dark: {source} -> {report['output']}")
        print(
            f"dark: {report['width']}x{report['height']}, "
            f"hot pixels repaired: {report['hot_pixels_repaired']}"
        )
        print(f"dark: report: {report['report']}")
    return EXIT_OK


# -- plan ----------------------------------------------------------------


def _cmd_plan(args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {}
    if args.isos:
        kwargs["isos"] = args.isos
    if args.exposures:
        kwargs["exposures_s"] = args.exposures
    if args.temperatures:
        kwargs["temperatures_c"] = args.temperatures
    if args.frames is not None:
        kwargs["frames_per_target"] = args.frames
    if args.title:
        kwargs["title"] = args.title

    try:
        plan = build_plan(**kwargs)
    except ValueError as exc:
        print(f"dark: cannot build plan: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        written = write_plan(plan, args.out)
    except (OSError, ValueError) as exc:
        print(f"dark: cannot write plan: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.json:
        print(json.dumps(plan, indent=2, default=json_default))
    else:
        counts = plan["counts"]
        print(
            f"dark: {counts['group_targets']} target(s), "
            f"{counts['targets']} frame(s) -> {written}"
        )
    return EXIT_OK


# -- inspect -------------------------------------------------------------


_METADATA_KEYS = (
    "width",
    "height",
    "iso",
    "exposure_s",
    "temperature_c",
    "black_level",
    "white_level",
)


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"dark: file not found: {path}", file=sys.stderr)
        return EXIT_USAGE

    try:
        meta = read_metadata(path)
    except Exception as exc:
        print(f"dark: cannot read metadata: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    present = any(meta.get(key) is not None for key in _METADATA_KEYS)

    if args.json:
        print(json.dumps(meta, indent=2, default=json_default))
    else:
        def show(label: str, value: Any, suffix: str = "") -> None:
            text = "unknown" if value is None else f"{value}{suffix}"
            print(f"{label:<16}: {text}")

        print(str(meta.get("path", path)))
        dimensions = (
            f"{meta['width']}x{meta['height']}"
            if meta.get("width") and meta.get("height")
            else None
        )
        show("Dimensions", dimensions)
        show("ISO", meta.get("iso"))
        show("Exposure", meta.get("exposure_s"), " s")
        show("Temperature", meta.get("temperature_c"), " C")
        show("Black level", meta.get("black_level"))
        show("White level", meta.get("white_level"))
        show("exifread", meta.get("exifread"))
        show("rawpy", "decoded" if meta.get("rawpy") else "not decoded")
        if meta.get("decode_error"):
            print(f"{'decode error':<16}: {meta['decode_error']}")

    if not present and meta.get("decode_error"):
        return EXIT_FAILURE
    return EXIT_OK


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fp_companion.dark",
        description=(
            "Build and apply long-exposure dark-frame and hot-pixel "
            "calibrations for SIGMA fp DNGs."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build", help="Median-stack a directory of dark DNGs into master.npz"
    )
    build_parser.add_argument("directory", help="Directory of dark-frame DNGs")
    build_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR",
        help="Output directory for master.npz and build-report.json",
    )
    build_parser.add_argument(
        "--threshold-sigma", type=_positive_float,
        default=DEFAULT_THRESHOLD_SIGMA, metavar="N",
        help=f"Hot-pixel detection threshold in robust sigma (default: {DEFAULT_THRESHOLD_SIGMA:g})",
    )
    build_parser.add_argument(
        "--block", type=_positive_int, default=DEFAULT_BLOCK, metavar="N",
        help=f"Local baseline block size in pixels (default: {DEFAULT_BLOCK})",
    )
    build_parser.add_argument(
        "--recursive", action="store_true", help="Scan subdirectories for DNGs"
    )
    build_parser.add_argument(
        "--json", action="store_true", help="Print the build report as JSON"
    )
    build_parser.set_defaults(func=_cmd_build)

    correct_parser = subparsers.add_parser(
        "correct", help="Subtract a master and repair hot pixels in one DNG"
    )
    correct_parser.add_argument("dng", help="Single DNG to correct")
    correct_parser.add_argument(
        "--master", required=True, type=Path, metavar="NPZ",
        help="master.npz written by 'build'",
    )
    correct_parser.add_argument(
        "--out", required=True, type=Path, metavar="IMAGE",
        help="Output 16-bit .tif/.tiff/.png (DNG is not written)",
    )
    correct_parser.add_argument(
        "--report", type=Path, default=None, metavar="JSON",
        help="Report path (default: alongside the output image)",
    )
    correct_parser.add_argument(
        "--no-repair", action="store_true",
        help="Skip hot-pixel repair even if the master contains a mask",
    )
    correct_parser.add_argument(
        "--json", action="store_true", help="Print the correction report as JSON"
    )
    correct_parser.set_defaults(func=_cmd_correct)

    plan_parser = subparsers.add_parser(
        "plan", help="Generate a dark-frame acquisition plan (JSON or Markdown)"
    )
    plan_parser.add_argument(
        "--out", required=True, type=Path, metavar="FILE",
        help="Plan file; .json for JSON, .md/.markdown for a checklist",
    )
    plan_parser.add_argument(
        "--iso", dest="isos", action="append", type=_positive_int, metavar="N",
        help="ISO target (repeatable; default: 100, 800, 3200)",
    )
    plan_parser.add_argument(
        "--exposure", dest="exposures", action="append", type=_positive_float,
        metavar="S", help="Exposure in seconds (repeatable; default: 40, 120, 300, 500)",
    )
    plan_parser.add_argument(
        "--temperature", dest="temperatures", action="append",
        type=_finite_float, metavar="C",
        help="Temperature target in C (repeatable; default: 20)",
    )
    plan_parser.add_argument(
        "--frames", type=_positive_int, default=None, metavar="N",
        help=f"Frames per target (default: {DEFAULT_FRAMES_PER_TARGET})",
    )
    plan_parser.add_argument(
        "--title", default=None, help="Plan title"
    )
    plan_parser.add_argument(
        "--json", action="store_true", help="Print the plan JSON to stdout"
    )
    plan_parser.set_defaults(func=_cmd_plan)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Read EXIF/rawpy metadata without modifying the file"
    )
    inspect_parser.add_argument("path", help="DNG to inspect")
    inspect_parser.add_argument(
        "--json", action="store_true", help="Print metadata as JSON"
    )
    inspect_parser.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
