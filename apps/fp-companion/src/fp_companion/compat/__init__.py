"""OFFLINE SUP compatibility/dependency solver.

Reads the same artifacts the camera reads (``fpSup/*.BIN`` headers,
``MANIFEST.sha256``, ``build.json`` and ``LOAD.LOG``) and answers whether the
sups on a card can be combined by a given loader.  It never opens the camera,
never writes an SD card and never patches a sup.
"""

from __future__ import annotations

from .binparse import parse_sup_bytes, parse_sup_file
from .graph import SupGraph, build_graph, family_for_loader, graph_from_loadlog
from .loadlog import (
    COMMON_CODES,
    V3_CODES,
    V4_CODES,
    LoadEvent,
    detect_family,
    event_summary,
    parse_loadlog,
    parse_loadlog_file,
    resolve_code,
)
from .model import (
    FORMAT_FSB1,
    FORMAT_FSB4,
    FORMAT_UNKNOWN,
    KIND_NAMES,
    KIND_UNKNOWN,
    SL_CHAIN,
    SL_EXCL,
    SL_SHARED_UI,
    Claim,
    Issue,
    SupInfo,
    kind_from,
    kind_name,
    sup_id_text,
)
from .solver import DEFAULT_LOADER_VERSION, CompatResult, can_combine

__all__ = [
    "COMMON_CODES",
    "DEFAULT_LOADER_VERSION",
    "FORMAT_FSB1",
    "FORMAT_FSB4",
    "FORMAT_UNKNOWN",
    "KIND_NAMES",
    "KIND_UNKNOWN",
    "SL_CHAIN",
    "SL_EXCL",
    "SL_SHARED_UI",
    "V3_CODES",
    "V4_CODES",
    "Claim",
    "CompatResult",
    "Issue",
    "LoadEvent",
    "SupGraph",
    "SupInfo",
    "build_graph",
    "can_combine",
    "detect_family",
    "event_summary",
    "family_for_loader",
    "graph_from_loadlog",
    "kind_from",
    "kind_name",
    "parse_loadlog",
    "parse_loadlog_file",
    "parse_sup_bytes",
    "parse_sup_file",
    "resolve_code",
    "sup_id_text",
]
