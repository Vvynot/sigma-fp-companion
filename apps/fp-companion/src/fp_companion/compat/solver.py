"""A small SUP compatibility/dependency solver.

Rules (all mirrored from the loaders rather than invented):

* duplicate ``sup_id`` -- v3 ``enter_sup`` and v4 ``load_one`` skip a second
  file with an id already kept;
* claim compatibility -- ``SL_EXCL`` excludes everyone; ``SL_CHAIN`` and
  ``SL_SHARED_UI`` may share only the *same* ``(addr, len)`` with the same
  kind (``svc_claim`` in ``sloader.c``);
* ``min_svc`` -- ``header_ok`` refuses ``min_svc > loader service version``;
* format -- v3 (service <= 6) refuses ``FSB4``; v4 (service >= 7) refuses
  ``FSB1`` as the old format;
* claims observed in LOAD.LOG are compared with build-metadata declarations
  where a declaration exists.  A sup header itself has no claim table.

Example::

    from fp_companion.compat import can_combine
    result = can_combine(graph.sups, loader_version=6)
    if not result.ok:
        print(result.reasons)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import (
    FORMAT_FSB1,
    FORMAT_FSB4,
    FORMAT_UNKNOWN,
    KIND_UNKNOWN,
    SL_EXCL,
    STATUS_BAD_HEADER,
    Claim,
    Issue,
    SupInfo,
    sup_id_text,
)

DEFAULT_LOADER_VERSION = 6


@dataclass
class CompatResult:
    """The structured answer from :func:`can_combine`."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    loader_version: int = DEFAULT_LOADER_VERSION

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if not issue.is_error]

    @property
    def loader_family(self) -> str:
        return "v3" if self.loader_version <= 6 else "v4"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "loader_version": self.loader_version,
            "loader_family": self.loader_family,
            "reasons": list(self.reasons),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _claim_overlaps(a: Claim, b: Claim) -> bool:
    if a.len <= 0 or b.len <= 0:  # a resource claim is identified by its id
        return a.addr == b.addr
    return a.addr < b.addr + b.len and b.addr < a.addr + a.len


def _claims_compatible(a: Claim, b: Claim) -> bool:
    """``svc_claim``: EXCL excludes; otherwise an identical (addr,len) shares."""
    if a.kind == SL_EXCL or b.kind == SL_EXCL:
        return False
    return a.kind == b.kind and a.addr == b.addr and a.len == b.len


def _range_text(claim: Claim) -> str:
    if claim.len <= 0:
        return f"resource 0x{claim.addr:08X}"
    return f"0x{claim.addr:08X}..0x{claim.addr + claim.len:08X}"


def can_combine(
    sups: Iterable[SupInfo], loader_version: int = DEFAULT_LOADER_VERSION
) -> CompatResult:
    """Decide whether ``sups`` can be loaded together by that loader.

    ``loader_version`` is the loader's *service* version: 6 is v3, 7 is v4.
    """
    loader_version = int(loader_version)
    if loader_version < 1:
        raise ValueError("loader_version must be a positive service version")
    sup_list = list(sups)
    issues: list[Issue] = []

    def add(severity: str, code: str, message: str, *names: str) -> None:
        issues.append(Issue(severity, code, message, tuple(names)))

    v3_loader = loader_version <= 6

    # -- header format and minimum service version -------------------------
    for sup in sup_list:
        if not sup.has_header:
            continue  # a LOAD.LOG-only record: no header to judge
        name = sup.name or sup.path
        if sup.format == FORMAT_UNKNOWN:
            add("error", "bad-header", f"{name}: no FSB1/FSB4 magic", name)
        elif v3_loader and sup.format == FORMAT_FSB4:
            add(
                "error",
                "loader-format",
                f"{name}: FSB4 sup refused by the v3 loader "
                f"(service v{loader_version}); v3 loads FSB1 only",
                name,
            )
        elif not v3_loader and sup.format == FORMAT_FSB1:
            add(
                "error",
                "old-format",
                f"{name}: FSB1 sup refused by the v4 loader "
                f"(service v{loader_version}) as an old-format file",
                name,
            )
        if sup.status == STATUS_BAD_HEADER and sup.has_header:
            detail = "; ".join(sup.problems) if sup.problems else "header check failed"
            add("error", "bad-header", f"{name}: {detail}", name)
        if sup.min_svc is not None and sup.min_svc > loader_version:
            add(
                "error",
                "min-svc",
                f"{name}: min_svc {sup.min_svc} > loader service "
                f"v{loader_version}",
                name,
            )

    # -- duplicate sup_id ---------------------------------------------------
    by_id: dict[int, list[SupInfo]] = {}
    for sup in sup_list:
        if sup.has_header and sup.sup_id:
            by_id.setdefault(sup.sup_id, []).append(sup)
    for sup_id, group in sorted(by_id.items()):
        if len(group) > 1:
            names = tuple(sup.name or sup.path for sup in group)
            add(
                "error",
                "duplicate-id",
                f"duplicate sup_id {sup_id_text(sup_id)}: " + ", ".join(names),
                *names,
            )

    # -- declared claim compatibility --------------------------------------
    declared: list[tuple[SupInfo, Claim]] = [
        (sup, claim)
        for sup in sup_list
        for claim in sup.claims
        if claim.source != "load.log" and claim.kind != KIND_UNKNOWN
    ]
    for index, (left_sup, left) in enumerate(declared):
        for right_sup, right in declared[index + 1 :]:
            if not _claim_overlaps(left, right):
                continue
            if _claims_compatible(left, right):
                continue
            left_name = left_sup.name or left_sup.path
            right_name = right_sup.name or right_sup.path
            add(
                "error",
                "claim-conflict",
                f"{_range_text(left)} {left.kind_name} ({left_name}) "
                f"overlaps {_range_text(right)} {right.kind_name} "
                f"({right_name})",
                left_name,
                right_name,
            )

    # -- observed LOAD.LOG claims vs build-metadata declarations -----------
    for sup in sup_list:
        if not (sup.loaded or sup.released):
            continue
        known = [
            claim
            for claim in sup.claims
            if claim.source != "load.log" and claim.kind != KIND_UNKNOWN
        ]
        observed = [claim for claim in sup.claims if claim.source == "load.log"]
        if not known or not observed:
            continue
        name = sup.name or sup.path
        for claim in observed:
            if not any(decl.covers(claim.addr) for decl in known):
                add(
                    "warning",
                    "claim-undeclared",
                    f"{name}: claim observed at 0x{claim.addr:08X} is not "
                    f"declared by build metadata",
                    name,
                )
        for claim in known:
            if not any(claim.covers(obs.addr) for obs in observed):
                add(
                    "warning",
                    "claim-unobserved",
                    f"{name}: declared claim {_range_text(claim)} was not "
                    f"observed in LOAD.LOG",
                    name,
                )

    # -- conflicts the loader itself logged --------------------------------
    for sup in sup_list:
        name = sup.name or sup.path
        for message in sup.conflicts:
            add("error", "log-conflict", f"{name}: {message}", name)

    # Stable de-duplication: the same header problem may be found twice.
    seen: set[tuple[str, str, str]] = set()
    unique: list[Issue] = []
    for issue in issues:
        key = (issue.severity, issue.code, issue.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)

    ok = not any(issue.is_error for issue in unique)
    reasons = [issue.message for issue in unique]
    return CompatResult(
        ok=ok,
        reasons=reasons,
        issues=unique,
        loader_version=loader_version,
    )
