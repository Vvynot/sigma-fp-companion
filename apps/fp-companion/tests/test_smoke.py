from pathlib import Path

from fp_companion.core.card import CardBuildRequest, validate
from fp_companion.core.lut import parse_cube


def test_card_validation_rejects_other_firmware() -> None:
    result = validate(CardBuildRequest(target="E:/", firmware="5.00"))
    assert not result.ok
    assert any("5.02" in error for error in result.errors)


def test_parse_identity_cube(tmp_path: Path) -> None:
    cube = tmp_path / "identity.cube"
    cube.write_text(
        "TITLE \"identity\"\nLUT_3D_SIZE 2\n"
        "0 0 0\n1 0 0\n0 1 0\n1 1 0\n"
        "0 0 1\n1 0 1\n0 1 1\n1 1 1\n",
        encoding="utf-8",
    )
    lut = parse_cube(cube)
    assert lut.title == "identity"
    assert lut.size_3d == 2
    assert len(lut.data) == 8
