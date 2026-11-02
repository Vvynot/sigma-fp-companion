"""Command-line interface for :mod:`fp_companion.hotload`.

Usage::

    python -m fp_companion.hotload doctor
    python -m fp_companion.hotload probe       [--json]
    python -m fp_companion.hotload snapshot    [--json]
    python -m fp_companion.hotload release     --yes [--json]
    python -m fp_companion.hotload reload      --yes [--json]
    python -m fp_companion.hotload load        --path '\\fpSup\\30LEXPO.BIN' --yes
    python -m fp_companion.hotload cycle --n 10 --yes [--json]
    python -m fp_companion.hotload stress --n 20 --yes [--json]

``probe`` and ``snapshot`` only read.  Every write command prints its plan and
refuses without ``--yes``.  Exit status is ``0`` (success), ``1`` (a
verification failed) or ``2`` (usage, transport or device error).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

from .core import (
    CODE_ADDR,
    ECHO_ORIG,
    ECHO_SLOT,
    LAYOUTS,
    LXPO_ID,
    P_BLOCK,
    PATH_BUF,
    PATH_BUF_BYTES,
    SL_BOOK_AT,
    SITE_LV,
    SITE_PATCH,
    Hotload,
    TransportError,
    UsageError,
    VerifyError,
    clang_path,
)
from .link import BACKENDS, open_link

EXIT_OK = 0
EXIT_VERIFY = 1
EXIT_USAGE = 2

WRITE_COMMANDS = {"release", "load", "reload", "cycle", "stress"}

CAMERA_VID = 0x1003
CAMERA_PID = 0xC432


# =============================================================================
# Static diagnostics (no camera required)
# =============================================================================
_dll_handles: list[object] = []


def libusb_loadable() -> tuple[bool, str]:
    """Best-effort check that libusb can be loaded by the running interpreter."""
    for directory in (
        Path(sys.prefix) / "Library" / "bin",
        Path(sys.prefix) / "Lib" / "site-packages" / "usb1",
    ):
        if not directory.is_dir():
            continue
        if hasattr(os, "add_dll_directory"):
            try:
                _dll_handles.append(os.add_dll_directory(str(directory)))
            except OSError:
                pass
        if str(directory) not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = (
                str(directory) + os.pathsep + os.environ.get("PATH", "")
            )

    if sys.platform == "win32":
        names = ["libusb-1.0.dll"]
        loader = ctypes.WinDLL  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        names = ["libusb-1.0.dylib", "libusb-1.0.0.dylib"]
        loader = ctypes.CDLL
    else:
        names = ["libusb-1.0.so.0", "libusb-1.0.so"]
        loader = ctypes.CDLL

    errors: list[str] = []
    for name in names:
        try:
            handle = loader(name)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
            continue
        _dll_handles.append(handle)
        return True, name
    return False, "; ".join(errors) or "not found"


def usb_device_present() -> tuple[bool | None, str]:
    """Whether libusb enumerates the camera.  ``None`` means not detectable."""
    try:
        import usb.core  # noqa: F401  (import after DLL paths are set)
    except Exception as exc:  # noqa: BLE001
        return None, f"pyusb unavailable: {exc}"
    try:
        device = usb.core.find(idVendor=CAMERA_VID, idProduct=CAMERA_PID)
    except Exception as exc:  # noqa: BLE001
        return None, f"libusb error: {exc}"
    if device is None:
        return False, f"{CAMERA_VID:04x}:{CAMERA_PID:04x} not found"
    return True, f"{CAMERA_VID:04x}:{CAMERA_PID:04x} found"


def winusb_binding() -> tuple[bool | None, str]:
    """Best-effort Windows driver check for the camera's USB device node."""
    if sys.platform != "win32":
        return None, "not detectable (Windows only)"
    try:
        import winreg
    except Exception as exc:  # noqa: BLE001
        return None, f"not detectable (winreg unavailable: {exc})"

    root = r"SYSTEM\CurrentControlSet\Enum\USB"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
    except OSError as exc:
        return None, f"not detectable ({exc})"

    names = f"VID_{CAMERA_VID:04X}&PID_{CAMERA_PID:04X}".upper()
    found: list[str] = []
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            if names not in name.upper():
                continue
            try:
                instance = winreg.OpenKey(key, name)
            except OSError:
                continue
            try:
                sub_index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(instance, sub_index)
                    except OSError:
                        break
                    sub_index += 1
                    try:
                        sub_key = winreg.OpenKey(instance, sub)
                    except OSError:
                        continue
                    try:
                        service = winreg.QueryValueEx(sub_key, "Service")[0]
                        found.append(str(service))
                    except OSError:
                        pass
                    finally:
                        winreg.CloseKey(sub_key)
            finally:
                winreg.CloseKey(instance)
    finally:
        winreg.CloseKey(key)

    if not found:
        return None, "not detectable (no matching Enum\\USB entry)"
    services = ", ".join(sorted(set(found)))
    if any(item.lower() in ("winusb", "libusbk", "libusb0") for item in found):
        return True, f"yes ({services})"
    return False, f"no ({services})"


