from pathlib import Path

from scripts import parse_pbschema

EMPTY_MESSAGE = """\
// lib: , url: package:google.example.v1/example_service.pb.dart

class EmptyReply extends GeneratedMessage {
  static BuilderInfo _i() {
    // 0x1: r16 = Instance_PackageName
    //     0x1: add x16, PP, #0x1 ; Obj!PackageName@AbC123
    // 0x2: r2 = "EmptyReply"
    // 0x3: r0 = BuilderInfo()
    //     0x3: bl #0x4 ; BuilderInfo::BuilderInfo
  }
}
"""


MESSAGE_WITH_FIELD = """\
// lib: , url: package:google.example.v1/example_service.pb.dart

class ExampleRequest extends GeneratedMessage {
  static BuilderInfo _i() {
    // 0x1: add x16, PP, #0x1 ; Obj!PackageName@abc123
    // 0x2: r2 = "ExampleRequest"
    // 0x3: r0 = BuilderInfo()
    //     0x3: bl #0x4 ; BuilderInfo::BuilderInfo
    // 0x4: r2 = 1
    // 0x5: r3 = "displayName"
    // 0x6: bl #0x7 ; BuilderInfo::aOS
  }
}
"""

NESTED_MESSAGE = """\
// lib: , url: package:google.example.v1/example_service.pb.dart

class Outer_Entry extends GeneratedMessage {
  static BuilderInfo _i() {
    // 0x1: add x16, PP, #0x1 ; Obj!PackageName@abc123
    // 0x2: r2 = "Outer.Entry"
    // 0x3: r0 = BuilderInfo()
    //     0x3: bl #0x4 ; BuilderInfo::BuilderInfo
    // 0x4: r2 = 1
    // 0x5: r3 = "key"
    // 0x6: bl #0x7 ; BuilderInfo::aOS
  }
}
"""


def _write_dump(tmp_path: Path, source: str, *, with_objects: bool = True) -> tuple[Path, Path]:
    dump_root = tmp_path / "dump"
    asm_root = dump_root / "asm"
    library_dir = asm_root / "google.example.v1"
    library_dir.mkdir(parents=True)
    source_path = library_dir / "example_service.pb.dart"
    source_path.write_text(source)
    if with_objects:
        (dump_root / "objs.txt").write_text(
            'Obj!PackageName@abc123 : {\n  off_8: "google.protobuf"\n}\n'
        )
    return asm_root, source_path


def test_parse_file_retains_zero_field_generated_messages(tmp_path: Path) -> None:
    _, source_path = _write_dump(tmp_path, EMPTY_MESSAGE)

    messages = parse_pbschema.parse_file(source_path)

    assert messages == {"EmptyReply": []}


def test_parse_file_messages_resolves_exact_package_and_library(tmp_path: Path) -> None:
    asm_root, source_path = _write_dump(tmp_path, EMPTY_MESSAGE + MESSAGE_WITH_FIELD)
    package_names = parse_pbschema.find_package_names(asm_root)

    messages = parse_pbschema.parse_file_messages(source_path, package_names)

    assert package_names == {"abc123": "google.protobuf"}
    assert [message.fqn for message in messages] == [
        "google.protobuf.EmptyReply",
        "google.protobuf.ExampleRequest",
    ]
    assert all(
        message.library_uri == "package:google.example.v1/example_service.pb.dart"
        for message in messages
    )
    assert messages[1].fields == [
        {
            "tag": 1,
            "name": "displayName",
            "type": "string",
            "repeated": False,
            "adder": "aOS",
        }
    ]


def test_main_marks_unresolved_package_instead_of_inferring_from_library(
    tmp_path: Path, capsys
) -> None:
    asm_root, _ = _write_dump(tmp_path, EMPTY_MESSAGE, with_objects=False)

    parse_pbschema.main([str(asm_root), "google.example.v1"])

    captured = capsys.readouterr()
    assert "// Dart library: package:google.example.v1/example_service.pb.dart" in captured.out
    assert "// Protobuf FQN unresolved: EmptyReply (PackageName object abc123)" in captured.out
    assert "// Protobuf FQN: google.example.v1.EmptyReply" not in captured.out
    assert "message EmptyReply {\n}" in captured.out
    assert "resolved 0/1 protobuf FQNs" in captured.err


def test_main_default_patterns_preserve_the_complete_historical_scope(
    tmp_path: Path, capsys
) -> None:
    dump_root = tmp_path / "dump"
    asm_root = dump_root / "asm"
    for directory, message_name in (
        ("google.internal.labs.tailwind.orchestration.v1", "OrchestrationEmpty"),
        ("labs.language.tailwind.common.protos", "CommonEmpty"),
        ("logs.proto.labs_tailwind.metadata", "LoggingEmpty"),
    ):
        library_dir = asm_root / directory
        library_dir.mkdir(parents=True)
        (library_dir / "example.pb.dart").write_text(
            EMPTY_MESSAGE.replace("EmptyReply", message_name)
        )
    (dump_root / "objs.txt").write_text(
        'Obj!PackageName@abc123 : {\n  off_8: "google.protobuf"\n}\n'
    )

    parse_pbschema.main([str(asm_root)])

    captured = capsys.readouterr()
    assert "message OrchestrationEmpty {\n}" in captured.out
    assert "message CommonEmpty {\n}" in captured.out
    assert "message LoggingEmpty {\n}" in captured.out
    assert "parsed 3 messages, 0 fields from 3 files" in captured.err
    assert "resolved 3/3 protobuf FQNs" in captured.err


def test_nested_message_keeps_its_dotted_builder_name_as_the_fqn(tmp_path: Path) -> None:
    """A nested message is registered as ``Outer.Entry``; the Dart class name is not the FQN."""
    asm_root, source_path = _write_dump(tmp_path, NESTED_MESSAGE)
    package_names = parse_pbschema.find_package_names(asm_root)

    (message,) = parse_pbschema.parse_file_messages(source_path, package_names)

    assert message.class_name == "Outer_Entry"
    assert message.builder_name == "Outer.Entry"
    assert message.fqn == "google.protobuf.Outer.Entry"
    assert [field["name"] for field in message.fields] == ["key"]
