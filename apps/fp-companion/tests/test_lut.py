from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pytest

from fp_companion.core import lut_card
from fp_companion.core.lut import (
    ALLOWED_3D_SIZES,
    GAMMAS,
    MAX_1D_SIZE,
    MAX_CUBE_BYTES,
    CubeFormatError,
    CubeLUT,
    LutSettings,
    parse_cube,
    validate_cube,
    validate_settings,
)
from fp_companion.core.lut_card import (
    LUT_DIR_NAME,
    SETTINGS_NAME,
    SOURCE_NAME,
    write_lut_staging,
)
from fp_companion.core.plan import PROJECT_ROOT

VALID_SETTINGS = LutSettings("Rec.709", "Gamma 2.4", 0.5)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _error_codes(issues) -> set[str]:
    return {issue.code for issue in issues if issue.severity == "error"}


def _warning_codes(issues) -> set[str]:
    return {issue.code for issue in issues if issue.severity == "warning"}


def _write_3d_cube(
    path: Path,
    size: int = 2,
    *,
    title: str = "identity",
    rows: list[str] | None = None,
) -> Path:
    if rows is None:
        rows = []
        for blue in range(size):
            for green in range(size):
                for red in range(size):
                    divisor = max(size - 1, 1)
                    rows.append(
                        f"{red / divisor} {green / divisor} {blue / divisor}"
                    )
    text = (
        f'TITLE "{title}"\n'
        f"LUT_3D_SIZE {size}\n"
        + "\n".join(rows)
        + "\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def _write_1d_cube(path: Path, values: list[tuple[float, float, float]]) -> Path:
    lines = [f"{red} {green} {blue}" for red, green, blue in values]
    path.write_text(
        'TITLE "ramp"\n'
        f"LUT_1D_SIZE {len(values)}\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )
    return path


def _lut(
    *,
    size_1d: int = 0,
    size_3d: int = 0,
    data: tuple[tuple[float, float, float], ...] | None = None,
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0),
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0),
    source_bytes: int = 0,
) -> CubeLUT:
    if data is None:
        count = size_1d if size_1d else max(size_3d, 0) ** 3
        data = ((0.0, 0.0, 0.0),) * count
    return CubeLUT(
        title="synthetic",
        size_1d=size_1d,
        size_3d=size_3d,
        domain_min=domain_min,
        domain_max=domain_max,
        data=data,
        source_bytes=source_bytes,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# parse_cube
# ---------------------------------------------------------------------------


def test_parse_1d_cube(tmp_path: Path) -> None:
    cube = _write_1d_cube(
        tmp_path / "ramp.cube",
        [(0.0, 0.0, 0.0), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0)],
    )

    lut = parse_cube(cube)

    assert lut.title == "ramp"
    assert lut.size_1d == 3
    assert lut.size_3d == 0
    assert lut.data == ((0.0, 0.0, 0.0), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0))
    assert lut.domain_min == (0.0, 0.0, 0.0)
    assert lut.domain_max == (1.0, 1.0, 1.0)
    assert lut.source_bytes == cube.stat().st_size
    assert validate_cube(lut) == ()


def test_parse_3d_cube(tmp_path: Path) -> None:
    cube = _write_3d_cube(tmp_path / "identity.cube", 2)

    lut = parse_cube(cube)

    assert lut.title == "identity"
    assert lut.size_1d == 0
    assert lut.size_3d == 2
    assert len(lut.data) == 8
    assert lut.data[0] == (0.0, 0.0, 0.0)
    assert lut.data[-1] == (1.0, 1.0, 1.0)


def test_parse_domain(tmp_path: Path) -> None:
    path = tmp_path / "domain.cube"
    path.write_text(
        "LUT_1D_SIZE 1\n"
        "DOMAIN_MIN -1 0 0\n"
        "DOMAIN_MAX 1 2 4\n"
        "0.5 0.5 0.5\n",
        encoding="utf-8",
    )

    lut = parse_cube(path)

    assert lut.domain_min == (-1.0, 0.0, 0.0)
    assert lut.domain_max == (1.0, 2.0, 4.0)