def _guidance_lines() -> list[str]:
    return [
        "",
        "USB device not found.  On Windows the camera must be bound to a",
        "libusb-compatible driver (WinUSB).  To fix it with Zadig:",
        "  1. Run Zadig as administrator.",
        "  2. Options -> List All Devices.",
        f"  3. Select the SIGMA fp ({CAMERA_VID:04x}:{CAMERA_PID:04x}).",
        "  4. Choose WinUSB and click Replace Driver.",
        "  5. Reconnect the camera, then rerun `fp-hotload doctor`.",
        "",
        "Use the fp-companion conda interpreter: its Library\\bin supplies",
        "libusb-1.0.dll.",
    ]


# =============================================================================
# Output helpers
# =============================================================================
def _emit(payload: dict, as_json: bool, human: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in human:
            print(line)


def _probe_lines(data: dict) -> list[str]:
    svc = ("n/a" if data["svc_version"] is None
           else str(data["svc_version"]))
    book = f"0x{data['sl_book']:08X}"
    book += (" (loader resident)" if data["loader_ok"]
             else " (not a loader table)")
    return [
        f"version      : {data['version'] or '<empty>'}",
        f"0x{SITE_PATCH:08X} : 0x{data['site_patch']:08X} "
        f"({data['site_patch_state']})",
        f"0x{SITE_LV:08X} : 0x{data['site_lv']:08X} "
        f"({data['site_lv_state']})",
        f"SL_BOOK_AT   : {book}",
        f"svc.version  : {svc}",
        f"result       : {'PASS' if data['ok'] else 'FAIL'}",
    ]


def _snapshot_lines(data: dict) -> list[str]:
    lines = _probe_lines(data)[:-1]
    if data["lv_done"] is None:
        if data["lv_done_addr"] is None:
            where = "n/a (hook not armed, or no veneer)"
        else:
            where = f"unknown @ 0x{data['lv_done_addr']:08X}"
    else:
        where = f"{data['lv_done']} @ 0x{data['lv_done_addr']:08X}"
    lines.append(f"lv_done      : {where}")
    lines.append("result       : snapshot (read-only)")
    return lines


def _site_lines(sites: dict) -> list[str]:
    return [
        f"  0x{SITE_PATCH:08X} : 0x{sites['patch']:08X} "
        f"({sites['patch_state']})",
        f"  0x{SITE_LV:08X} : 0x{sites['lv']:08X} ({sites['lv_state']})",
    ]


def _op_lines(command: str, res: dict, sites: dict, ok: bool) -> list[str]:
    extra = (f", path 0x{res['path_addr']:08X}" if "path_addr" in res else "")
    lines = [
        f"{command}: return {res['return']} "
        f"({'SL_OK' if res['return'] == 0 else 'not SL_OK'})",
        f"  svc        : 0x{res['book']:08X}, entry 0x{res['entry']:08X}{extra}",
    ]
    lines += _site_lines(sites)
    lines.append(f"  result     : {'PASS' if ok else 'FAIL'}")
    return lines


def _cycle_lines(results: list[dict], n: int) -> list[str]:
    lines = []
    for result in results:
        if "error" in result:
            lines.append(f"cycle {result['iteration']}: FAIL {result['error']}")
        else:
            lines.append(
                f"cycle {result['iteration']}: return {result['return']}  "
                f"patch 0x{result['sites']['patch']:08X}  "
                f"lv {result['sites']['lv_state']}  "
                f"{result['elapsed_s'] * 1000:.1f} ms  "
                f"{'PASS' if result['ok'] else 'FAIL'}")
    ok = len(results) == n and all(item["ok"] for item in results)
    lines.append(f"result: {'PASS' if ok else 'FAIL'} "
                 f"({len(results)}/{n} iterations)")
    return lines


def _stress_lines(summary: dict) -> list[str]:
    lines = _cycle_lines(summary["results"], summary["n"])
    if summary["min_s"] is not None:
        lines.insert(
            -1,
            f"timing: min {summary['min_s'] * 1000:.1f} ms  "
            f"avg {summary['avg_s'] * 1000:.1f} ms  "
            f"max {summary['max_s'] * 1000:.1f} ms",
        )
    return lines


def _fail(exc: BaseException, as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": str(exc)}))
    else:
        print(f"ERR {exc}", file=sys.stderr)
    return code


# =============================================================================
# Command implementations
# =============================================================================
def _cmd_doctor(args: argparse.Namespace) -> int:
    clang = clang_path()
    dll_ok, dll_detail = libusb_loadable()
    binding_ok, binding_detail = winusb_binding()
    present, present_detail = usb_device_present()

    payload: dict = {
        "command": "doctor",
        "clang": clang,
        "libusb": {"ok": dll_ok, "detail": dll_detail},
        "usb_binding": {"ok": binding_ok, "detail": binding_detail},
        "usb_device_present": present,
        "usb_device_detail": present_detail,
    }

    link = None
    try:
        link = open_link(args.backend, timeout_ms=args.timeout,
                         retries=args.retries)
    except Exception as exc:  # noqa: BLE001
        payload.update({"ok": False, "shell_responsive": False,
                        "error": str(exc)})
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"clang (optional): {clang or 'not on PATH'}")
            print(f"libusb-1.0.dll : "
                  f"{'loadable' if dll_ok else 'not loadable'} ({dll_detail})")
            print(f"USB binding    : {binding_detail}")
            print(f"USB device     : {present_detail}")
            print(f"camera         : {exc}")
        not_found = (
            present is False
            or "not found" in str(exc).lower()
            or "no device" in str(exc).lower()
        )
        if not_found:
            stream = sys.stderr if args.json else sys.stdout
            for line in _guidance_lines():
                print(line, file=stream)
        return EXIT_USAGE

    try:
        try:
            version = link.sh("version").strip()
        except Exception as exc:  # noqa: BLE001
            version = ""
            payload["version_error"] = str(exc)

        probe: dict | None = None
        try:
            probe = Hotload(link, layout=args.svc_layout).probe()
        except Exception as exc:  # noqa: BLE001
            payload["probe_error"] = str(exc)

        shell_ok = bool(version)
        loader_ok = bool(
            probe is not None and probe["loader_ok"] and probe["version_ok"])
        ok = shell_ok and loader_ok
        payload.update({
            "ok": ok,
            "shell_responsive": shell_ok,
            "loader_ready": loader_ok,
            "version": version,
        })
        if probe is not None:
            payload.update({
                "sl_book": probe["sl_book"],
                "loader_ok": probe["loader_ok"],
                "svc_version": probe["svc_version"],
                "site_patch": probe["site_patch"],
                "site_patch_state": probe["site_patch_state"],
                "site_lv": probe["site_lv"],
                "site_lv_state": probe["site_lv_state"],
            })
    finally:
        try:
            link.close()
        except Exception:  # noqa: BLE001
            pass

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"version        : {version or '<empty>'}")
        if probe is not None:
            book = f"0x{probe['sl_book']:08X}"
            book += (" (loader resident)" if probe["loader_ok"]
                     else " (not a loader table)")
            svc = ("n/a" if probe["svc_version"] is None
                   else str(probe["svc_version"]))
            print(f"SL_BOOK_AT     : {book}")
            print(f"svc.version    : {svc}")
            print(f"0x{SITE_PATCH:08X} : 0x{probe['site_patch']:08X} "
                  f"({probe['site_patch_state']})")
            print(f"0x{SITE_LV:08X} : 0x{probe['site_lv']:08X} "
                  f"({probe['site_lv_state']})")
        else:
            print(f"loader         : {payload.get('probe_error', 'unavailable')}")
        print(f"shell          : "
              f"{'responsive' if shell_ok else 'no response'}")
        print(f"clang (optional): {clang or 'not on PATH'}")
        print(f"libusb-1.0.dll : "
              f"{'loadable' if dll_ok else 'not loadable'} ({dll_detail})")
        print(f"USB binding    : {binding_detail}")
        print(f"USB device     : {present_detail}")
        print(f"doctor         : "
              f"{'OK' if ok else 'shell responsive; loader not ready' if shell_ok else 'shell did not respond'}")
    return EXIT_OK if ok else EXIT_VERIFY


