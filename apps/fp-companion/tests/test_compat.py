"""Tests for the offline SUP compatibility solver.

Everything is synthetic except ``test_real_load_log``, which parses the live
LOAD.LOG captured at ``C:\\Users\\vvynot\\AppData\\Local\\Temp\\LOAD.LOG`` when
it exists.  No camera is opened and no card is written.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from fp_companion.compat import (
    SL_CHAIN,
    SL_EXCL,
    SL_SHARED_UI,
    Claim,
    SupInfo,
    build_graph,
    can_combine,
    detect_family,
    graph_from_loadlog,
    parse_loadlog,
    parse_sup_bytes,
    resolve_code,
    sup_id_text,
)
from fp_companion.compat import cli as compat_cli

SL_MAGIC = 0x31425346
SL4_MAGIC = 0x34425346

REAL_LOAD_LOG = Path(r"C:\Users\vvynot\AppData\Local\Temp\LOAD.LOG")


# -- synthetic images --------------------------------------------------------


def _id(text: str | bytes) -> int:
    raw = text.encode() if isinstance(text, str) else text
    return struct.unpack("<I", raw.ljust(4, b"\0")[:4])[0]


def fsb1(
    *,
    sup_id: str = "TEST",
    version: int = 1,
    min_svc: int = 1,
    body: bytes = b"\0\0\0\0",
    entry_off: int = 32,
    file_len: int | None = None,
    block_size: int | None = None,
    magic: int = SL_MAGIC,
    header_len: int = 32,
    pad: bool = True,
) -> bytes:
    raw_len = 32 + len(body)
    if file_len is None:
        file_len = raw_len
        file_len += -file_len % 4
    if block_size is None:
        block_size = max(file_len, raw_len)
    header = struct.pack(
        "<8I",
        magic,
        header_len,
        file_len,
        block_size,
        entry_off,
        _id(sup_id),
        version,
        min_svc,
    )
    data = header + body
    if pad and len(data) < file_len:
        data += b"\0" * (file_len - len(data))
    return data


def fsb4(
    *,
    sup_id: str = "TEST",
    version: int = 1,
    min_svc: int = 7,
    body: bytes = b"\0\0\0\0",
    entry_off: int = 48,
    name: str | None = "Test Sup",
    flags: int = 0,
    dma: tuple[int, int] = (0, 0),
    file_len: int | None = None,
    block_size: int | None = None,
    magic: int = SL4_MAGIC,
    header_len: int = 48,
) -> bytes:
    tail = b""
    name_off = 0
    if name is not None:
        name_off = 48 + len(body)
        tail = name.encode() + b"\0"
    raw_len = 48 + len(body) + len(tail)
    if file_len is None:
        file_len = raw_len
        file_len += -file_len % 4
    if block_size is None:
        block_size = max(file_len, raw_len)
    header = struct.pack(
        "<12I",
        magic,
        header_len,
        file_len,
        block_size,
        entry_off,
        _id(sup_id),
        version,
        min_svc,
        flags,
        dma[0],
        dma[1],
        name_off,
    )
    data = header + body + tail
    if len(data) < file_len:
        data += b"\0" * (file_len - len(data))
    return data


def sup(
    name: str,
    sup_id: str,
    *,
    fmt: str = "FSB1",
    version: int = 1,
    min_svc: int = 1,
    claims: list[Claim] | None = None,
    status: str = "ok",
    loaded: bool = False,
    released: bool = False,
    conflicts: list[str] | None = None,
) -> SupInfo:
    return SupInfo(
        path=name,
        name=name,
        sup_id=_id(sup_id),
        format=fmt,
        version=version,
        min_svc=min_svc,
        claims=list(claims or []),
        status=status,
        loaded=loaded,
        released=released,
        conflicts=list(conflicts or []),
    )


# -- binparse ----------------------------------------------------------------


def test_parse_fsb1_header() -> None:
    data = fsb1(sup_id="LXPO", version=3, min_svc=2, body=b"\0" * 8)
    info = parse_sup_bytes(data, name="30LEXPO.BIN")

    assert info.format == "FSB1"
    assert info.sup_id_text == "LXPO"
    assert info.version == 3
    assert info.min_svc == 2
    assert info.file_len == len(data)
    assert info.header_len == 32
    assert info.status == "ok"
    assert info.problems == []


def test_parse_fsb4_header_and_display_name() -> None:
    data = fsb4(sup_id="LOSS", version=2, min_svc=7, name="Lossless")
    info = parse_sup_bytes(data, name="Lossless_v0.3.1.BIN")

    assert info.format == "FSB4"
    assert info.sup_id_text == "LOSS"
    assert info.version == 2
    assert info.min_svc == 7
    assert info.display_name == "Lossless"
    assert info.header_len == 48
    assert info.status == "ok"


def test_fsb4_name_offset_past_file_is_a_problem() -> None:
    data = bytearray(fsb4())
    struct.pack_into("<I", data, 44, len(data) + 100)  # name_off
    info = parse_sup_bytes(bytes(data), name="bad.BIN")

    assert info.status == "bad_header"
    assert any("name_off" in problem for problem in info.problems)


def test_file_len_mismatch_is_bad_header() -> None:
    data = fsb1() + b"\0\0\0\0"  # header says one size, file is longer
    info = parse_sup_bytes(data, name="stale.BIN")

    assert info.status == "bad_header"
    assert any("actual size" in problem for problem in info.problems)


def test_unknown_magic_is_identified() -> None:
    info = parse_sup_bytes(b"VBIN" + b"\0" * 60, name="LOADER.BIN")

    assert info.format == "unknown"
    assert info.status == "bad_header"
    assert "FSB1" in info.problems[0]


def test_loader_format_acceptance() -> None:
    fsb1_info = parse_sup_bytes(fsb1(), name="v3.BIN")
    fsb4_info = parse_sup_bytes(fsb4(), name="v4.BIN")

    assert fsb1_info.loader_accepts(6)
    assert not fsb1_info.loader_accepts(7)
    assert fsb1_info.is_old_format_for(7)
    assert fsb4_info.loader_accepts(7)
    assert not fsb4_info.loader_accepts(6)


def test_sup_id_text() -> None:
    assert sup_id_text(_id("LXPO")) == "LXPO"
    assert sup_id_text(0) == "0000"
    assert sup_id_text(0xFFFFFFFF).startswith("0x")


# -- LOAD.LOG ----------------------------------------------------------------

LOG_SAMPLE = (
    "01 002D20D2 00000000 \n"
    "03 00000000 00000000 LOAD.LOG\n"
    "03 00000000 00000000 LOADER.BIN\n"
    "11 00000002 00000000 \n"
    "0A 45046540 00001190 00SHELL.BIN\n"
    "12 C072F000 4C485356 \n"
    "0A 45148580 000002FC 30LEXPO.BIN\n"
    "12 C007F324 E309C6FC \n"
    "0E 00000002 00000002 \n"
    "0F 00000000 00000000 \n"
    "10 C007F324 4F50584C 30LEXPO.BIN\n"
    "13 4F50584C 00000000 30LEXPO.BIN\n"
    "14 4F50584C 45148580 30LEXPO.BIN\n"
    "15 45028040 000002FC 30LEXPO.BIN\n"
)


def test_parse_loadlog_uses_enum_names_not_numbers() -> None:
    events = parse_loadlog(LOG_SAMPLE)

    assert [event.name for event in events] == [
        "START",
        "SKIP_NAME",
        "SKIP_NAME",
        "LISTED",
        "LOADED",
        "CLAIM",
        "LOADED",
        "CLAIM",
        "DONE",
        "NO_POFF",
        "CONFLICT",
        "DISABLED",
        "UNLOAD",
        "HOTLOAD",
    ]
    assert events[4].text == "00SHELL.BIN"
    assert events[4].a == 0x45046540
    assert events[5].a == 0xC072F000


def test_loadlog_14_and_15_ambiguity() -> None:
    # v4's signature: a is the FSB1 magic and b is 0.
    assert resolve_code(0x14, a=SL_MAGIC, b=0) == "OLD_FORMAT"
    # v3's release() record: a is a sup_id, b a block address.
    assert resolve_code(0x14, a=_id("LXPO"), b=0x45148580) == "UNLOAD"
    assert resolve_code(0x15, family="v3") == "HOTLOAD"
    assert resolve_code(0x15, family="v4") == "PWRLOG"
    assert resolve_code(0x31) == "PWRFILE"
    assert resolve_code(0x32) == "UI"
    assert resolve_code(0xFE) == "UNKNOWN_FE"


def test_loadlog_accepts_bytes_and_padded_4096_byte_file() -> None:
    text = LOG_SAMPLE + " " * (4096 - len(LOG_SAMPLE) - 1) + "\n"
    assert len(text) == 4096
    assert len(parse_loadlog(text.encode("latin-1"))) == 14
    assert parse_loadlog("\n   \n" + " " * 100) == []


def test_detect_family() -> None:
    assert detect_family(parse_loadlog(LOG_SAMPLE)) == "v3"
    v4 = "14 31425346 00000000 OLD.BIN\n32 00000000 00000000 \n"
    assert detect_family(parse_loadlog(v4)) == "v4"
    assert detect_family(parse_loadlog("0E 00000001 00000001 \n")) == "unknown"


# -- solver ------------------------------------------------------------------


def test_duplicate_sup_id_conflicts() -> None:
    a = sup("A.BIN", "DUPL")
    b = sup("B.BIN", "DUPL")

    result = can_combine([a, b], loader_version=6)

    assert not result.ok
    assert any(issue.code == "duplicate-id" for issue in result.issues)
    assert any("DUPL" in reason for reason in result.reasons)


def test_distinct_ids_combine() -> None:
    result = can_combine([sup("A.BIN", "AAAA"), sup("B.BIN", "BBBB")], 6)
    assert result.ok
    assert result.reasons == []


def test_excl_claims_overlap_conflicts() -> None:
    a = sup("A.BIN", "AAAA", claims=[Claim(0xC0000000, 4, SL_EXCL)])
    b = sup("B.BIN", "BBBB", claims=[Claim(0xC0000000, 4, SL_EXCL)])

    result = can_combine([a, b], 6)

    assert not result.ok
    assert any(issue.code == "claim-conflict" for issue in result.issues)


def test_chain_and_shared_ui_share_same_range() -> None:
    chain_a = sup("A.BIN", "AAAA", claims=[Claim(0xC0000000, 4, SL_CHAIN)])
    chain_b = sup("B.BIN", "BBBB", claims=[Claim(0xC0000000, 4, SL_CHAIN)])
    ui_a = sup("C.BIN", "CCCC", claims=[Claim(0xC0001000, 8, SL_SHARED_UI)])
    ui_b = sup("D.BIN", "DDDD", claims=[Claim(0xC0001000, 8, SL_SHARED_UI)])

    assert can_combine([chain_a, chain_b], 6).ok
    assert can_combine([ui_a, ui_b], 6).ok


def test_chain_vs_shared_ui_or_partial_overlap_conflicts() -> None:
    chain = sup("A.BIN", "AAAA", claims=[Claim(0xC0000000, 4, SL_CHAIN)])
    shared = sup("B.BIN", "BBBB", claims=[Claim(0xC0000000, 4, SL_SHARED_UI)])
    wider = sup("C.BIN", "CCCC", claims=[Claim(0xC0000000, 8, SL_CHAIN)])
    excl = sup("D.BIN", "DDDD", claims=[Claim(0xC0000000, 4, SL_EXCL)])

    assert not can_combine([chain, shared], 6).ok
    assert not can_combine([chain, wider], 6).ok
    assert not can_combine([chain, excl], 6).ok


def test_min_svc_must_not_exceed_loader() -> None:
    needs_seven = sup("new.BIN", "NEW1", min_svc=7)

    result = can_combine([needs_seven], loader_version=6)

    assert not result.ok
    assert any(issue.code == "min-svc" for issue in result.issues)

    # Under v4 the file has the right format but still asks for too much.
    needs_eight = sup("new4.BIN", "NEW4", fmt="FSB4", min_svc=8)
    assert not can_combine([needs_eight], loader_version=7).ok
    ok_v4 = sup("ok4.BIN", "OK44", fmt="FSB4", min_svc=7)
    assert can_combine([ok_v4], loader_version=7).ok


def test_loader_format_mismatch_both_ways() -> None:
    v3_file = sup("v3.BIN", "V3F1", fmt="FSB1")
    v4_file = sup("v4.BIN", "V4F4", fmt="FSB4", min_svc=7)

    v3_with_v4 = can_combine([v4_file], loader_version=6)
    v4_with_v3 = can_combine([v3_file], loader_version=7)

    assert not v3_with_v4.ok
    assert any(issue.code == "loader-format" for issue in v3_with_v4.issues)
    assert not v4_with_v3.ok
    assert any(issue.code == "old-format" for issue in v4_with_v3.issues)
    assert can_combine([v3_file], loader_version=6).ok
    assert can_combine([v4_file], loader_version=7).ok


def test_observed_claims_are_compared_with_declarations() -> None:
    s = sup(
        "A.BIN",
        "AAAA",
        loaded=True,
        claims=[
            Claim(0xC0000000, 4, SL_EXCL, source="build.json:patch"),
            Claim(0xC0000100, 4, source="load.log", length_known=False),
        ],
    )

    result = can_combine([s], 6)

    assert result.ok  # mismatches are warnings, not hard conflicts
    codes = {issue.code for issue in result.issues}
    assert "claim-undeclared" in codes
    assert "claim-unobserved" in codes


def test_observed_claim_inside_declared_range_is_clean() -> None:
    s = sup(
        "A.BIN",
        "AAAA",
        loaded=True,
        claims=[
            Claim(0xC0000000, 16, SL_EXCL, source="build.json:patch"),
            Claim(0xC0000004, 4, source="load.log", length_known=False),
        ],
    )

    result = can_combine([s], 6)

    assert result.ok
    assert result.issues == []


def test_logged_conflicts_are_errors() -> None:
    s = sup("A.BIN", "AAAA", conflicts=["duplicate sup_id LXPO"])

    result = can_combine([s], 6)

    assert not result.ok
    assert any(issue.code == "log-conflict" for issue in result.issues)


def test_result_json_shape() -> None:
    result = can_combine([sup("A.BIN", "AAAA")], 6).to_dict()
    assert result["ok"] is True
    assert result["loader_family"] == "v3"
    assert result["reasons"] == []


# -- graph -------------------------------------------------------------------


def _write_card(card: Path) -> tuple[Path, Path]:
    fp_sup = card / "fpSup"
    fp_sup.mkdir(parents=True)
    (fp_sup / "A.BIN").write_bytes(fsb1(sup_id="AAAA", body=b"\0" * 8))
    (fp_sup / "B.BIN").write_bytes(fsb1(sup_id="BBBB", body=b"\0" * 8))
    (fp_sup / "LOAD.LOG").write_text(
        "01 00000001 00000000 \n"
        "11 00000002 00000000 \n"
        "0A 45000000 00000028 A.BIN\n"
        "12 C0000010 DEADBEEF \n"
        "0E 00000001 00000001 \n"
    )
    (card / "build.json").write_text(
        json.dumps({"product": "test card", "file": "A.BIN",
                    "patch": {"address": 0xC0000010}})
    )
    lines = []
    for path in sorted(fp_sup.glob("*.BIN")):
        rel = f"fpSup\\{path.name}"
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{rel}\n")
    (card / "MANIFEST.sha256").write_text("".join(lines))
    return card, fp_sup


def test_graph_reads_headers_manifest_build_and_log(tmp_path: Path) -> None:
    card, fp_sup = _write_card(tmp_path / "card")

    graph = build_graph(card)

    assert {info.name for info in graph.sups} == {"A.BIN", "B.BIN"}
    assert graph.loader_version == 6
    assert graph.loader_family == "v3"
    assert graph.log_path == fp_sup / "LOAD.LOG"
    assert len(graph.manifest) == 2
    assert graph.build["product"] == "test card"
    assert not [issue for issue in graph.issues if issue.is_error]

    a = graph.by_name("A.BIN")
    assert a is not None and a.loaded and not a.released
    assert [(c.addr, c.source) for c in a.claims] == [
        (0xC0000010, "build.json:patch (kind inferred EXCL)"),
        (0xC0000010, "load.log"),
    ]

    result = can_combine(graph.sups, loader_version=graph.loader_version)
    assert result.ok, result.reasons


def test_graph_manifest_mismatch_is_reported(tmp_path: Path) -> None:
    card, fp_sup = _write_card(tmp_path / "card")
    lines = (card / "MANIFEST.sha256").read_text().splitlines()
    lines[0] = "0" * 64 + lines[0][64:]
    (card / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")

    graph = build_graph(card)

    assert any(issue.code == "manifest-mismatch" for issue in graph.issues)


def test_graph_manifest_path_escape_is_refused(tmp_path: Path) -> None:
    card, _ = _write_card(tmp_path / "card")
    with (card / "MANIFEST.sha256").open("a") as handle:
        handle.write("0" * 64 + "  ../outside.BIN\n")

    graph = build_graph(card)

    assert any(issue.code == "manifest-path" for issue in graph.issues)


def test_graph_accepts_fp_sup_directory_directly(tmp_path: Path) -> None:
    _, fp_sup = _write_card(tmp_path / "card")

    graph = build_graph(fp_sup)

    assert {info.name for info in graph.sups} == {"A.BIN", "B.BIN"}
    assert graph.build["product"] == "test card"


def test_graph_from_load_log_alone() -> None:
    graph = graph_from_loadlog(LOG_SAMPLE)

    assert graph.root is None
    assert graph.loader_version == 6
    assert graph.by_name("30LEXPO.BIN") is not None
    assert graph.by_name("00SHELL.BIN") is not None
    observed = graph.observed_claims()
    assert any(claim.addr == 0xC007F324 for _, claim in observed)


def test_graph_uses_8192_byte_padding_as_v4_hint() -> None:
    line = "15 00000000 00000000 \n"  # 0x15 is PWRLOG only in v4
    text = line + " " * (8192 - len(line) - 1) + "\n"
    assert len(text) == 8192

    graph = graph_from_loadlog(text)

    assert graph.loader_family == "v4"
    assert graph.loader_version == 7
    assert [event.name for event in graph.events] == ["PWRLOG"]


# -- CLI ---------------------------------------------------------------------


def test_cli_check_compatible_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    card, _ = _write_card(tmp_path / "card")

    code = compat_cli.main(["check", str(card), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["loader_version"] == 6


def test_cli_check_conflicts_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fp_sup = tmp_path / "card" / "fpSup"
    fp_sup.mkdir(parents=True)
    (fp_sup / "A.BIN").write_bytes(fsb1(sup_id="DUPL"))
    (fp_sup / "B.BIN").write_bytes(fsb1(sup_id="DUPL"))

    code = compat_cli.main(["check", str(tmp_path / "card"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert any(issue["code"] == "duplicate-id" for issue in payload["issues"])


def test_cli_graph_and_explain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    card, _ = _write_card(tmp_path / "card")

    assert compat_cli.main(["graph", str(card), "--json"]) == 0
    graph_payload = json.loads(capsys.readouterr().out)
    assert len(graph_payload["sups"]) == 2

    assert compat_cli.main(["explain", str(card)]) == 0
    output = capsys.readouterr().out
    assert "verdict: compatible" in output
    assert "LOAD.LOG events:" in output


def test_cli_missing_path_is_usage_error(tmp_path: Path) -> None:
    assert compat_cli.main(["check", str(tmp_path / "does-not-exist")]) == 2


# -- the real captured log ---------------------------------------------------


@pytest.mark.skipif(not REAL_LOAD_LOG.is_file(), reason="no captured LOAD.LOG")
def test_real_load_log() -> None:
    data = REAL_LOAD_LOG.read_bytes()
    assert len(data) == 4096

    events = parse_loadlog(data)
    names = {event.name for event in events}
    assert {"START", "LISTED", "LOADED", "CLAIM", "DONE", "UNLOAD",
            "HOTLOAD"}.issubset(names)
    assert "SKIP_NAME" in names
    assert detect_family(events) == "v3"

    graph = graph_from_loadlog(events, path=REAL_LOAD_LOG)
    assert graph.loader_version == 6
    lexpo = graph.by_name("30LEXPO.BIN")
    assert lexpo is not None
    assert lexpo.loaded
    assert lexpo.load_count >= 1
    assert lexpo.release_count >= 1
    assert any(claim.addr == 0xC007F324 for claim in lexpo.claims)
    assert graph.by_name("00SHELL.BIN") is not None

    # A log-only graph has no headers or declarations, so it must not invent
    # incompatibilities; the log's own conflicts (if any) would still fail it.
    result = can_combine(graph.sups, loader_version=6)
    assert result.ok, result.reasons


def test_repeated_loadlog_claims_are_deduped() -> None:
    text = (
        "0A 45000000 00000100 A.BIN\n"
        "12 C0000000 00000000 \n"
        "15 45000000 00000100 A.BIN\n"
        "0A 45000000 00000100 A.BIN\n"
        "12 C0000000 00000000 \n"
    )
    graph = graph_from_loadlog(text)
    a = graph.by_name("A.BIN")
    assert a is not None
    observed = [c for c in a.claims if c.source == "load.log"]
    assert [(c.addr, c.len) for c in observed] == [(0xC0000000, 4)]
