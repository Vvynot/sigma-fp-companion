"""Build a :class:`SupGraph` from a card directory and/or a LOAD.LOG.

Inputs understood (all optional except the files themselves):

* ``fpSup/*.BIN`` -- top-level files, exactly what the loaders list.  The
  loader itself (``LOADER.BIN``) is not a sup and is skipped.  Subdirectories
  (``fpSup/UI``) are not listed by either loader.
* ``MANIFEST.sha256`` -- ``sha256  ./path`` lines; hashes and missing files are
  reported, and paths that escape the card directory are refused.
* ``build.json`` -- the builders' metadata.  Its ``patch``/``claims`` entries
  are the only claim *declarations* available offline (see the note below).
* ``LOAD.LOG`` -- parsed into events and folded onto the matching sups.

Important, verified limitation: an ``FSB1``/``FSB4`` header has no claim
table.  A sup declares claims only at run time via ``svc->claim``.  So
"declared claims" here means claims declared by build metadata, and the
declaration source is recorded on each :class:`Claim` (``build.json:patch``).
Everything else is *observed* from LOAD.LOG.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .binparse import parse_sup_file
from .loadlog import (
    LoadEvent,
    detect_family,
    event_summary,
    parse_loadlog,
)
from .model import (
    FORMAT_FSB1,
    FORMAT_FSB4,
    KIND_UNKNOWN,
    SL_EXCL,
    STATUS_BAD_HEADER,
    STATUS_DISABLED,
    STATUS_DUPLICATE,
    STATUS_LOADED,
    STATUS_LOG_ONLY,
    STATUS_OLD_FORMAT,
    STATUS_RELEASED,
    Claim,
    Issue,
    SupInfo,
    kind_from,
    sup_id_text,
)


@dataclass
class SupGraph:
    """The offline picture of one card (or of one LOAD.LOG)."""

    root: Path | None = None
    sups: list[SupInfo] = field(default_factory=list)
    events: list[LoadEvent] = field(default_factory=list)
    loader_version: int = 6
    loader_family: str = "v3"
    issues: list[Issue] = field(default_factory=list)
    manifest: dict[str, str] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    log_path: Path | None = None
    fp_sup_dir: Path | None = None

    def by_name(self, name: str) -> SupInfo | None:
        wanted = name.casefold()
        for sup in self.sups:
            if sup.name.casefold() == wanted:
                return sup
        return None

    def by_id(self) -> dict[int, list[SupInfo]]:
        groups: dict[int, list[SupInfo]] = {}
        for sup in self.sups:
            if sup.sup_id:
                groups.setdefault(sup.sup_id, []).append(sup)
        return groups

    def observed_claims(self) -> list[tuple[SupInfo, Claim]]:
        return [
            (sup, claim)
            for sup in self.sups
            for claim in sup.claims
            if claim.source == "load.log"
        ]

    def event_counts(self) -> dict[str, int]:
        return event_summary(self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root) if self.root is not None else None,
            "fp_sup_dir": str(self.fp_sup_dir) if self.fp_sup_dir else None,
            "loader_version": self.loader_version,
            "loader_family": self.loader_family,
            "log_path": str(self.log_path) if self.log_path else None,
            "sups": [sup.to_dict() for sup in self.sups],
            "events": [event.to_dict() for event in self.events],
            "event_counts": self.event_counts(),
            "manifest": dict(self.manifest),
            "build": self.build,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def family_for_loader(loader_version: int) -> str:
    return "v3" if loader_version <= 6 else "v4"


def _find_child(directory: Path, name: str) -> Path | None:
    """Case-insensitive single-name lookup (card files may vary in case)."""
    if not directory.is_dir():
        return None
    wanted = name.casefold()
    try:
        for child in directory.iterdir():
            if child.name.casefold() == wanted and child.is_file():
                return child
    except OSError:
        return None
    return None


def _family_hint(length: int) -> str:
    """v3 pads LOAD.LOG to 4096 bytes, v4 to 8192 (sloader.c/sloader4.c)."""
    if length == 4096:
        return "v3"
    if length == 8192:
        return "v4"
    return "auto"


def _parse_log_source(source: str | bytes | Path) -> list[LoadEvent]:
    if isinstance(source, Path):
        return _parse_log_source(source.read_bytes())
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        return parse_loadlog(data, family=_family_hint(len(data)))
    text = str(source)
    if "\n" not in text and "\r" not in text and len(text) < 4096:
        try:
            path = Path(text)
            if path.is_file():
                return _parse_log_source(path.read_bytes())
        except (OSError, ValueError):
            pass
    return parse_loadlog(text, family=_family_hint(len(text)))


def build_graph(
    path: str | Path,
    *,
    loader_version: int | None = None,
    load_log: str | bytes | Path | None = None,
) -> SupGraph:
    """Build a graph from a card directory, a ``.LOG`` or a single ``.BIN``.

    ``loader_version`` is the loader's *service* version (v3 = 6, v4 = 7).
    When omitted it is inferred from the log family, then from the sup formats;
    the fallback is 6 (v3).
    """
    where = Path(path)
    if where.is_file():
        suffix = where.suffix.lower()
        if suffix == ".log":
            return graph_from_loadlog(
                where.read_bytes(), path=where, loader_version=loader_version
            )
        if suffix == ".bin":
            return _graph_from_one_sup(where, loader_version=loader_version)
        raise ValueError(f"{where}: expected a directory, a .LOG or a .BIN")
    if not where.is_dir():
        raise FileNotFoundError(f"{where}: no such card directory or LOAD.LOG")

    root = where.resolve()
    fp_sup = root / "fpSup"
    if not fp_sup.is_dir():
        fp_sup = root  # the caller pointed at fpSup/ itself

    graph = SupGraph(root=root, fp_sup_dir=fp_sup)

    # Every top-level .BIN except the loader.  Both loaders skip directories.
    files = sorted(
        (
            child
            for child in fp_sup.iterdir()
            if child.is_file()
            and child.suffix.lower() == ".bin"
            and child.name.casefold() != "loader.bin"
        ),
        key=lambda item: item.name.casefold(),
    )
    for file_path in files:
        try:
            graph.sups.append(parse_sup_file(file_path))
        except OSError as exc:
            graph.issues.append(
                Issue("error", "read-failed", f"{file_path.name}: {exc}", (file_path.name,))
            )

    _read_manifest(graph, root, fp_sup)
    _read_build(graph, root, fp_sup)

    log_path = _find_child(fp_sup, "LOAD.LOG") or _find_child(root, "LOAD.LOG")
    if load_log is not None:
        graph.events = _parse_log_source(load_log)
        graph.log_path = None
    elif log_path is not None:
        graph.events = _parse_log_source(log_path)
        graph.log_path = log_path
    if graph.events:
        _apply_loadlog(graph, graph.events)

    # Family/version: explicit wins, then the log, then the file formats.
    if loader_version is None:
        family = detect_family(graph.events)
        if family == "unknown":
            formats = {sup.format for sup in graph.sups if sup.has_header}
            if formats == {FORMAT_FSB4}:
                family = "v4"
            elif FORMAT_FSB1 in formats:
                family = "v3"
            else:
                family = "v3"
        loader_version = 7 if family == "v4" else 6
    graph.loader_version = int(loader_version)
    graph.loader_family = family_for_loader(graph.loader_version)
    return graph


def graph_from_loadlog(
    source: str | bytes | Path | Iterable[LoadEvent],
    *,
    path: Path | None = None,
    loader_version: int | None = None,
) -> SupGraph:
    """A graph made only from a LOAD.LOG (or already-parsed events)."""
    if isinstance(source, (str, bytes, bytearray, Path)):
        events = _parse_log_source(source)
    else:
        events = list(source)
    family = detect_family(events)
    if loader_version is None:
        loader_version = 7 if family == "v4" else 6
    graph = SupGraph(
        root=None,
        events=events,
        loader_version=int(loader_version),
        loader_family=family_for_loader(int(loader_version)),
        log_path=path,
    )
    _apply_loadlog(graph, events)
    return graph


def _graph_from_one_sup(path: Path, *, loader_version: int | None = None) -> SupGraph:
    sup = parse_sup_file(path)
    if loader_version is None:
        loader_version = 7 if sup.format == FORMAT_FSB4 else 6
    graph = SupGraph(
        root=path.parent,
        sups=[sup],
        loader_version=int(loader_version),
        loader_family=family_for_loader(int(loader_version)),
        fp_sup_dir=path.parent,
    )
    return graph


# -- manifest ----------------------------------------------------------------


def _read_manifest(graph: SupGraph, root: Path, fp_sup: Path) -> None:
    manifest = (
        _find_child(root, "MANIFEST.sha256")
        or _find_child(fp_sup, "MANIFEST.sha256")
        or _find_child(fp_sup.parent, "MANIFEST.sha256")
    )
    if manifest is None:
        return
    base = manifest.parent.resolve()
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        graph.issues.append(
            Issue("warning", "manifest-read", f"{manifest.name}: {exc}", (manifest.name,))
        )
        return

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            graph.issues.append(
                Issue(
                    "warning",
                    "manifest-line",
                    f"{manifest.name}:{lineno}: expected '<sha256>  <path>'",
                )
            )
            continue
        digest, rel = parts[0].lower(), parts[1].strip()
        rel = rel.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        graph.manifest[rel] = digest

        if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
            graph.issues.append(
                Issue(
                    "warning",
                    "manifest-path",
                    f"{manifest.name}:{lineno}: refused path {rel!r}",
                )
            )
            continue
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            graph.issues.append(
                Issue(
                    "warning",
                    "manifest-path",
                    f"{manifest.name}:{lineno}: path escapes the card: {rel!r}",
                )
            )
            continue
        if not target.is_file():
            graph.issues.append(
                Issue(
                    "warning",
                    "manifest-missing",
                    f"{manifest.name}:{lineno}: listed file missing: {rel}",
                )
            )
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            graph.issues.append(
                Issue(
                    "warning",
                    "manifest-mismatch",
                    f"{rel}: manifest {digest[:12]}..., file {actual[:12]}...",
                )
            )


# -- build.json --------------------------------------------------------------


def _read_build(graph: SupGraph, root: Path, fp_sup: Path) -> None:
    build_path = (
        _find_child(root, "build.json")
        or _find_child(fp_sup, "build.json")
        or _find_child(fp_sup.parent, "build.json")
    )
    if build_path is None:
        return
    try:
        data = json.loads(build_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        graph.issues.append(
            Issue("warning", "build-json", f"{build_path.name}: {exc}", (build_path.name,))
        )
        return
    if not isinstance(data, dict):
        graph.issues.append(
            Issue("warning", "build-json", f"{build_path.name}: not a JSON object")
        )
        return
    graph.build = data

    file_name = data.get("file")
    target = graph.by_name(file_name) if isinstance(file_name, str) else None
    if target is None and len(graph.sups) == 1:
        target = graph.sups[0]

    patch = data.get("patch")
    if isinstance(patch, dict):
        addr = patch.get("address", patch.get("addr"))
        if isinstance(addr, int) and not isinstance(addr, bool):
            length = patch.get("len", patch.get("length", 4))
            if not isinstance(length, int) or isinstance(length, bool):
                length = 4
            kind = kind_from(patch.get("kind", SL_EXCL))
            _add_declared_claim(
                graph,
                target,
                Claim(
                    addr=addr,
                    len=length,
                    kind=kind,
                    source="build.json:patch (kind inferred EXCL)"
                    if "kind" not in patch
                    else "build.json:patch",
                ),
                file_name,
            )

    claims = data.get("claims")
    if isinstance(claims, list):
        for index, item in enumerate(claims):
            if not isinstance(item, dict):
                continue
            addr = item.get("address", item.get("addr"))
            if not isinstance(addr, int) or isinstance(addr, bool):
                continue
            length = item.get("len", item.get("length", 4))
            if not isinstance(length, int) or isinstance(length, bool):
                length = 4
            kind = kind_from(item.get("kind", SL_EXCL))
            claim_target = (
                graph.by_name(item["file"])
                if isinstance(item.get("file"), str)
                else target
            )
            _add_declared_claim(
                graph,
                claim_target,
                Claim(
                    addr=addr,
                    len=length,
                    kind=kind,
                    source=f"build.json:claims[{index}]",
                ),
                item.get("file", file_name),
            )


def _add_declared_claim(
    graph: SupGraph,
    sup: SupInfo | None,
    claim: Claim,
    file_name: Any,
) -> None:
    if sup is None:
        graph.issues.append(
            Issue(
                "warning",
                "build-claim-unmatched",
                f"build metadata claim 0x{claim.addr:08X} has no matching sup"
                + (f" ({file_name})" if file_name else ""),
            )
        )
        return
    sup.claims.append(claim)


# -- LOAD.LOG ----------------------------------------------------------------


def _sup_for_log(graph: SupGraph, name: str, *, sup_id: int = 0) -> SupInfo | None:
    if not name:
        return None
    sup = graph.by_name(name)
    if sup is not None:
        if sup_id and not sup.sup_id:
            sup.sup_id = sup_id
        return sup
    sup = SupInfo(
        path="",
        name=name,
        sup_id=sup_id,
        format="unknown",
        status=STATUS_LOG_ONLY,
        has_header=False,
    )
    graph.sups.append(sup)
    return sup


def _apply_loadlog(graph: SupGraph, events: Iterable[LoadEvent]) -> None:
    last_loaded: SupInfo | None = None
    for event in events:
        name = event.name
        if name == "LOADED":
            sup = _sup_for_log(graph, event.text)
            if sup is None:
                continue
            sup.loaded = True
            sup.released = False
            sup.status = STATUS_LOADED
            sup.load_count += 1
            last_loaded = sup
        elif name == "CLAIM":
            # v3/v4 log only the first address of a claim, never its length
            # or kind.  Keep it as observed evidence with len=4 (word) and
            # length_known=False; the solver never treats it as a declaration.
            if last_loaded is not None:
                claim = Claim(
                    addr=event.a,
                    len=4,
                    kind=KIND_UNKNOWN,
                    source="load.log",
                    length_known=False,
                )
                if not any(
                    old.addr == claim.addr and old.source == "load.log"
                    for old in last_loaded.claims
                ):
                    last_loaded.claims.append(claim)
        elif name == "RELEASED":
            sup = graph.by_name(event.text) or last_loaded
            if sup is not None:
                sup.loaded = False
                sup.released = True
                sup.status = STATUS_RELEASED
                sup.release_count += 1
            if last_loaded is not None and sup is last_loaded:
                last_loaded = None
        elif name == "UNLOAD":
            sup = _sup_for_log(graph, event.text, sup_id=event.a)
            if sup is not None:
                sup.loaded = False
                sup.released = True
                sup.status = STATUS_RELEASED
                sup.release_count += 1
            if last_loaded is not None and sup is last_loaded:
                last_loaded = None
        elif name == "HOTLOAD":
            sup = _sup_for_log(graph, event.text)
            if sup is not None:
                sup.loaded = True
                sup.released = False
                sup.status = STATUS_LOADED
                sup.load_count += 1
        elif name == "DUP_ID":
            sup = _sup_for_log(graph, event.text, sup_id=event.a)
            if sup is not None:
                sup.status = STATUS_DUPLICATE
                sup.conflicts.append(
                    f"duplicate sup_id {sup_id_text(event.a)} (loader skipped this file)"
                )
        elif name == "CONFLICT":
            sup = _sup_for_log(graph, event.text)
            if sup is not None:
                sup.conflicts.append(
                    f"claim conflict at 0x{event.a:08X} with sup_id "
                    f"{sup_id_text(event.b)}"
                )
        elif name == "OLD_FORMAT":
            sup = _sup_for_log(graph, event.text)
            if sup is not None:
                sup.status = STATUS_OLD_FORMAT
                sup.conflicts.append(
                    "v4 loader refused FSB1 (old format); see OLD_FORMAT"
                )
        elif name == "BAD_HEADER":
            sup = _sup_for_log(graph, event.text)
            if sup is not None:
                sup.status = STATUS_BAD_HEADER
                sup.conflicts.append(
                    f"loader refused the header (magic 0x{event.a:08X}, got {event.b} bytes)"
                )
        elif name == "DISABLED":
            sup = _sup_for_log(graph, event.text, sup_id=event.a)
            if sup is not None:
                sup.status = STATUS_DISABLED
        elif name == "NO_DIR":
            graph.issues.append(
                Issue("error", "no-sup-dir", f"loader found no directory: {event.text}")
            )
        elif name == "NO_POFF":
            graph.issues.append(
                Issue(
                    "warning",
                    "no-poweroff",
                    f"loader could not register power-off handler {event.a}",
                )
            )
        elif name in {"OPEN_FAIL", "SHORT_READ", "NO_MEMORY"}:
            graph.issues.append(
                Issue(
                    "warning",
                    name.lower().replace("_", "-"),
                    f"{name}: {event.text or 'no filename'} "
                    f"(a=0x{event.a:08X}, b=0x{event.b:08X})",
                )
            )