def _run(link: object, args: argparse.Namespace) -> int:
    hotload = Hotload(link, layout=args.svc_layout,
                      code_addr=args.code_addr,
                      force_code_addr=args.force_code_addr)

    if args.command == "probe":
        data = hotload.probe()
        _emit({"command": "probe", **data}, args.json, _probe_lines(data))
        return EXIT_OK if data["ok"] else EXIT_VERIFY

    if args.command == "snapshot":
        data = hotload.snapshot()
        _emit({"command": "snapshot", **data}, args.json,
              _snapshot_lines(data))
        return EXIT_OK

    if args.command == "release":
        res = hotload.release()
        sites = hotload.verify_released()
        ok = res["return"] == 0 and sites["ok"]
        _emit({"command": "release", **res, "sites": sites, "ok": ok},
              args.json, _op_lines("release", res, sites, ok))
        return EXIT_OK if ok else EXIT_VERIFY

    if args.command == "reload":
        res = hotload.reload()
        sites = hotload.verify_sites()
        ok = res["return"] == 0 and sites["ok"]
        _emit({"command": "reload", **res, "sites": sites, "ok": ok},
              args.json, _op_lines("reload", res, sites, ok))
        return EXIT_OK if ok else EXIT_VERIFY

    if args.command == "load":
        res = hotload.load(path=args.path, path_addr=args.path_addr,
                           force_scratch=args.force_scratch)
        sites = hotload.verify_sites()
        ok = res["return"] == 0 and sites["ok"]
        _emit({"command": "load", **res, "sites": sites, "ok": ok},
              args.json, _op_lines("load", res, sites, ok))
        return EXIT_OK if ok else EXIT_VERIFY

    if args.command == "cycle":
        results = hotload.cycle(args.n)
        ok = len(results) == args.n and all(item["ok"] for item in results)
        _emit({"command": "cycle", "n": args.n, "results": results, "ok": ok},
              args.json, _cycle_lines(results, args.n))
        return EXIT_OK if ok else EXIT_VERIFY

    if args.command == "stress":
        summary = hotload.stress(args.n)
        _emit({"command": "stress", **summary}, args.json,
              _stress_lines(summary))
        return EXIT_OK if summary["ok"] else EXIT_VERIFY

    raise UsageError(f"unknown command {args.command!r}")