def test_parse_1d_length_mismatch_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.cube"
    path.write_text("LUT_1D_SIZE 3\n0 0 0\n1 1 1\n", encoding="utf-8")

    with pytest.raises(CubeFormatError):
        parse_cube(path)


def test_parse_3d_length_mismatch_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.cube"
    path.write_text(
        "LUT_3D_SIZE 2\n"
        + "0 0 0\n" * 7,
        encoding="utf-8",
    )

    with pytest.raises(CubeFormatError):
        parse_cube(path)


def test_parse_missing_size_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.cube"
    path.write_text("0 0 0\n1 1 1\n", encoding="utf-8")

    with pytest.raises(CubeFormatError):
        parse_cube(path)


def test_parse_non_finite_values_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "nan.cube"
    path.write_text(
        "LUT_1D_SIZE 2\nnan 0 0\ninf 1 1\n",
        encoding="utf-8",
    )

    lut = parse_cube(path)
    issues = validate_cube(lut)

    assert math.isnan(lut.data[0][0])
    assert math.isinf(lut.data[1][0])
    assert "nonfinite" in _error_codes(issues)


# ---------------------------------------------------------------------------
# validate_cube
# ---------------------------------------------------------------------------


def test_valid_33_cube_has_no_issues() -> None:
    assert validate_cube(_lut(size_3d=33)) == ()


def test_valid_1d_cube_has_no_issues() -> None:
    assert validate_cube(_lut(size_1d=2)) == ()


@pytest.mark.parametrize("size", [17, 65])
def test_non_33_3d_sizes_warn_but_are_valid(size: int) -> None:
    issues = validate_cube(_lut(size_3d=size))

    assert "error" not in {issue.severity for issue in issues}
    assert "size" in _warning_codes(issues)


def test_unsupported_3d_grid_is_an_error() -> None:
    issues = validate_cube(_lut(size_3d=2))

    assert "grid" in _error_codes(issues)
    assert "2" in next(
        issue.message for issue in issues if issue.code == "grid"
    )


@pytest.mark.parametrize("size", [1, MAX_1D_SIZE])
def test_supported_1d_sizes(size: int) -> None:
    assert validate_cube(_lut(size_1d=size)) == ()


def test_1d_grid_above_4096_is_an_error() -> None:
    issues = validate_cube(_lut(size_1d=MAX_1D_SIZE + 1))
    assert "grid" in _error_codes(issues)


def test_both_1d_and_3d_is_an_error() -> None:
    issues = validate_cube(_lut(size_1d=2, size_3d=2))
    assert "grid" in _error_codes(issues)


def test_missing_grid_is_an_error() -> None:
    issues = validate_cube(_lut(data=((0.0, 0.0, 0.0),)))
    assert "grid" in _error_codes(issues)


def test_empty_data_is_an_error() -> None:
    issues = validate_cube(_lut(size_3d=33, data=()))
    assert "empty" in _error_codes(issues)


def test_data_length_mismatch_is_an_error() -> None:
    issues = validate_cube(_lut(size_3d=33, data=((0.0, 0.0, 0.0),) * 8))
    assert "length" in _error_codes(issues)


def test_non_finite_data_is_an_error() -> None:
    data = ((0.0, 0.0, 0.0), (math.nan, 0.0, 0.0))
    issues = validate_cube(_lut(size_1d=2, data=data))
    assert "nonfinite" in _error_codes(issues)


def test_non_finite_domain_is_an_error() -> None:
    issues = validate_cube(
        _lut(size_1d=1, domain_min=(0.0, math.inf, 0.0))
    )
    assert "nonfinite" in _error_codes(issues)


