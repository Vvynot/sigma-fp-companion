from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from fp_companion.core import plan as plan_module
from fp_companion.core.models import Release, ReleaseFile
from fp_companion.core.plan import MIN_FREE_BYTES, PROJECT_ROOT, make_plan, validate_plan

PAYLOAD = b"payload-data"
RELEASE_DIR = "fpsup-gyro-v1.14.0"


def _release(**overrides: object) -> Release:
    """Build a synthetic, valid release; override any field per test."""

    files = overrides.pop("files", (ReleaseFile("payload.bin", len(PAYLOAD), "0" * 64),))
    values: dict[str, object] = {
        "id": "gyro",
        "name": "Gyro stabilisation",
        "version": "1.14.0",
        "directory": RELEASE_DIR,
        "firmware": "5.02",
        "loader": 4,
        "min_svc": 3,
        "files": files,
        "category": "gyro",
        "summary": "Synthetic release used by the plan tests.",
        "risk": "release",
    }
    values.update(overrides)
    return Release(**values)  # type: ignore[arg-type]


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    release_dir = root / RELEASE_DIR
    release_dir.mkdir(parents=True)
    (release_dir / "payload.bin").write_bytes(PAYLOAD)
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    card = tmp_path / "card"
    card.mkdir()
    return card


def _error_codes(result) -> set[str]:
    return {issue.code for issue in result.issues if issue.severity == "error"}


def test_happy_path(source_tree: Path, target: Path) -> None:
    release = _release()
    plan = make_plan(release, target, source_tree)

    assert plan.release is release
    assert plan.target == target
    assert plan.source_root == source_tree
    assert plan.dry_run is False

    result = validate_plan(plan)
    assert result.ok
    assert _error_codes(result) == set()
    # The stock-color reminder is always present, and only the stock warning
    # is expected for a clean release.
    assert [issue.code for issue in result.issues] == ["stock"]
    assert all(issue.severity == "warning" for issue in result.issues)


def test_make_plan_records_dry_run(source_tree: Path, target: Path) -> None:
    plan = make_plan(_release(), target, source_tree, dry_run=True)
    assert plan.dry_run is True


def test_non_502_firmware_is_an_error(source_tree: Path, target: Path) -> None:
    result = validate_plan(make_plan(_release(firmware="5.00"), target, source_tree))
    assert not result.ok
    assert "firmware" in _error_codes(result)


def test_missing_source_file_is_an_error(source_tree: Path, target: Path) -> None:
    files = (
        ReleaseFile("payload.bin", len(PAYLOAD), "0" * 64),
        ReleaseFile("missing.bin", 4, "1" * 64),
    )
    result = validate_plan(make_plan(_release(files=files), target, source_tree))

    assert not result.ok
    missing = [issue for issue in result.issues if issue.code == "missing"]
    assert len(missing) == 1
    assert "missing.bin" in missing[0].message


def test_duplicate_file_path_is_an_error(source_tree: Path, target: Path) -> None:
    files = (
        ReleaseFile("payload.bin", len(PAYLOAD), "0" * 64),
        ReleaseFile("payload.bin", len(PAYLOAD), "f" * 64),
    )
    result = validate_plan(make_plan(_release(files=files), target, source_tree))

    assert not result.ok
    duplicates = [issue for issue in result.issues if issue.code == "duplicate"]
    assert len(duplicates) == 1
    assert "payload.bin" in duplicates[0].message


def test_target_inside_source_is_refused(source_tree: Path) -> None:
    inside = source_tree / "card"
    inside.mkdir()

    result = validate_plan(make_plan(_release(), inside, source_tree))
    assert not result.ok
    assert "source" in _error_codes(result)


def test_source_tree_inside_target_is_allowed(tmp_path: Path) -> None:
    # Reading from E:\project\... while writing to E:\ is legitimate; the
    # reverse containment must not be rejected.
    card = tmp_path / "card"
    card.mkdir()
    source_root = card / "source"
    release_dir = source_root / RELEASE_DIR
    release_dir.mkdir(parents=True)
    (release_dir / "payload.bin").write_bytes(PAYLOAD)

    result = validate_plan(make_plan(_release(), card, source_root))
    assert result.ok
    assert "source" not in _error_codes(result)


