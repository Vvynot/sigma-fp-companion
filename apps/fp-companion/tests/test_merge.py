from __future__ import annotations

from fp_companion.core.merge import (
    MAX_MODULES,
    MERGE_NOTE,
    MergePlan,
    describe_plan,
    plan_merge,
)
from fp_companion.core.models import Release, ReleaseFile

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _release(
    release_id: str = "gyro",
    *,
    version: str = "1.14.0",
    name: str | None = None,
    directory: str | None = None,
    firmware: str = "5.02",
    loader: int = 3,
    risk: str = "release",
    category: str = "gyro",
    files: tuple[ReleaseFile, ...] | None = None,
) -> Release:
    """Build a synthetic release; every field can be overridden per test."""

    if directory is None:
        directory = f"fpsup-{release_id}-v{version}"
    if name is None:
        name = release_id
    if files is None:
        files = (
            ReleaseFile(f"fpSup/{release_id.upper()}.BIN", 128, SHA_A),
        )
    return Release(
        id=release_id,
        name=name,
        version=version,
        directory=directory,
        firmware=firmware,
        loader=loader,
        min_svc=1,
        files=files,
        category=category,
        summary=f"Synthetic {release_id} release.",
        risk=risk,
    )


def _codes(plan: MergePlan, severity: str | None = None) -> list[str]:
    return [
        issue.code
        for issue in plan.issues
        if severity is None or issue.severity == severity
    ]


def test_happy_path_with_two_modules() -> None:
    gyro = _release(
        "gyro",
        name="Gyro stabilisation",
        files=(ReleaseFile("fpSup/GYRO.BIN", 100, SHA_A),),
    )
    raw = _release(
        "raw-view",
        name="RAW view",
        version="0.3.0test",
        files=(ReleaseFile("fpSup/RAWV.BIN", 200, SHA_B),),
    )

    plan = plan_merge([gyro, raw])

    assert plan.ok
    assert plan.issues == ()
    assert plan.loader == 3
    assert plan.releases == (gyro, raw)
    assert plan.file_count == 2
    assert plan.total_bytes == 300
    assert plan.notes == (MERGE_NOTE,)


def test_empty_selection_is_an_error() -> None:
    plan = plan_merge([])

    assert not plan.ok
    assert _codes(plan, "error") == ["empty"]
    assert plan.loader is None
    assert plan.file_count == 0
    assert plan.total_bytes == 0
    assert plan.releases == ()
    assert plan.notes == (MERGE_NOTE,)


def test_mixed_firmware_is_an_error() -> None:
    plan = plan_merge(
        [
            _release("gyro", firmware="5.02"),
            _release("raw-view", firmware="5.01"),
        ]
    )

    assert not plan.ok
    assert "mixed-firmware" in _codes(plan, "error")
    message = next(i.message for i in plan.issues if i.code == "mixed-firmware")
    assert "5.01" in message and "5.02" in message


def test_mixed_loader_versions_are_an_error() -> None:
    plan = plan_merge(
        [
            _release("gyro", loader=3),
            _release("raw-view", loader=4),
        ]
    )

    assert not plan.ok
    assert "mixed-loader" in _codes(plan, "error")
    assert plan.loader is None


def test_unsupported_loader_is_an_error() -> None:
    plan = plan_merge([_release("gyro", loader=5)])

    assert not plan.ok
    assert "unsupported-loader" in _codes(plan, "error")
    assert plan.loader is None


def test_duplicate_module_id_is_an_error() -> None:
    older = _release(
        "gyro",
        version="1.13.1",
        directory="fpsup-gyro-v1.13.1",
        files=(ReleaseFile("fpSup/GYRO.BIN", 100, SHA_A),),
    )
    newer = _release(
        "gyro",
        version="1.14.0",
        directory="fpsup-gyro-v1.14.0",
        files=(ReleaseFile("fpSup/GYRO.BIN", 100, SHA_A),),
    )

    plan = plan_merge([older, newer])

    assert not plan.ok
    assert "duplicate-id" in _codes(plan, "error")
    message = next(i.message for i in plan.issues if i.code == "duplicate-id")
    assert "pick one version" in message.lower()
    assert "fpsup-gyro-v1.13.1" in message
    assert "fpsup-gyro-v1.14.0" in message


def test_duplicate_release_directory_is_an_error() -> None:
    gyro = _release("gyro")

    plan = plan_merge([gyro, gyro])

    assert not plan.ok
    assert "duplicate-directory" in _codes(plan, "error")


def test_more_than_eight_modules_is_an_error() -> None:
    modules = [_release(f"module{i}", version="1.0.0") for i in range(MAX_MODULES + 1)]

    plan = plan_merge(modules)

    assert not plan.ok
    assert "too-many-modules" in _codes(plan, "error")
    message = next(i.message for i in plan.issues if i.code == "too-many-modules")
    assert str(MAX_MODULES) in message


