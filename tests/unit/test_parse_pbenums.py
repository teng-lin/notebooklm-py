from pathlib import Path

from scripts import parse_pbenums

OBJS = """\
Obj!Color@a1 : {
  Super!ProtobufEnum : {
    off_8: int(0x0),
    off_10: "COLOR_UNSPECIFIED"
  }
}

Obj!Color@a2 : {
  Super!ProtobufEnum : {
    off_8: int(0x2),
    off_10: "COLOR_BLUE"
  }
}

Obj!Color@a3 : {
  Super!ProtobufEnum : {
    off_8: int(0x1),
    off_10: "COLOR_RED"
  }
}

Obj!Hidden@b1 : {
  Super!ProtobufEnum : {
    off_8: int(0x7),
    off_10: "HIDDEN_SEVEN"
  }
}

Obj!NotAnEnum@c1 : {
  off_8: int(0x1)
}
"""

POOL = """\
[pp+0x10] Obj!Color@A1 : {
[pp+0x18] Obj!Color@a3 : {
[pp+0x20] List<Color>(3) [Obj!Color@a1, Obj!Color@a2, Obj!Color@a3]
"""


def _write_dump(tmp_path: Path) -> Path:
    (tmp_path / "objs.txt").write_text(OBJS)
    (tmp_path / "pp.txt").write_text(POOL)
    return tmp_path


def test_render_merges_pool_and_store_and_annotates_store_only_values(tmp_path: Path) -> None:
    root = _write_dump(tmp_path)

    enums = parse_pbenums.parse_enum_objects(root / "objs.txt")
    pool_refs = parse_pbenums.parse_pool_refs(root / "pp.txt", set(enums))

    assert set(enums) == {"Color", "Hidden"}
    assert pool_refs == {"Color": {"a1", "a3"}}
    assert parse_pbenums.render(enums, pool_refs) == (
        "\n"
        "=== Color (3)  [objs adds [2]] ===\n"
        "  0 = COLOR_UNSPECIFIED\n"
        "  1 = COLOR_RED\n"
        "  2 = COLOR_BLUE\n"
        "\n"
        "=== Hidden (1)  [objs-ONLY] ===\n"
        "  7 = HIDDEN_SEVEN\n"
    )


def test_main_writes_inventory_and_reports_totals(tmp_path: Path, capsys) -> None:
    root = _write_dump(tmp_path)

    assert parse_pbenums.main([str(root)]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("\n=== Color (3)  [objs adds [2]] ===\n")
    assert "parsed 2 enums, 4 values" in captured.err


def test_main_rejects_missing_argument(capsys) -> None:
    assert parse_pbenums.main([]) == 2
    assert "usage:" in capsys.readouterr().err
