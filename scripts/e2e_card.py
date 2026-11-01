from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

from fp_companion.core.catalog import scan_releases
from fp_companion.core.plan import make_plan, validate_plan
from fp_companion.core.writer import write_card

REPO = Path(__file__).resolve().parents[1]
RELEASES = REPO / "vendor" / "fpSup" / "fpSup" / "releases"
RELEASE_DIR = sys.argv[1] if len(sys.argv) > 1 else "fpsup-desqueeze-v0.1.4test"


def main() -> int:
    catalog = scan_releases(RELEASES)
    release = next(item for item in catalog.releases if item.directory == RELEASE_DIR)

    with tempfile.TemporaryDirectory(prefix="fp-card-e2e-") as tmp:
        target = Path(tmp) / "card"
        target.mkdir()
        (target / "OLD.TXT").write_text("previous card content", encoding="utf-8")

        plan = make_plan(release, target, RELEASES)
        validation = validate_plan(plan)
        print("validate:", validation.ok, [issue.code for issue in validation.issues])
        if not validation.ok:
            return 1

        report = write_card(plan, backup=True)
        print("write:", report.ok, "files:", len(report.files), "bytes:", report.bytes_written)
        print("verified:", len(report.verified), "backup:", report.backup)
        print("target:", sorted(path.name for path in target.iterdir()))

        manifest = json.loads((target / "fpsup-manifest.json").read_text(encoding="utf-8"))
        print("manifest:", manifest["release"]["id"], manifest["release"]["version"])

        if report.backup is not None:
            with zipfile.ZipFile(report.backup) as archive:
                print("backup entries:", archive.namelist())

        return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
