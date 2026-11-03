"""Tests for the dark-frame / hot-pixel calibration core.

Everything here is synthetic: no camera, no rawpy file dependency and no
real DNG.  The one file-loading path exercised by ``correct_frame`` is
replaced with a pre-built :class:`DarkFrame`; the CLI ``build`` loader is
replaced with a scripted function.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fp_companion.dark import cli
from fp_companion.dark import correct as correct_module
from fp_companion.dark.correct import (
    correct_frame,
    repair_hot_pixels,
    subtract_master,
)
from fp_companion.dark.master import (
    HotPixelMap,
    amp_glow_profile,
    black_level_curve,
    build_master,
    hot_pixel_map,
    load_master,
    save_master,
)
from fp_companion.dark.model import (
    DarkFrame,
    merge_metadata,
    metadata_from_exif_tags,
    read_metadata,
)
from fp_companion.dark.plan import build_plan, plan_to_markdown, write_plan

BASE = 512
HOT_PIXELS = ((5, 7, 4000), (20, 30, 2500))
HEIGHT, WIDTH = 32, 40


def _frames(
    count: int = 3,
    *,
    seed: int = 7,
    noise: float = 1.2,
) -> list[DarkFrame]:
    """Repeated dark frames with two fixed hot pixels and mild read noise."""
    rng = np.random.default_rng(seed)
    frames: list[DarkFrame] = []
    for index in range(count):
        array = (BASE + rng.normal(0.0, noise, (HEIGHT, WIDTH))).astype(np.int32)
        for x, y, delta in HOT_PIXELS:
            array[y, x] = BASE + delta
        array = np.clip(array, 0, 65535).astype(np.uint16)
        frames.append(
            DarkFrame(
                Path(f"frame{index}.DNG"),
                array,
                iso=800,
                exposure_s=40.0,
                temperature_c=21.0,
            )
        )
    return frames


# -- model ---------------------------------------------------------------


def test_darkframe_fills_and_validates_dimensions() -> None:
    array = np.zeros((4, 6), dtype=np.uint16)
    frame = DarkFrame(Path("a.DNG"), array)

    assert frame.raw.dtype == np.uint16
    assert (frame.height, frame.width) == (4, 6)
    assert frame.metadata()["path"].endswith("a.DNG")

    with pytest.raises(ValueError, match="width"):
        DarkFrame(Path("a.DNG"), array, width=5)


def test_metadata_from_exif_tags_parses_common_tags() -> None:
    meta = metadata_from_exif_tags(
        {
            "EXIF ISOSpeedRatings": [3200],
            "EXIF ExposureTime": "300/1",
            "EXIF Temperature": 23.5,
            "EXIF ExifImageWidth": 6000,
            "EXIF ExifImageLength": 4000,
        }
    )

    assert meta["iso"] == 3200
    assert meta["exposure_s"] == pytest.approx(300.0)
    assert meta["temperature_c"] == pytest.approx(23.5)
    assert (meta["width"], meta["height"]) == (6000, 4000)


def test_merge_metadata_uses_rawpy_fallback() -> None:
    exif = metadata_from_exif_tags(
        {"EXIF ISOSpeedRatings": [800], "EXIF ExposureTime": "40/1"}
    )
    assert exif == {"iso": 800, "exposure_s": 40.0}

    merged = merge_metadata(
        exif,
        {
            "width": 6000,
            "height": 4000,
            "black_level": 512.5,
            "white_level": 16383,
        },
    )

    # EXIF supplies ISO/exposure; rawpy supplies decoded dimensions/levels.
    assert merged["iso"] == 800
    assert merged["exposure_s"] == pytest.approx(40.0)
    assert merged["width"] == 6000
    assert merged["height"] == 4000
    assert merged["black_level"] == pytest.approx(512.5)
    assert merged["white_level"] == 16383


def test_read_metadata_falls_back_to_rawpy() -> None:
    class FakeSizes:
        width = 6000
        height = 4000

    class FakeRaw:
        sizes = FakeSizes()
        black_level_per_channel = [512, 512, 512, 512]
        white_level = 16383

    meta = read_metadata(Path("missing.DNG"), tags={}, raw=FakeRaw())

    assert meta["exifread"] == "provided"
    assert meta["rawpy"] is True
    assert (meta["width"], meta["height"]) == (6000, 4000)
    assert meta["black_level"] == pytest.approx(512.0)
    assert meta["white_level"] == 16383
    assert meta.get("iso") is None
    assert meta.get("temperature_c") is None


# -- master --------------------------------------------------------------


def test_build_master_median_rejects_transient() -> None:
    array = np.full((8, 8), BASE, dtype=np.uint16)
    frames = [DarkFrame(Path(str(index)), array.copy()) for index in range(3)]
    frames[1].raw[2, 3] = 60000

    master = build_master(frames)

    assert master.dtype == np.float32
    assert master.shape == (8, 8)
    assert master[2, 3] == pytest.approx(BASE)
    assert float(np.median(master)) == pytest.approx(BASE)


def test_build_master_rejects_empty_and_mismatched_stacks() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_master([])

    a = np.zeros((4, 4), dtype=np.uint16)
    b = np.zeros((2, 2), dtype=np.uint16)
    with pytest.raises(ValueError, match="shape"):
        build_master([a, b])


def test_hot_pixel_map_finds_known_pixels() -> None:
    hot = hot_pixel_map(_frames())

    assert isinstance(hot, HotPixelMap)
    assert hot.mask.dtype == bool
    assert hot.mask.shape == (HEIGHT, WIDTH)
    assert hot.count == int(hot.mask.sum())
    assert hot.total_pixels == HEIGHT * WIDTH
    assert hot.count == len(HOT_PIXELS)
    assert hot.fraction == pytest.approx(hot.count / hot.total_pixels)

    for x, y, delta in HOT_PIXELS:
        assert hot.mask[y, x]
        assert (x, y, BASE + delta) in hot.values

    assert not hot.mask[0, 0]

    payload = hot.to_dict(limit=1)
    assert payload["count"] == len(HOT_PIXELS)
    assert payload["truncated"] is True
    assert len(payload["pixels"]) == 1


def test_hot_pixel_map_rejects_bad_threshold() -> None:
    frames = _frames()
    with pytest.raises(ValueError, match="threshold_sigma"):
        hot_pixel_map(frames, threshold_sigma=0)
    with pytest.raises(ValueError, match="block"):
        hot_pixel_map(frames, block=0)


def test_amp_glow_profile_row_and_column_medians() -> None:
    master = np.full((8, 10), 10.0, dtype=np.float32)
    master[-1, :] = 100.0

    profile = amp_glow_profile(master)

    assert len(profile["row_medians"]) == 8
    assert len(profile["column_medians"]) == 10
    assert profile["row_medians"][-1] == pytest.approx(100.0)
    assert profile["row_peak"]["index"] == 7
    assert profile["row_peak"]["value"] == pytest.approx(100.0)
    assert profile["edges"]["bottom"] == pytest.approx(100.0)
    assert profile["edge_to_centre_ratio"] is not None

    with pytest.raises(ValueError, match="edge_fraction"):
        amp_glow_profile(master, edge_fraction=0)


def test_black_level_curve_groups_by_iso_and_exposure() -> None:
    frames = _frames()
    curve = black_level_curve(frames)

    assert curve["frame_count"] == 3
    assert len(curve["groups"]) == 1
    group = curve["groups"][0]
    assert group["iso"] == 800
    assert group["exposure_s"] == pytest.approx(40.0)
    assert group["count"] == 3
    assert group["observed_black_level"] == pytest.approx(BASE, abs=2.0)

    other = DarkFrame(
        Path("other.DNG"),
        frames[0].raw.copy(),
        iso=3200,
        exposure_s=120.0,
    )
    curve = black_level_curve(frames + [other])
    assert [group["iso"] for group in curve["groups"]] == [800, 3200]
    assert [group["exposure_s"] for group in curve["groups"]] == [40.0, 120.0]


def test_save_and_load_master_roundtrip(tmp_path: Path) -> None:
    frames = _frames()
    master = build_master(frames)
    hot = hot_pixel_map(frames)

    path = save_master(tmp_path / "master", master, hot, metadata={"iso": 800})
    assert path.name == "master.npz"

    loaded = load_master(path)
    np.testing.assert_allclose(loaded["master"], master)
    np.testing.assert_array_equal(loaded["hot_mask"], hot.mask)
    assert loaded["metadata"] == {"iso": 800}
    assert loaded["hot_values"].shape == (hot.count, 3)


# -- correction ----------------------------------------------------------


def test_subtract_master_clips_and_preserves_shape() -> None:
    raw = np.full((4, 4), 1000, dtype=np.uint16)
    master = np.full((4, 4), 300.0, dtype=np.float32)
    master[0, 0] = 1200.0

    corrected = subtract_master(raw, master)

    assert corrected.shape == raw.shape
    assert corrected.dtype == np.uint16
    assert corrected[0, 0] == 0  # clipped, not wrapped
    assert corrected[1, 1] == 700

    with pytest.raises(ValueError, match="shape"):
        subtract_master(raw, master[:2])


def test_repair_hot_pixels_uses_neighbour_median() -> None:
    raw = np.full((5, 5), 100, dtype=np.uint16)
    raw[2, 2] = 60000
    mask = np.zeros((5, 5), dtype=bool)
    mask[2, 2] = True

    repaired = repair_hot_pixels(raw, mask)

    assert repaired[2, 2] == 100
    assert raw[2, 2] == 60000  # input untouched
    assert repaired.dtype == np.uint16
    np.testing.assert_array_equal(repaired[~mask], raw[~mask])

    with pytest.raises(ValueError, match="mask shape"):
        repair_hot_pixels(raw, np.zeros((2, 2), dtype=bool))


@pytest.mark.parametrize("suffix", [".tif", ".png"])
def test_correct_frame_exports_16bit_image_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    array = np.full((8, 8), 500, dtype=np.uint16)
    array[3, 3] = 3000
    frame = DarkFrame(
        tmp_path / "synthetic.DNG",
        array,
        iso=800,
        exposure_s=40.0,
        temperature_c=21.0,
    )
    monkeypatch.setattr(
        correct_module.DarkFrame,
        "from_path",
        classmethod(lambda cls, path: frame),
    )

    master = np.full((8, 8), 500.0, dtype=np.float32)
    mask = np.zeros((8, 8), dtype=bool)
    mask[3, 3] = True
    out_path = tmp_path / f"corrected{suffix}"

    report = correct_frame(
        tmp_path / "synthetic.DNG", master, mask, out_path=out_path
    )

    assert report["width"] == 8
    assert report["height"] == 8
    assert report["hot_pixels_repaired"] == 1
    assert report["clipped_low_pixels"] == 0
    assert out_path.is_file()

    from PIL import Image

    with Image.open(out_path) as image:
        assert image.size == (8, 8)
        exported = np.array(image)
    assert exported.dtype == np.uint16
    assert exported[3, 3] == 0  # hot pixel replaced after offset subtraction

    report_path = tmp_path / "corrected.json"
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["output"] == str(out_path)
    assert payload["master"]["shape"] == [8, 8]


def test_correct_frame_rejects_mismatched_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = DarkFrame(tmp_path / "synthetic.DNG", np.zeros((4, 4), np.uint16))
    monkeypatch.setattr(
        correct_module.DarkFrame,
        "from_path",
        classmethod(lambda cls, path: frame),
    )

    with pytest.raises(ValueError, match="master shape"):
        correct_frame(
            tmp_path / "synthetic.DNG",
            np.zeros((2, 2), np.float32),
            None,
            out_path=tmp_path / "out.tif",
        )


# -- plan ----------------------------------------------------------------


def test_plan_structure_counts_and_markdown() -> None:
    plan = build_plan(
        isos=[100, 800],
        exposures_s=[40, 120],
        temperatures_c=[20, 25],
        frames_per_target=2,
    )

    assert plan["schema"] == "fp-dark-plan/1"
    assert plan["counts"] == {
        "group_targets": 8,
        "frames_per_target": 2,
        "targets": 16,
    }
    assert len(plan["targets"]) == 16
    assert len({target["filename"] for target in plan["targets"]}) == 16

    settings = json.dumps(plan["camera_settings"]).lower()
    assert "long-exposure noise reduction" in settings
    assert "off" in settings
    assert "lens cap" in settings
    assert any("temperature" in item.lower() for item in plan["checklist"])

    markdown = plan_to_markdown(plan)
    assert "# " in markdown
    assert "- [ ]" in markdown
    assert "## Camera settings" in markdown
    assert "dark_iso100_40s_20C_01.DNG" in markdown
    assert "| 1 | 100 | 40 | 20 | 1/2 |" in markdown


def test_plan_defaults_cover_requested_presets() -> None:
    plan = build_plan()

    assert plan["isos"] == [100, 800, 3200]
    assert plan["exposures_s"] == [40.0, 120.0, 300.0, 500.0]
    assert plan["temperatures_c"] == [20.0]
    assert plan["counts"]["targets"] == 3 * 4 * 1 * 2


def test_plan_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        build_plan(isos=[0])
    with pytest.raises(ValueError, match="positive"):
        build_plan(exposures_s=[0])
    with pytest.raises(ValueError, match="at least 1"):
        build_plan(frames_per_target=0)


def test_write_plan_json_and_markdown(tmp_path: Path) -> None:
    plan = build_plan(isos=[800], exposures_s=[40], temperatures_c=[20])

    json_path = write_plan(plan, tmp_path / "plan.json")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["counts"]["targets"] == 2

    markdown_path = write_plan(plan, tmp_path / "plan.md")
    text = markdown_path.read_text(encoding="utf-8")
    assert "## Checklist" in text
    assert "dark_iso800_40s_20C_02.DNG" in text

    with pytest.raises(ValueError, match="unsupported plan format"):
        write_plan(plan, tmp_path / "plan.txt")


# -- CLI -----------------------------------------------------------------


def test_cli_plan_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    code = cli.main(
        [
            "plan",
            "--out",
            str(out),
            "--iso",
            "800",
            "--exposure",
            "40",
            "--temperature",
            "20",
            "--frames",
            "1",
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["counts"]["targets"] == 1
    assert payload["targets"][0]["filename"] == "dark_iso800_40s_20C_01.DNG"


def test_cli_inspect_metadata_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = tmp_path / "fake.DNG"
    fake.write_bytes(b"not a real DNG")

    # Simulates EXIF missing exposure/temperature and rawpy supplying dims.
    metadata = {
        "path": str(fake),
        "width": 6000,
        "height": 4000,
        "iso": 800,
        "exposure_s": 40.0,
        "temperature_c": None,
        "black_level": 512.0,
        "white_level": 65535,
        "exifread": "provided",
        "rawpy": True,
    }
    monkeypatch.setattr(cli, "read_metadata", lambda path, **kwargs: dict(metadata))

    code = cli.main(["inspect", str(fake), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["width"] == 6000
    assert output["exposure_s"] == 40.0
    assert output["temperature_c"] is None


def test_cli_inspect_missing_file_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["inspect", str(tmp_path / "missing.DNG")]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_build_with_synthetic_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = _frames()
    for index, frame in enumerate(frames):
        (tmp_path / f"dark{index}.DNG").write_bytes(b"synthetic")

    def fake_load(path: Path) -> DarkFrame:
        index = int(path.stem.removeprefix("dark"))
        source = frames[index]
        return DarkFrame(
            path,
            source.raw.copy(),
            iso=source.iso,
            exposure_s=source.exposure_s,
            temperature_c=source.temperature_c,
        )

    monkeypatch.setattr(cli, "_load_frame", fake_load)
    out_dir = tmp_path / "out"

    code = cli.main(["build", str(tmp_path), "--out", str(out_dir)])

    assert code == 0
    master_path = out_dir / "master.npz"
    assert master_path.is_file()

    loaded = load_master(master_path)
    assert loaded["master"].shape == (HEIGHT, WIDTH)
    assert loaded["hot_mask"] is not None
    for x, y, _delta in HOT_PIXELS:
        assert loaded["hot_mask"][y, x]

    report = json.loads(
        (out_dir / "build-report.json").read_text(encoding="utf-8")
    )
    assert report["frame_count"] == 3
    assert report["hot_pixels"]["count"] >= len(HOT_PIXELS)
    assert report["black_level_curve"]["frame_count"] == 3
    assert report["master"]["width"] == WIDTH


def test_cli_build_missing_directory_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["build", str(tmp_path / "nope"), "--out", str(tmp_path)]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_build_without_dng_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["build", str(tmp_path), "--out", str(tmp_path / "out")]) == 1
    assert "no .DNG" in capsys.readouterr().err


def test_cli_correct_with_synthetic_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    array = np.full((8, 8), 500, dtype=np.uint16)
    array[2, 2] = 3000
    frame = DarkFrame(
        tmp_path / "frame.DNG", array, iso=800, exposure_s=40.0
    )
    source = tmp_path / "frame.DNG"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        correct_module.DarkFrame,
        "from_path",
        classmethod(lambda cls, path: frame),
    )

    master = np.full((8, 8), 500.0, dtype=np.float32)
    mask = np.zeros((8, 8), dtype=bool)
    mask[2, 2] = True
    master_path = save_master(tmp_path / "master.npz", master, mask)
    out_path = tmp_path / "corrected.tif"

    code = cli.main(
        [
            "correct",
            str(source),
            "--master",
            str(master_path),
            "--out",
            str(out_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["hot_pixels_repaired"] == 1
    assert out_path.is_file()
    assert (tmp_path / "corrected.json").is_file()


def test_cli_correct_requires_existing_master(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "frame.DNG"
    source.write_bytes(b"synthetic")
    code = cli.main(
        [
            "correct",
            str(source),
            "--master",
            str(tmp_path / "missing.npz"),
            "--out",
            str(tmp_path / "out.tif"),
        ]
    )
    assert code == 2
    assert "master not found" in capsys.readouterr().err
