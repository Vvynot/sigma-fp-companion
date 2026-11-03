"""Command-line interface for the offline SUP compatibility solver.

Usage::

    python -m fp_companion.compat check  <card-dir|LOAD.LOG|file.BIN> [--json]
    python -m fp_companion.compat graph  <card-dir|LOAD.LOG|file.BIN> [--json]
    python -m fp_companion.compat explain <card-dir|LOAD.LOG|file.BIN> [--json]

All commands accept ``--loader-version N`` (service version: 6 = v3/FSB1,
7 = v4/FSB4).  Without it the version is inferred from the log family or the
sup formats, falling back to 6.

Exit codes for ``check`` and ``explain``: 0 compatible, 1 conflicts, 2 usage
or unreadable input.  ``graph`` uses 0 on success and 2 on usage/input errors.
No camera is contacted and no card is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .graph import SupGraph, build_graph
from .solver import CompatResult, can_combine

EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2


def _loader_version(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive service version")
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _load_graph(path: str, loader_version: int | None) -> SupGraph:
    return build_graph(path, loader_version=loader_version)


def _check(graph: SupGraph) -> CompatResult:
    return can_combine(graph.sups, loader_version=graph.loader_version)


def _print_result(graph: SupGraph, result: CompatResult) -> None:
    if result.ok:
        print(
            f"compatible: {len(graph.sups)} sup(s), loader service "
            f"v{result.loader_version} ({result.loader_family})"
        )
    else:
        print(
            f"conflicts: {len(result.errors)} error(s), "
            f"{len(result.warnings)} warning(s), loader service "
            f"v{result.loader_version} ({result.loader_family})"
        )
    for issue in result.issues:
        print(f"  {issue.severity}: {issue.message}")


def _print_graph(graph: SupGraph) -> None:
    print(f"card: {graph.root if graph.root is not None else '(LOAD.LOG only)'}")
    if graph.fp_sup_dir is not None:
        print(f"fpSup: {graph.fp_sup_dir}")
    if graph.log_path is not None:
        print(f"log: {graph.log_path}")
    print(
        f"loader: service v{graph.loader_version} ({graph.loader_family}), "
        f"{len(graph.sups)} sup(s)"
    )
    for sup in graph.sups:
        header = (
            f"{sup.format} id={sup.sup_id_text} version={sup.version} "
            f"min_svc={sup.min_svc}"
            if sup.has_header
            else "log record"
        )
        claims = len(sup.claims)
        print(
            f"  {sup.label}  [{header}] status={sup.status} "
            f"loaded={sup.loaded} released={sup.released} claims={claims}"
        )
        for claim in sup.claims:
            note = "" if claim.length_known else " (address only)"
            print(
                f"    claim 0x{claim.addr:08X} len={claim.len} "
                f"{claim.kind_name} source={claim.source}{note}"
            )
        for conflict in sup.conflicts:
            print(f"    conflict: {conflict}")
        for problem in sup.problems:
            print(f"    problem: {problem}")
    if graph.manifest:
        print(f"manifest: {len(graph.manifest)} entries")
    if graph.build:
        print(f"build.json: {graph.build.get('product') or '(no product)'}")
    if graph.events:
        counts = ", ".join(
            f"{name}={count}" for name, count in sorted(graph.event_counts().items())
        )
        print(f"log events: {counts}")
    for issue in graph.issues:
        print(f"  {issue.severity}: {issue.message}")


def _print_explain(graph: SupGraph, result: CompatResult) -> None:
    print(
        f"verdict: {'compatible' if result.ok else 'conflicts'} "
        f"(loader service v{result.loader_version}, {result.loader_family})"
    )
    if result.issues:
        for issue in result.issues:
            print(f"  {issue.severity} [{issue.code}]: {issue.message}")
    else:
        print("  no conflicts or warnings")
    print()
    _print_graph(graph)
    if graph.events:
        print()
        print("LOAD.LOG events:")
        for event in graph.events:
            text = f" {event.text}" if event.text else ""
            print(
                f"  {event.line:>4} {event.code:02X} {event.name:<10} "
                f"a=0x{event.a:08X} b=0x{event.b:08X}{text}"
            )


def _usage_error(message: str) -> int:
    print(f"compat: {message}", file=sys.stderr)
    return EXIT_USAGE


def _run(
    args: argparse.Namespace, mode: str
) -> int:
    try:
        graph = _load_graph(args.path, args.loader_version)
    except (OSError, ValueError) as exc:
        return _usage_error(str(exc))

    if mode == "graph":
        if args.json:
            _print_json(graph.to_dict())
        else:
            _print_graph(graph)
        return EXIT_OK

    result = _check(graph)
    if mode == "explain":
        if args.json:
            _print_json({"graph": graph.to_dict(), "result": result.to_dict()})
        else:
            _print_explain(graph, result)
    else:
        if args.json:
            _print_json(result.to_dict())
        else:
            _print_result(graph, result)
    return EXIT_OK if result.ok else EXIT_CONFLICT


def _cmd_check(args: argparse.Namespace) -> int:
    return _run(args, "check")


def _cmd_graph(args: argparse.Namespace) -> int:
    return _run(args, "graph")


def _cmd_explain(args: argparse.Namespace) -> int:
    return _run(args, "explain")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fp_companion.compat",
        description=(
            "Offline SUP compatibility/dependency solver: reads fpSup/*.BIN "
            "headers, MANIFEST.sha256, build.json and LOAD.LOG without a camera."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("check", _cmd_check, "Report whether the card's sups can combine"),
        ("graph", _cmd_graph, "Show the card graph (sups, claims, log events)"),
        ("explain", _cmd_explain, "Explain the verdict and every claim/event"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument(
            "path",
            help="Card directory, fpSup directory, LOAD.LOG or one .BIN file",
        )
        command.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON",
        )
        command.add_argument(
            "--loader-version",
            metavar="N",
            type=_loader_version,
            default=None,
            help=(
                "Loader service version: 6 = v3/FSB1, 7 = v4/FSB4 "
                "(default: infer from log/formats, else 6)"
            ),
        )
        command.set_defaults(func=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