@pytest.mark.parametrize(
    "domain_min, domain_max",
    [
        ((0.5, 0.0, 0.0), (0.5, 1.0, 1.0)),
        ((0.0, 0.0, 0.0), (0.0, 1.0, 1.0)),
        ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0)),
    ],
)
def test_domain_min_at_or_above_max_is_an_error(
    domain_min: tuple[float, float, float],
    domain_max: tuple[float, float, float],
) -> None:
    issues = validate_cube(
        _lut(size_1d=1, domain_min=domain_min, domain_max=domain_max)
    )
    assert "domain" in _error_codes(issues)


def test_large_file_warns_but_does_not_block() -> None:
    lut = _lut(size_1d=1, source_bytes=MAX_CUBE_BYTES + 1)

    issues = validate_cube(lut)

    assert "error" not in {issue.severity for issue in issues}
    assert "filesize" in _warning_codes(issues)


def test_file_at_4_mib_does_not_warn() -> None:
    issues = validate_cube(_lut(size_1d=1, source_bytes=MAX_CUBE_BYTES))
    assert "filesize" not in _warning_codes(issues)


def test_allowed_3d_sizes_are_the_documented_set() -> None:
    assert ALLOWED_3D_SIZES == (17, 33, 65)


# ---------------------------------------------------------------------------
# validate_settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        LutSettings("ACES", "Linear"),
        LutSettings("Adobe RGB", "Adobe RGB"),
        LutSettings("Blackmagic Design", "Blackmagic Design Film"),
        LutSettings("Blackmagic Design", "Blackmagic Extended Video"),
        LutSettings("P3 D60", "Gamma 2.6"),
        LutSettings("P3 D65", "Gamma 2.4"),
        LutSettings("Rec.2020", "Rec.709"),
        LutSettings("Rec.709", "Gamma 2.4"),
        LutSettings("sRGB", "sRGB"),
    ],
)
def test_valid_settings_combinations(settings: LutSettings) -> None:
    assert validate_settings(settings) == ()


@pytest.mark.parametrize("intensity", [0.0, 0.5, 1.0])
def test_intensity_boundaries_are_valid(intensity: float) -> None:
    settings = LutSettings("Rec.709", "Gamma 2.4", intensity)
    assert validate_settings(settings) == ()


def test_unknown_color_space_is_an_error() -> None:
    issues = validate_settings(LutSettings("DCI-P3", "Gamma 2.4", 1.0))
    assert "color_space" in _error_codes(issues)


def test_unknown_gamma_is_an_error() -> None:
    issues = validate_settings(LutSettings("Rec.709", "LogC3", 1.0))
    assert "gamma" in _error_codes(issues)


@pytest.mark.parametrize("intensity", [-0.01, 1.01, math.nan, math.inf])
def test_intensity_out_of_range_is_an_error(intensity: float) -> None:
    issues = validate_settings(LutSettings("Rec.709", "Gamma 2.4", intensity))
    assert "intensity" in _error_codes(issues)


def test_blackmagic_gammas_are_only_for_blackmagic_color_space() -> None:
    for gamma in ("Blackmagic Design Film", "Blackmagic Extended Video"):
        issues = validate_settings(LutSettings("Rec.709", gamma, 1.0))
        assert "gamma_group" in _error_codes(issues)


def test_standard_gammas_are_not_allowed_for_blackmagic_color_space() -> None:
    issues = validate_settings(LutSettings("Blackmagic Design", "Gamma 2.4", 1.0))
    assert "gamma_group" in _error_codes(issues)


def test_gamma_list_matches_product_contract() -> None:
    assert set(GAMMAS) == {
        "Adobe RGB",
        "Gamma 2.4",
        "Gamma 2.6",
        "Linear",
        "Rec.709",
        "sRGB",
        "Blackmagic Design Film",
        "Blackmagic Extended Video",
    }


# ---------------------------------------------------------------------------
# write_lut_staging
# ---------------------------------------------------------------------------