# =============================================================================
# Argparse
# =============================================================================
def _common_options() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON on stdout")
    parser.add_argument("--backend", choices=BACKENDS, default="auto",
                        help="transport backend (default: auto)")
    parser.add_argument("--timeout", type=int, default=None, metavar="MS",
                        help="per-transfer timeout in ms")
    parser.add_argument("--retries", type=int, default=None, metavar="N",
                        help="transport attempts per command")
    parser.add_argument("--svc-layout", choices=("standard", "allsups"),
                        default="standard",
                        help="sl_svc layout: standard (release +44) or "
                             "SL_ALLSUPS (release +48); default standard")
    parser.add_argument("--code-addr", type=lambda s: int(s, 0),
                        default=CODE_ADDR,
                        help=f"cave address for callfn.S "
                             f"(default 0x{CODE_ADDR:08X})")
    parser.add_argument("--force-code-addr", action="store_true",
                        help="overwrite a nonzero first word at --code-addr")
    parser.add_argument("--yes", action="store_true",
                        help="confirm a write command")
    return parser


def _doctor_options() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON on stdout")
    parser.add_argument("--backend", choices=BACKENDS, default="auto",
                        help="transport backend (default: auto)")
    parser.add_argument("--timeout", type=int, default=None, metavar="MS",
                        help="per-transfer timeout in ms")
    parser.add_argument("--retries", type=int, default=None, metavar="N",
                        help="transport attempts per command")
    parser.add_argument("--svc-layout", choices=("standard", "allsups"),
                        default="standard", help="sl_svc layout")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    ap = argparse.ArgumentParser(
        prog="python -m fp_companion.hotload",
        description="LExpo hot reload over the fpSup USB shell (read-only "
                    "until a write command is invoked with --yes).",
        epilog="A wrong function address passed to callfn.S freezes the shell "
               "and may need a battery pull.  Service-table pointers are "
               "validated first; --force-code-addr overrides only the cave "
               "occupancy check.  reload/load do file I/O on the camera: if "
               "the default 1000 ms transfer timeout is too short, pass "
               "--timeout 10000.")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=[_doctor_options()],
                   help="environment and camera/loader diagnostics")
    sub.add_parser("probe", parents=[common],
                   help="read version, both sites, SL_BOOK_AT and svc.version")
    sub.add_parser("snapshot", parents=[common],
                   help="probe plus lv_done when the hook exposes it")
    sub.add_parser("release", parents=[common],
                   help="call svc->release for LExpo (needs --yes)")
    sub.add_parser("reload", parents=[common],
                   help="call svc->reload for LExpo (needs --yes)")
    load_parser = sub.add_parser("load", parents=[common],
                                 help="call svc->load_one for a LExpo file "
                                      "(needs --yes)")
    group = load_parser.add_mutually_exclusive_group()
    group.add_argument("--path",
                       help=r"camera path, e.g. \fpSup\30LEXPO.BIN "
                            f"(<= {PATH_BUF_BYTES} bytes with NUL)")
    group.add_argument("--path-addr", type=lambda s: int(s, 0),
                       help="address of an existing NUL-terminated path")
    load_parser.add_argument(
        "--force-scratch", action="store_true",
        help="overwrite a path scratch left by a crash with foreign data")
    cycle_parser = sub.add_parser("cycle", parents=[common],
                                  help="reload N times and verify both sites "
                                       "(needs --yes)")
    cycle_parser.add_argument("--n", type=int, default=1, metavar="N",
                              help="iterations (default 1)")
    stress_parser = sub.add_parser("stress", parents=[common],
                                   help="timed reload loop (needs --yes)")
    stress_parser.add_argument("--n", type=int, default=10, metavar="N",
                               help="iterations (default 10)")
    return ap


