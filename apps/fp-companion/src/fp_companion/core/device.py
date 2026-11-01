from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

SIGMA_VENDOR_ID = 0x1003
FP_PRODUCT_ID = 0xC432
FP_L_PRODUCT_ID = 0xC442


class CameraModel(str, Enum):
    FP = "fp"
    FP_L = "fp L"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CameraInfo:
    model: CameraModel
    vendor_id: int
    product_id: int
    bus: int | None = None
    address: int | None = None
    serial: str | None = None


def model_for_ids(vendor_id: int, product_id: int) -> CameraModel:
    if vendor_id != SIGMA_VENDOR_ID:
        return CameraModel.UNKNOWN
    if product_id == FP_PRODUCT_ID:
        return CameraModel.FP
    if product_id == FP_L_PRODUCT_ID:
        return CameraModel.FP_L
    return CameraModel.UNKNOWN


def discover() -> list[CameraInfo]:
    try:
        import usb.core
    except ImportError:
        return []

    try:
        devices = usb.core.find(find_all=True, idVendor=SIGMA_VENDOR_ID)
    except (usb.core.NoBackendError, usb.core.USBError):
        return []

    found: list[CameraInfo] = []
    for dev in devices:
        model = model_for_ids(dev.idVendor, dev.idProduct)
        if model is CameraModel.UNKNOWN:
            continue
        found.append(
            CameraInfo(
                model=model,
                vendor_id=dev.idVendor,
                product_id=dev.idProduct,
                bus=getattr(dev, "bus", None),
                address=getattr(dev, "address", None),
                serial=getattr(dev, "serial_number", None),
            )
        )
    return found
