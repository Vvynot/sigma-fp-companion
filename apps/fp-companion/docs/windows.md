# Windows toolchain

The fpSup upstream host tools use a Unix-socket daemon (`fpshd`) and do not run
on native Windows. This tree uses a direct pyusb transport instead.

## Requirements

- Windows 10/11, Python 3.11+.
- WinUSB bound to the camera interface. Use Zadig once: Options → List All
  Devices → `1003:c432` → WinUSB. Replug after binding.
- `libusb-1.0.dll` on the DLL search path. The conda env provides it under
  `Library\bin`; `fp_companion.autotest.session` adds it automatically.
- LLVM/clang is optional. The 140-byte `callfn.S` bridge image is embedded in
  `fp_companion.hotload`, so `fp-hotload` runs without LLVM or a firmware
  checkout. clang is only needed to rebuild the bridge from source; `doctor`
  reports it as an optional rebuild aid.

## Check

```bash
fp-hotload doctor
```

It reports the shell version, `SL_BOOK_AT`, `svc.version`, patch/hook site
states, optional clang, `libusb-1.0.dll`, the USB device, and the best-effort
WinUSB binding. If no device is found it prints the Zadig/WinUSB steps and
exits 2.

```bash
fp-hotload doctor
# if the console script is not installed:
PYTHONPATH=src python -m fp_companion.hotload doctor
```

## Build compatibility

- `fp_usb_shell/cave.py` imports `fcntl` only when available; on Windows the
  advisory file lock is skipped. Do not ship a module named `fcntl.py` — it
  would shadow the real module on POSIX.
- `build_autorun.py` creates the parent directory for a `fpSup\LOADER.BIN`
  bin-name on Windows.
- Shell commands use FPSH v1 frames (64-byte header/payload, CRC32) on EP
  `0x01`/`0x82`; no AF_UNIX socket, no macOS-only helper binary.

## Limits

- The driver binding itself is a user action (Zadig or an INF). The tool only
  detects and guides; it does not install a signed driver.
- A camera in Storage/PTP/UVC mode uses class drivers and cannot be reached by
  WinUSB. Switch back to Camera Control, then replug.