# =============================================================================
# Plan / preflight
# =============================================================================
def _plan_lines(args: argparse.Namespace) -> list[str]:
    layout = LAYOUTS[args.svc_layout]
    code = f"0x{args.code_addr:08X}"
    lines = [
        f"plan: {args.command} LExpo  (service layout: {args.svc_layout})",
        f"  loader     : SL_BOOK_AT 0x{SL_BOOK_AT:08X} -> svc (read now)",
        f"  callfn     : {code}, parameter block 0x{P_BLOCK:08X}",
        f"  borrows    : echo handler 0x{ECHO_SLOT:08X} -> {code}, "
        f"restored to 0x{ECHO_ORIG:08X}",
    ]
    if args.command in ("cycle", "stress"):
        lines.append(f"  iterations : {args.n}")
    if args.command in ("reload", "cycle", "stress"):
        lines += [
            f"  call       : svc->reload = *(svc + 0x{layout.reload:X} "
            f"/ {layout.reload}), r0=svc, r1=0x{LXPO_ID:08X} ('LXPO')",
            "  expect     : 0xC007F324 0xE309C6F4 -> 0xE309C6FC",
            "               0xC02BAD0C 0xE24DD064 -> BL into the cave "
            "(or stays stock once self-disarmed)",
        ]
    elif args.command == "release":
        lines += [
            f"  call       : svc->release = *(svc + 0x{layout.release:X} "
            f"/ {layout.release}), r0=svc, r1=0x{LXPO_ID:08X} ('LXPO')",
            "  expect     : 0xC007F324 -> 0xE309C6F4 (stock)",
            "               0xC02BAD0C -> 0xE24DD064 (stock)",
        ]
    elif args.command == "load":
        if args.path is not None:
            where = f"--path {args.path!r} -> scratch 0x{PATH_BUF:08X}"
            try:
                need = len(args.path.encode("ascii")) + 1
            except UnicodeEncodeError:
                need = PATH_BUF_BYTES + 1
            if need > PATH_BUF_BYTES:
                where += (f"  [TOO LONG: {need} > {PATH_BUF_BYTES} bytes "
                          f"with NUL; use --path-addr]")
        else:
            where = f"--path-addr 0x{args.path_addr:08X}"
        lines += [
            f"  call       : svc->load_one = *(svc + 0x{layout.load_one:X} "
            f"/ {layout.load_one}), r0=svc, r1=<path>",
            f"  path       : {where}",
            "  expect     : 0xC007F324 -> 0xE309C6FC; 0xC02BAD0C -> BL or stock",
        ]
    return lines


