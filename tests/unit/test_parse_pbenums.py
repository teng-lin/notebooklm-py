from pathlib import Path

from scripts import parse_pbenums

# Two ``Color`` runs: the wire library's (three values) and a persistence copy that reuses
# integer 2 for a different name. ``Hidden`` has no pool object at all. ``Twin`` carries an
# alias (two names for integer 1) inside one enum.
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

Obj!NotAnEnum@c1 : {
  off_8: int(0x1)
}

Obj!Color@d1 : {
  Super!ProtobufEnum : {
    off_8: int(0x2),
    off_10: "COLOR_GREEN"
  }
}

Obj!Hidden@b1 : {
  Super!ProtobufEnum : {
    off_8: int(0x7),
    off_10: "HIDDEN_SEVEN"
  }
}

Obj!Twin@e1 : {
  Super!ProtobufEnum : {
    off_8: int(0x1),
    off_10: "TWIN_ONE"
  }
}

Obj!Twin@e2 : {
  Super!ProtobufEnum : {
    off_8: int(0x1),
    off_10: "TWIN_UNO"
  }
}
"""

POOL = """\
[pp+0x10] Obj!Color@A1 : {
[pp+0x18] Obj!Color@a3 : {
[pp+0x20] List<Color>(3) [Obj!Color@a1, Obj!Color@a2, Obj!Color@a3]
[pp+0x28] List<Color>(1) [Obj!Color@d1]
[pp+0x30] Obj!Twin@e1 : {
"""


def _write_dump(tmp_path: Path) -> Path:
    """Lay out ``objs.txt``, ``pp.txt`` and two libraries' disassembly under ``tmp_path``."""
    (tmp_path / "objs.txt").write_text(OBJS)
    (tmp_path / "pp.txt").write_text(POOL)
    wire = tmp_path / "asm" / "google.example.v1"
    local = tmp_path / "asm" / "app.local.persistence"
    wire.mkdir(parents=True)
    local.mkdir(parents=True)
    (wire / "color.pbenum.dart").write_text(
        "class Color extends ProtobufEnum {\n}\n"
        "class Twin extends ProtobufEnum {\n}\n"
        "    // 0x1: ldr x0, [pp+0x20]\n"
    )
    (wire / "hidden.pbenum.dart").write_text("class Hidden extends ProtobufEnum {\n}\n")
    (local / "color.pbenum.dart").write_text(
        "class Color extends ProtobufEnum {\n}\n    // 0x1: ldr x0, [pp+0x28]\n"
    )
    return tmp_path


def test_parse_enum_runs_splits_same_named_classes_into_separate_runs(tmp_path: Path) -> None:
    """Each contiguous run of one class name in ``objs.txt`` is its own enum."""
    root = _write_dump(tmp_path)

    runs = parse_pbenums.parse_enum_runs(root / "objs.txt")

    assert [(cls, sorted(objects)) for cls, objects in runs] == [
        ("Color", ["a1", "a2", "a3"]),
        ("Color", ["d1"]),
        ("Hidden", ["b1"]),
        ("Twin", ["e1", "e2"]),
    ]


def test_attribute_runs_ties_each_run_to_the_library_that_references_its_values_list(
    tmp_path: Path,
) -> None:
    """Runs take the library whose disassembly references their pool ``values`` list."""
    root = _write_dump(tmp_path)
    runs = parse_pbenums.parse_enum_runs(root / "objs.txt")
    classes = {cls for cls, _ in runs}
    pool_refs, pool_lists = parse_pbenums.parse_pool(root / "pp.txt", classes)
    offset_libraries, class_libraries = parse_pbenums.parse_libraries(root / "asm", classes)

    attributed = parse_pbenums.attribute_runs(runs, pool_lists, offset_libraries, class_libraries)

    assert pool_refs == {"Color": {"a1", "a3"}, "Twin": {"e1"}}
    assert [(cls, library) for cls, library, _ in attributed] == [
        ("Color", "google.example.v1"),
        ("Color", "app.local.persistence"),
        ("Hidden", "google.example.v1"),
        ("Twin", "google.example.v1"),
    ]


def test_render_keeps_libraries_apart_and_preserves_aliases(tmp_path: Path) -> None:
    """Same-named enums render as separate blocks; an aliased integer renders every name."""
    root = _write_dump(tmp_path)
    runs = parse_pbenums.parse_enum_runs(root / "objs.txt")
    classes = {cls for cls, _ in runs}
    pool_refs, pool_lists = parse_pbenums.parse_pool(root / "pp.txt", classes)
    offset_libraries, class_libraries = parse_pbenums.parse_libraries(root / "asm", classes)
    attributed = parse_pbenums.attribute_runs(runs, pool_lists, offset_libraries, class_libraries)

    assert parse_pbenums.render(attributed, pool_refs) == (
        "\n"
        "=== Color (1)  [library app.local.persistence]  [objs-ONLY] ===\n"
        "  2 = COLOR_GREEN\n"
        "\n"
        "=== Color (3)  [library google.example.v1]  [objs adds [2]] ===\n"
        "  0 = COLOR_UNSPECIFIED\n"
        "  1 = COLOR_RED\n"
        "  2 = COLOR_BLUE\n"
        "\n"
        "=== Hidden (1)  [library google.example.v1]  [objs-ONLY] ===\n"
        "  7 = HIDDEN_SEVEN\n"
        "\n"
        "=== Twin (2)  [library google.example.v1]  [aliases [1]] ===\n"
        "  1 = TWIN_ONE\n"
        "  1 = TWIN_UNO\n"
    )


def test_unattributable_run_is_labelled_rather_than_guessed(tmp_path: Path) -> None:
    """With no list evidence and two candidate libraries, a run stays unattributed."""
    root = _write_dump(tmp_path)
    (root / "pp.txt").write_text("")
    runs = parse_pbenums.parse_enum_runs(root / "objs.txt")
    classes = {cls for cls, _ in runs}
    pool_refs, pool_lists = parse_pbenums.parse_pool(root / "pp.txt", classes)
    offset_libraries, class_libraries = parse_pbenums.parse_libraries(root / "asm", classes)

    attributed = parse_pbenums.attribute_runs(runs, pool_lists, offset_libraries, class_libraries)

    assert [(cls, library) for cls, library, _ in attributed if cls == "Color"] == [
        ("Color", None),
        ("Color", None),
    ]
    assert "=== Color (1)  [library unattributed]  [objs-ONLY] ===" in parse_pbenums.render(
        attributed, pool_refs
    )


def test_main_writes_inventory_and_reports_totals(tmp_path: Path, capsys) -> None:
    """The CLI writes the inventory to stdout and its totals to stderr."""
    root = _write_dump(tmp_path)

    assert parse_pbenums.main([str(root)]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("\n=== Color (1)  [library app.local.persistence]")
    assert "parsed 4 enums (3 class names, 0 unattributed), 7 values" in captured.err


def test_main_rejects_missing_argument(capsys) -> None:
    """The CLI needs exactly one dump directory."""
    assert parse_pbenums.main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_conflicting_list_evidence_leaves_the_run_unattributed(tmp_path: Path) -> None:
    """Two libraries referencing lists that contain the same run must not race on pool order."""
    root = _write_dump(tmp_path)
    (root / "pp.txt").write_text(
        "[pp+0x20] List<Color>(3) [Obj!Color@a1, Obj!Color@a2, Obj!Color@a3]\n"
        "[pp+0x28] List<Color>(1) [Obj!Color@a1]\n"
    )
    runs = parse_pbenums.parse_enum_runs(root / "objs.txt")
    classes = {cls for cls, _ in runs}
    _, pool_lists = parse_pbenums.parse_pool(root / "pp.txt", classes)
    offset_libraries, class_libraries = parse_pbenums.parse_libraries(root / "asm", classes)

    attributed = parse_pbenums.attribute_runs(runs, pool_lists, offset_libraries, class_libraries)

    # The first Color run is referenced from both libraries' lists (conflict → unattributed);
    # the second run has no list evidence and two candidate libraries remain, so it stays
    # unattributed too rather than being assigned by elimination.
    assert [(cls, library) for cls, library, _ in attributed if cls == "Color"] == [
        ("Color", None),
        ("Color", None),
    ]