@pytest.mark.parametrize("project_target", [PROJECT_ROOT, PROJECT_ROOT / "apps"])
def test_project_tree_is_refused(source_tree: Path, project_target: Path) -> None:
    assert project_target.is_dir()
    result = validate_plan(make_plan(_release(), project_target, source_tree))
    assert not result.ok
    assert "project" in _error_codes(result)


@pytest.mark.skipif(os.name != "nt", reason="drive-root semantics are Windows-only")
def test_system_drive_root_is_refused(source_tree: Path) -> None:
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
    root = Path(f"{system_drive}\\")
    if not root.is_dir():
        pytest.skip(f"system drive root is unavailable: {root}")

    result = validate_plan(make_plan(_release(), root, source_tree))
    assert not result.ok
    assert "root" in _error_codes(result)


@pytest.mark.skipif(os.name != "nt", reason="drive-root semantics are Windows-only")
def test_non_system_drive_root_is_allowed(source_tree: Path) -> None:
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
    root = next(
        (
            Path(f"{letter}:\\")
            for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"
            if f"{letter}:" != system_drive and Path(f"{letter}:\\").is_dir()
        ),
        None,
    )
    if root is None:
        pytest.skip("no non-system drive root is available")

    result = validate_plan(make_plan(_release(), root, source_tree))
    # Free space may still fail on a full drive, but the drive root itself is
    # a valid SD-card target and must not be refused.
    assert "root" not in _error_codes(result)


def test_missing_target_is_an_error(tmp_path: Path, source_tree: Path) -> None:
    result = validate_plan(make_plan(_release(), tmp_path / "not-there", source_tree))
    assert not result.ok
    assert "target" in _error_codes(result)


def test_target_that_is_a_file_is_an_error(tmp_path: Path, source_tree: Path) -> None:
    path = tmp_path / "card.txt"
    path.write_text("not a directory", encoding="utf-8")

    result = validate_plan(make_plan(_release(), path, source_tree))
    assert not result.ok
    assert "target" in _error_codes(result)


def test_alpha_release_warns_but_does_not_block(source_tree: Path, target: Path) -> None:
    result = validate_plan(make_plan(_release(risk="alpha"), target, source_tree))

    assert result.ok
    assert "risk" in {issue.code for issue in result.issues if issue.severity == "warning"}


@pytest.mark.parametrize("release_id", ["og3k", "og4k", "res-custom"])
def test_ssd_only_modes_warn(source_tree: Path, target: Path, release_id: str) -> None:
    result = validate_plan(make_plan(_release(id=release_id), target, source_tree))

    assert result.ok
    ssd = [issue for issue in result.issues if issue.code == "ssd"]
    assert len(ssd) == 1
    assert ssd[0].severity == "warning"
    assert "SSD" in ssd[0].message


def test_stock_color_warning_is_always_present(source_tree: Path, target: Path) -> None:
    result = validate_plan(make_plan(_release(), target, source_tree))
    stock = [issue for issue in result.issues if issue.code == "stock"]
    assert len(stock) == 1
    assert stock[0].severity == "warning"
    assert "stock color mode" in stock[0].message


def test_stock_warning_present_even_when_other_checks_fail(source_tree: Path, target: Path) -> None:
    result = validate_plan(make_plan(_release(firmware="5.00"), target, source_tree))
    assert any(issue.code == "stock" for issue in result.issues)


@pytest.mark.parametrize("slack, expected_block", [(-1, True), (0, False)])
def test_free_space_threshold(
    source_tree: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    slack: int,
    expected_block: bool,
) -> None:
    release = _release()
    free = release.total_bytes + MIN_FREE_BYTES + slack

    def fake_disk_usage(_path) -> object:
        return types.SimpleNamespace(total=free, used=0, free=free)

    monkeypatch.setattr(plan_module.shutil, "disk_usage", fake_disk_usage)

    result = validate_plan(make_plan(release, target, source_tree))
    space_errors = [issue for issue in result.issues if issue.code == "space"]
    assert bool(space_errors) is expected_block
    assert result.ok is not expected_block


def test_disk_usage_oserror_is_an_error(
    source_tree: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_disk_usage(_path) -> object:
        raise OSError("device not ready")

    monkeypatch.setattr(plan_module.shutil, "disk_usage", broken_disk_usage)

    result = validate_plan(make_plan(_release(), target, source_tree))
    assert not result.ok
    space = [issue for issue in result.issues if issue.code == "space"]
    assert len(space) == 1
    assert "device not ready" in space[0].message