def _preflight(args: argparse.Namespace) -> None:
    """Reject a doomed argument before the camera is opened."""
    if args.command == "load":
        if args.path is None and args.path_addr is None:
            raise UsageError("load needs --path or --path-addr")
        if args.path is not None:
            try:
                need = len(args.path.encode("ascii")) + 1
            except UnicodeEncodeError as exc:
                raise UsageError(
                    f"path {args.path!r} is not ASCII") from exc
            if need > PATH_BUF_BYTES:
                raise UsageError(
                    f"path {args.path!r} needs {need} bytes; the scratch at "
                    f"0x{PATH_BUF:08X} holds only {PATH_BUF_BYTES} including "
                    f"the NUL.  Use --path-addr for a longer path.")
        elif args.path_addr == 0:
            raise UsageError("--path-addr is 0")
    if args.command in ("cycle", "stress") and args.n < 1:
        raise UsageError("--n must be >= 1")


# =============================================================================
# main
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if args.command == "doctor":
        return _cmd_doctor(args)

    if args.command in WRITE_COMMANDS:
        stream = sys.stderr if args.json else sys.stdout
        for line in _plan_lines(args):
            print(line, file=stream)
        stream.flush()
        if not args.yes:
            return _fail(UsageError("write commands need --yes"),
                         args.json, EXIT_USAGE)
        try:
            _preflight(args)
        except UsageError as exc:
            return _fail(exc, args.json, EXIT_USAGE)

    link = None
    try:
        link = open_link(args.backend, timeout_ms=args.timeout,
                         retries=args.retries)
        return _run(link, args)
    except UsageError as exc:
        return _fail(exc, args.json, EXIT_USAGE)
    except TransportError as exc:
        return _fail(exc, args.json, EXIT_USAGE)
    except VerifyError as exc:
        return _fail(exc, args.json, EXIT_VERIFY)
    except KeyboardInterrupt:
        return 130
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
