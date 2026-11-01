from fp_companion.core.device import discover


def main() -> int:
    devices = discover()
    if not devices:
        print("No SIGMA camera found. Expected fp 1003:C432 or fp L 1003:C442.")
        return 0
    for device in devices:
        print(f"{device.model.value} bus={device.bus} address={device.address} serial={device.serial}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