@pytest.fixture()
def staging(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "staging"
    target.mkdir()
    cube = _write_3d_cube(tmp_path / "input.cube", 2)
    return target, cube


def test_staging_happy_path(staging: tuple[Path, Path]) -> None:
    target, cube = staging
    source_bytes = cube.read_bytes()
    expected_sha = _sha256(source_bytes)

    report = write_lut_staging(target, cube, VALID_SETTINGS)

    assert report.ok
    assert report.layout == "staging"
    assert report.bootable is False
    assert report.target == target
    assert report.cube_sha256 == expected_sha
    assert report.files == (
        f"{LUT_DIR_NAME}/{SOURCE_NAME}",
        f"{LUT_DIR_NAME}/{SETTINGS_NAME}",
    )
    assert report.issues == ()

    copied = target / LUT_DIR_NAME / SOURCE_NAME
    assert copied.read_bytes() == source_bytes
    # The original cube is never modified.
    assert cube.read_bytes() == source_bytes

    payload = json.loads(
        (target / LUT_DIR_NAME / SETTINGS_NAME).read_text(encoding="utf-8")
    )
    assert payload["layout"] == "staging"
    assert payload["bootable"] is False
    assert "not a bootable" in payload["note"].lower()
    assert payload["title"] == "identity"
    assert payload["size_1d"] == 0
    assert payload["size_3d"] == 2
    assert payload["grid"] == {"1d": 0, "3d": 2}
    assert payload["domain_min"] == [0.0, 0.0, 0.0]
    assert payload["domain_max"] == [1.0, 1.0, 1.0]
    assert payload["settings"] == {
        "input_color_space": "Rec.709",
        "input_gamma": "Gamma 2.4",
        "intensity": 0.5,
    }
    assert payload["sha256"] == expected_sha


def test_staging_records_1d_grid(tmp_path: Path) -> None:
    target = tmp_path / "staging"
    target.mkdir()
    cube = _write_1d_cube(
        tmp_path / "ramp.cube",
        [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
    )

    report = write_lut_staging(target, cube, VALID_SETTINGS)

    assert report.ok
    payload = json.loads(
        (target / LUT_DIR_NAME / SETTINGS_NAME).read_text(encoding="utf-8")
    )
    assert payload["size_1d"] == 2
    assert payload["size_3d"] == 0
    assert payload["grid"] == {"1d": 2, "3d": 0}


def test_staging_refuses_project_tree(staging: tuple[Path, Path]) -> None:
    _target, cube = staging

    report = write_lut_staging(PROJECT_ROOT, cube, VALID_SETTINGS)

    assert not report.ok
    assert "project" in _error_codes(report.issues)
    assert report.files == ()


def test_staging_refuses_missing_target(staging: tuple[Path, Path]) -> None:
    target, cube = staging
    missing = target.parent / "does-not-exist"

    report = write_lut_staging(missing, cube, VALID_SETTINGS)

    assert not report.ok
    assert "target" in _error_codes(report.issues)


def test_staging_refuses_target_that_is_a_file(
    staging: tuple[Path, Path], tmp_path: Path
) -> None:
    _target, cube = staging
    file_target = tmp_path / "not-a-directory"
    file_target.write_text("x", encoding="utf-8")

    report = write_lut_staging(file_target, cube, VALID_SETTINGS)

    assert not report.ok
    assert "target" in _error_codes(report.issues)


@pytest.mark.skipif(os.name != "nt", reason="drive-root semantics are Windows-only")
def test_staging_refuses_system_drive_root(staging: tuple[Path, Path]) -> None:
    _target, cube = staging
    drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
    root = Path(f"{drive}\\")
    if not root.is_dir():
        pytest.skip(f"system drive root is unavailable: {root}")

    report = write_lut_staging(root, cube, VALID_SETTINGS)

    assert not report.ok
    assert "root" in _error_codes(report.issues)
    assert report.files == ()


def test_staging_refuses_invalid_settings(staging: tuple[Path, Path]) -> None:
    target, cube = staging
    settings = LutSettings("Rec.709", "Gamma 2.4", 1.5)

    report = write_lut_staging(target, cube, settings)

    assert not report.ok
    assert "intensity" in _error_codes(report.issues)
    assert not (target / LUT_DIR_NAME).exists()


def test_staging_refuses_unparsable_cube(
    staging: tuple[Path, Path], tmp_path: Path
) -> None:
    target, _cube = staging
    bad = tmp_path / "bad.cube"
    bad.write_text("0 0 0\n", encoding="utf-8")

    report = write_lut_staging(target, bad, VALID_SETTINGS)

    assert not report.ok
    assert "cube" in _error_codes(report.issues)
    assert not (target / LUT_DIR_NAME / SOURCE_NAME).exists()


def test_staging_refuses_missing_cube(
    staging: tuple[Path, Path], tmp_path: Path
) -> None:
    target, _cube = staging

    report = write_lut_staging(target, tmp_path / "nope.cube", VALID_SETTINGS)

    assert not report.ok
    assert "cube" in _error_codes(report.issues)


def test_staging_hash_mismatch_fails_and_removes_copy(
    staging: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, cube = staging

    def corrupt_copy(src, dst, *args, **kwargs) -> None:
        dst.write(b"corrupted bytes")

    monkeypatch.setattr(lut_card.shutil, "copyfileobj", corrupt_copy)

    report = write_lut_staging(target, cube, VALID_SETTINGS)

    assert not report.ok
    assert "hash" in _error_codes(report.issues)
    assert report.files == ()
    assert report.cube_sha256 == _sha256(cube.read_bytes())
    assert not (target / LUT_DIR_NAME / SOURCE_NAME).exists()
    assert not (target / LUT_DIR_NAME / SETTINGS_NAME).exists()


def test_staging_does_not_touch_unrelated_target_files(
    staging: tuple[Path, Path]
) -> None:
    target, cube = staging
    (target / "DCIM").mkdir()
    (target / "DCIM" / "PHOTO.JPG").write_bytes(b"jpeg")

    report = write_lut_staging(target, cube, VALID_SETTINGS)

    assert report.ok
    assert (target / "DCIM" / "PHOTO.JPG").read_bytes() == b"jpeg"


def test_staging_replaces_previous_staged_cube(
    staging: tuple[Path, Path], tmp_path: Path
) -> None:
    target, cube = staging
    lut_dir = target / LUT_DIR_NAME
    lut_dir.mkdir()
    (lut_dir / SOURCE_NAME).write_bytes(b"old")
    (lut_dir / SETTINGS_NAME).write_text("{}", encoding="utf-8")

    report = write_lut_staging(target, cube, VALID_SETTINGS)

    assert report.ok
    assert (lut_dir / SOURCE_NAME).read_bytes() == cube.read_bytes()
    assert json.loads((lut_dir / SETTINGS_NAME).read_text(encoding="utf-8"))[
        "sha256"
    ] == _sha256(cube.read_bytes())


def test_settings_dataclass_defaults_to_full_intensity() -> None:
    assert LutSettings("Rec.709", "Gamma 2.4").intensity == 1.0


def test_cube_lut_source_bytes_is_optional() -> None:
    # Existing callers constructing CubeLUT positionally keep working.
    lut = CubeLUT(
        "manual",
        1,
        0,
        (0.0, 0.0, 0.0),
        (1.0, 1.0, 1.0),
        ((0.0, 0.0, 0.0),),
    )
    assert lut.source_bytes == 0
    assert validate_cube(lut) == ()


def test_parser_stays_permissive_for_unsupported_grids(tmp_path: Path) -> None:
    cube = _write_3d_cube(tmp_path / "small.cube", 2)
    parsed = parse_cube(cube)

    # parse_cube keeps every authored value; validate_cube is the gate.
    assert parsed.size_3d == 2
    assert "grid" in _error_codes(validate_cube(parsed))
