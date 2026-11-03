"""Windows-first hot reload for LExpo and the fpSup loader v3.

The public surface is small::

    from fp_companion.hotload import Hotload
    from fp_companion.hotload.link import open_link

    with open_link("auto") as link:
        hotload = Hotload(link)
        print(hotload.probe())

``probe``/``snapshot`` are read-only.  ``release``, ``load``, ``reload`` and
``cycle``/``stress`` write memory through the shell and run resident loader
code; callers must decide that this is safe before invoking them.
"""

from __future__ import annotations

from .core import (
    LAYOUTS,
    LXPO_ID,
    SL_BOOK_AT,
    BookError,
    CallDidNotReturn,
    EntryError,
    Hotload,
    HotloadError,
    Layout,
    RefuseError,
    TransportError,
    UsageError,
    VerifyError,
)

__all__ = [
    "Hotload",
    "HotloadError",
    "UsageError",
    "TransportError",
    "VerifyError",
    "CallDidNotReturn",
    "BookError",
    "EntryError",
    "RefuseError",
    "Layout",
    "LAYOUTS",
    "SL_BOOK_AT",
    "LXPO_ID",
]
