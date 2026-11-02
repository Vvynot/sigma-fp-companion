"""Hardware-in-the-loop test harness for SIGMA fp SUP cards.

The package is split into four small pieces:

* :mod:`fp_companion.autotest.session` -- the USB transport (FPSH v1).
* :mod:`fp_companion.autotest.plan` -- suite loading/validation.
* :mod:`fp_companion.autotest.steps` -- one step executor.
* :mod:`fp_companion.autotest.runner` -- suite orchestration and reports.
"""

from __future__ import annotations

from .plan import VALID_STEP_KINDS, Step, Suite, load_suite
from .runner import RunResult, StepResult, run_suite
from .session import CameraSession, ShellError
from .steps import execute

__all__ = [
    "VALID_STEP_KINDS",
    "CameraSession",
    "RunResult",
    "ShellError",
    "Step",
    "StepResult",
    "Suite",
    "execute",
    "load_suite",
    "run_suite",
]