def test_exactly_eight_modules_is_allowed() -> None:
    modules = [_release(f"module{i}", version="1.0.0") for i in range(MAX_MODULES)]

    plan = plan_merge(modules)

    assert plan.ok
    assert plan.loader == 3


def test_alpha_release_only_warns() -> None:
    plan = plan_merge([_release("gyro", version="1.14.0a", risk="alpha")])

    assert plan.ok
    warnings = [i for i in plan.issues if i.code == "risk"]
    assert len(warnings) == 1
    assert warnings[0].severity == "warning"
    assert "alpha" in warnings[0].message


def test_ssd_only_module_only_warns() -> None:
    plan = plan_merge([_release("og3k", version="0.2.8a", risk="alpha")])

    assert plan.ok
    warnings = [i for i in plan.issues if i.code == "ssd"]
    assert len(warnings) == 1
    assert warnings[0].severity == "warning"
    assert "external USB-C SSD" in warnings[0].message


def test_duplicate_payload_paths_warn_and_name_releases() -> None:
    shared = (
        ReleaseFile("AutoRun.txt", 32_768, SHA_A),
        ReleaseFile("fpSup/LOADER.BIN", 7_260, SHA_B),
        ReleaseFile("FPSUPUI/0.BIN", 114_688, SHA_C),
    )
    first = _release(
        "gyro",
        directory="fpsup-gyro-v1.14.0",
        files=shared,
    )
    second = _release(
        "raw-view",
        version="0.3.0test",
        directory="fpsup-raw-view-v0.3.0test",
        files=shared,
    )

    plan = plan_merge([first, second])

    assert plan.ok
    warnings = [i for i in plan.issues if i.code == "duplicate-path"]
    assert len(warnings) == 3
    assert all(i.severity == "warning" for i in warnings)
    messages = "\n".join(i.message for i in warnings)
    for path in ("AutoRun.txt", "fpSup/LOADER.BIN", "FPSUPUI/0.BIN"):
        assert path in messages
    assert "fpsup-gyro-v1.14.0" in messages
    assert "fpsup-raw-view-v0.3.0test" in messages


def test_duplicate_paths_are_case_insensitive() -> None:
    first = _release(
        "gyro",
        directory="fpsup-gyro-v1.14.0",
        files=(ReleaseFile("AutoRun.txt", 10, SHA_A),),
    )
    second = _release(
        "raw-view",
        version="0.3.0test",
        directory="fpsup-raw-view-v0.3.0test",
        files=(ReleaseFile("autorun.txt", 10, SHA_A),),
    )

    plan = plan_merge([first, second])

    assert plan.ok
    assert _codes(plan, "warning").count("duplicate-path") == 1


def test_duplicate_path_warning_does_not_block() -> None:
    files = (ReleaseFile("AutoRun.txt", 10, SHA_A),)
    plan = plan_merge(
        [
            _release("gyro", directory="fpsup-gyro-v1.14.0", files=files),
            _release(
                "raw-view",
                version="0.3.0test",
                directory="fpsup-raw-view-v0.3.0test",
                files=files,
            ),
        ]
    )

    assert plan.ok
    assert "duplicate-path" in _codes(plan, "warning")


def test_describe_plan_contains_key_facts() -> None:
    gyro = _release(
        "gyro",
        name="Gyro stabilisation",
        version="1.14.0",
        files=(ReleaseFile("fpSup/GYRO.BIN", 100, SHA_A),),
    )
    raw = _release(
        "raw-view",
        name="RAW view",
        version="0.3.0test",
        files=(ReleaseFile("fpSup/RAWV.BIN", 200, SHA_B),),
    )

    text = describe_plan(plan_merge([gyro, raw]))

    assert "Merge plan: OK" in text
    assert "Modules (2)" in text
    assert "Gyro stabilisation 1.14.0" in text
    assert "RAW view 0.3.0test" in text
    assert "Loader: v3" in text
    assert "2 files" in text
    assert "300 bytes" in text
    assert "Issues: none" in text
    assert "not implemented" in text


def test_describe_plan_reports_blocking_issues() -> None:
    text = describe_plan(
        plan_merge(
            [
                _release("gyro", firmware="5.02"),
                _release("raw-view", firmware="5.01"),
            ]
        )
    )

    assert "Merge plan: BLOCKED" in text
    assert "ERROR [mixed-firmware]" in text


def test_describe_plan_handles_empty_selection() -> None:
    text = describe_plan(plan_merge([]))

    assert "Merge plan: BLOCKED" in text
    assert "Modules (0): none" in text
    assert "Loader: unknown" in text
    assert "ERROR [empty]" in text
