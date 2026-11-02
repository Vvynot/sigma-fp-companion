"""Dark-frame and hot-pixel calibration for long-exposure SIGMA fp DNGs.

The package is split into five small pieces:

* :mod:`fp_companion.dark.model` -- read-only DNG/model metadata.
* :mod:`fp_companion.dark.master` -- median stack, hot-pixel map, profiles.
* :mod:`fp_companion.dark.correct` -- subtraction, repair and 16-bit export.
* :mod:`fp_companion.dark.plan` -- acquisition plan (JSON/Markdown).
* :mod:`fp_companion.dark.cli` -- the ``python -m fp_companion.dark`` commands.

The numeric core only needs numpy; rawpy is imported lazily for file loads and
Pillow for image export.
"""

from __future__ import annotations

from .correct import (
    correct_frame,
    repair_hot_pixels,
    subtract_master,
    write_image,
)
from .master import (
    DEFAULT_THRESHOLD_SIGMA,
    HotPixelMap,
    amp_glow_profile,
    black_level_curve,
    build_master,
    hot_pixel_map,
    load_master,
    save_master,
)
from .model import DarkFrame, read_metadata
from .plan import build_plan, plan_to_markdown, write_plan

__all__ = [
    "DEFAULT_THRESHOLD_SIGMA",
    "DarkFrame",
    "HotPixelMap",
    "amp_glow_profile",
    "black_level_curve",
    "build_master",
    "build_plan",
    "correct_frame",
    "hot_pixel_map",
    "load_master",
    "plan_to_markdown",
    "read_metadata",
    "repair_hot_pixels",
    "save_master",
    "subtract_master",
    "write_image",
    "write_plan",
]
