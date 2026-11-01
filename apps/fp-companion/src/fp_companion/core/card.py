from __future__ import annotations

from dataclasses import dataclass, field


class CardValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CardBuildRequest:
    target: str
    firmware: str
    modules: tuple[str, ...] = field(default_factory=tuple)
    loader_version: int = 4


@dataclass(frozen=True)
class CardValidation:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def validate(request: CardBuildRequest) -> CardValidation:
    errors: list[str] = []
    warnings: list[str] = []

    if request.firmware != "5.02":
        errors.append("Only SIGMA fp firmware 5.02 is supported by fpSup cards.")
    if not request.target:
        errors.append("No SD card target selected.")
    if request.loader_version not in (4, 6):
        errors.append("Loader version must be 4 or 6.")
    if not request.modules:
        warnings.append("No modules selected; the card will contain only the loader/UI.")

    return CardValidation(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))


def build(request: CardBuildRequest) -> None:
    validation = validate(request)
    if not validation.ok:
        raise CardValidationError("; ".join(validation.errors))
    raise NotImplementedError("Card writing will be implemented in Card Studio MVP.")
