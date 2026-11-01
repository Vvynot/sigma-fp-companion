from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from fp_companion.core.catalog import load_builtin, save_catalog, scan_releases
from fp_companion.core.models import Catalog

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
HEX_D = "d" * 64
HEX_E = "e" * 64
HEX_F = "f" * 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = {"format", "codec", "gyro", "monitor", "utility"}
_RISKS = {"release", "alpha", "test"}

# apps/fp-companion/tests/test_catalog.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENDOR_RELEASES = _REPO_ROOT / "vendor" / "fpSup" / "fpSup" / "releases"


def _write_release(
    root: Path,
    directory: str,
    manifest: str,
    about: str | None = None,
) -> Path:
    release_dir = root / directory
    release_dir.mkdir(parents=True)
    (release_dir / "MANIFEST.txt").write_text(manifest, encoding="utf-8")
    if about is not None:
        (release_dir / "ABOUT.txt").write_text(about, encoding="utf-8")
    return release_dir


@pytest.fixture()
def synthetic_tree(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    root.mkdir()

    _write_release(
        root,
        "fpsup-og3k-v0.2.8a",
        "\n".join(
            [
                "SIGMA fp — OG3K open gate (v0.2.8a)",
                "banner: fpSup-OG3K-v0.2.8a!",
                "firmware: SIGMA fp Ver.5.02 only",
                "FPUI header: min_svc=4",
                "",
                f"AutoRun.txt  32,772B  sha256={HEX_A}",
                f"fpSup/45RAWV.BIN  38,644B  sha256={HEX_B}",
                f"reference_firmware sha256={HEX_C}",
                "",
            ]
        ),
        about="\nen: Whole 3:2 sensor, eight frame rates.\nzh: 中文摘要。\n",
    )

    _write_release(
        root,
        "fpsup-raw-view-v0.3.0test",
        "\n".join(
            [
                "SIGMA fp -- RAW view (v0.3.0test), Loader v4 card",
                "firmware: SIGMA fp Ver.5.02 only",
                "",
                f"AutoRun.txt  32,768 B  sha256={HEX_A}",
                f"fpSup/45RAWV.BIN  38,644B  sha256={HEX_B}",
                f"fpSup/UI/0.BIN  114,688B  sha256={HEX_C}",
                "FPUI header: min_svc=2",
                "LOADER.BIN gives services v4",
                "",
            ]
        ),
        about="en: RAW monitoring on the LCD.\nzh: RAW 監看。\n",
    )

    # Header only, no payload lines: must be skipped.
    _write_release(
        root,
        "fpsup-broken-v9.9.9",
        "SIGMA fp -- Broken (v9.9.9)\nfirmware: SIGMA fp Ver.5.02 only\n",
    )

    # No MANIFEST.txt but a complete card: parsed by the fallback scanner.
    fallback = root / "fpsup-gyro-base-v2.0.0"
    (fallback / "fpSup" / "UI").mkdir(parents=True)
    (fallback / "AutoRun.txt").write_bytes(b"autorun payload\n")
    (fallback / "fpSup" / "BASE.BIN").write_bytes(b"base payload")
    (fallback / "fpSup" / "UI" / "0.BIN").write_bytes(b"ui payload")
    (fallback / "ABOUT.txt").write_text(
        "\nen: Fallback gyro base.\nzh: 備援。\n", encoding="utf-8"
    )
    (fallback / "README.txt").write_text(
        "fpGyroSup Base v2.0.0 -- SIGMA fp firmware Ver.5.02 only\n",
        encoding="utf-8",
    )

    # AutoRun.txt but no payload file: must be skipped.
    empty = root / "fpsup-empty-v1.0.0"
    empty.mkdir()
    (empty / "AutoRun.txt").write_bytes(b"autorun only\n")
    (empty / "ABOUT.txt").write_text("en: no payload\n", encoding="utf-8")

    # No MANIFEST.txt: must be skipped.
    no_manifest = root / "fpsup-no-manifest-v1.0.0"
    no_manifest.mkdir()
    (no_manifest / "ABOUT.txt").write_text("en: ignored\n", encoding="utf-8")

    # A stray file in the root must not be treated as a release.
    (root / "README.md").write_text("notes\n", encoding="utf-8")
    return root


def test_scan_parses_first_release(synthetic_tree: Path) -> None:
    catalog = scan_releases(synthetic_tree)
    assert isinstance(catalog, Catalog)

    release = catalog.by_id("og3k")
    assert release.directory == "fpsup-og3k-v0.2.8a"
    assert release.version == "0.2.8a"
    assert release.name == "OG3K open gate"
    assert release.firmware == "5.02"
    assert release.loader == 3
    assert release.min_svc == 4
    assert release.category == "format"
    assert release.risk == "alpha"
    assert release.summary == "en: Whole 3:2 sensor, eight frame rates."
    assert [item.path for item in release.files] == [
        "AutoRun.txt",
        "fpSup/45RAWV.BIN",
    ]
    assert [item.size for item in release.files] == [32772, 38644]
    assert [item.sha256 for item in release.files] == [HEX_A, HEX_B]
    assert release.total_bytes == 32772 + 38644


def test_scan_parses_second_release(synthetic_tree: Path) -> None:
    catalog = scan_releases(synthetic_tree)
    release = catalog.by_id("raw-view")
    assert release.version == "0.3.0test"
    assert release.name == "RAW view"
    assert release.loader == 4
    assert release.min_svc == 2
    assert release.category == "monitor"
    assert release.risk == "test"
    assert release.summary == "en: RAW monitoring on the LCD."
    assert len(release.files) == 3
    assert release.files[0].size == 32768
    assert release.files[2].path == "fpSup/UI/0.BIN"
    assert release.files[2].size == 114688
    assert release.total_bytes == 32768 + 38644 + 114688


def test_scan_falls_back_when_manifest_is_missing(synthetic_tree: Path) -> None:
    catalog = scan_releases(synthetic_tree)
    release = catalog.by_id("gyro-base")

    assert release.directory == "fpsup-gyro-base-v2.0.0"
    assert release.version == "2.0.0"
    assert release.name == "Gyro Base"
    assert release.firmware == "5.02"
    assert release.loader == 3
    assert release.min_svc is None
    assert release.category == "gyro"
    assert release.risk == "release"
    assert release.summary == "en: Fallback gyro base."

    assert [item.path for item in release.files] == [
        "AutoRun.txt",
        "fpSup/BASE.BIN",
        "fpSup/UI/0.BIN",
    ]
    expected = {
        "AutoRun.txt": b"autorun payload\n",
        "fpSup/BASE.BIN": b"base payload",
        "fpSup/UI/0.BIN": b"ui payload",
    }
    for item in release.files:
        payload = expected[item.path]
        assert item.size == len(payload)
        assert item.sha256 == hashlib.sha256(payload).hexdigest()
    assert release.total_bytes == sum(len(value) for value in expected.values())


def test_scan_skips_directories_without_manifest_or_autrun(
    synthetic_tree: Path,
) -> None:
    catalog = scan_releases(synthetic_tree)
    assert [release.id for release in catalog.releases] == [
        "gyro-base",
        "og3k",
        "raw-view",
    ]


def test_scan_missing_root_returns_empty_catalog(tmp_path: Path) -> None:
    assert scan_releases(tmp_path / "does-not-exist").releases == ()


def test_save_catalog_round_trips(synthetic_tree: Path, tmp_path: Path) -> None:
    catalog = scan_releases(synthetic_tree)
    target = tmp_path / "out" / "releases.json"
    save_catalog(catalog, target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [item["id"] for item in payload] == ["gyro-base", "og3k", "raw-view"]
    fallback = payload[0]
    assert fallback["min_svc"] is None
    assert [item["path"] for item in fallback["files"]] == [
        "AutoRun.txt",
        "fpSup/BASE.BIN",
        "fpSup/UI/0.BIN",
    ]
    assert payload[1]["files"][0] == {
        "path": "AutoRun.txt",
        "size": 32772,
        "sha256": HEX_A,
    }
    assert payload[2]["loader"] == 4


def test_load_builtin_is_populated_and_well_formed() -> None:
    catalog = load_builtin()
    assert len(catalog.releases) >= 20
    for release in catalog.releases:
        assert release.id
        assert release.version
        assert release.directory.startswith("fpsup-")
        assert release.firmware
        assert release.loader in (3, 4)
        assert release.min_svc is None or release.min_svc >= 0
        assert release.category in _CATEGORIES
        assert release.risk in _RISKS
        assert release.summary
        assert release.files, release.id
        for item in release.files:
            assert _SHA256_RE.match(item.sha256), (release.id, item.path)


@pytest.mark.skipif(
    not _VENDOR_RELEASES.is_dir(), reason="vendor release tree not available"
)
def test_real_vendor_releases_parse_without_exceptions() -> None:
    catalog = scan_releases(_VENDOR_RELEASES)
    assert len(catalog.releases) >= 48
    for release in catalog.releases:
        assert release.files, release.id
        assert release.firmware.startswith("5.0")
    ids = {release.id for release in catalog.releases}
    assert ids >= {
        "og3k",
        "og2k",
        "lossless",
        "gyro",
        "gyro2",
        "gyro-base",
        "raw-view",
        "res-custom",
        "usbshell",
    }
    versions = {(release.id, release.version) for release in catalog.releases}
    assert ("gyro-base", "1.14.0") in versions
    for version in ("3.1.0", "3.1.1", "3.2.0", "3.3.0"):
        assert ("usbshell", version) in versions
