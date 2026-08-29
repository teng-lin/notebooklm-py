import csv
import io
from pathlib import Path

import pytest

from scripts import extract_blutter_grpc_signatures as extractor


def _write(tmp_path: Path, name: str, contents: str) -> Path:
    path = tmp_path / name
    path.write_text(contents)
    return path


def _symbol(address: str, owner: str) -> str:
    return f'idaapi.set_name(0x{address}, "{owner}")\n'


def test_extracts_exact_fqns_for_google_empty_and_package_local_empty(tmp_path: Path) -> None:
    pp_path = _write(
        tmp_path,
        "pp.txt",
        """\
[pp+0x30] TypeArguments: <DeleteRequest, LocalEmptyResponse>
[pp+0x38] String: "/acme.notes.v1.NotesService/DeleteLocal"
[pp+0x10] TypeArguments: <DeleteRequest, Empty>
[pp+0x18] String: "/acme.notes.v1.NotesService/Delete"
[pp+0x40] TypeArguments: <IgnoredRequest, IgnoredResponse>
[pp+0x50] String: "/acme.notes.v1.NotesService/NotAdjacent"
""",
    )
    symbols_path = _write(
        tmp_path,
        "addNames.py",
        _symbol(
            "1000",
            "acme.notes.v1$notes_service.pb_DeleteRequest::_i_1000",
        )
        + _symbol(
            "1001",
            "other.v1$notes_service.pb_DeleteRequest::_i_1001",
        )
        + _symbol(
            "1002",
            "acme.notes.v1$notes_service.pb_LocalEmptyResponse::_i_1002",
        )
        + _symbol("1003", "google.protobuf$empty.pb_Empty::_i_1003"),
    )

    signatures = extractor.extract_signatures(pp_path, symbols_path)

    assert signatures == [
        extractor.GrpcSignature(
            path="/acme.notes.v1.NotesService/Delete",
            service="acme.notes.v1.NotesService",
            method="Delete",
            request_fqn=".acme.notes.v1.DeleteRequest",
            response_fqn=".google.protobuf.Empty",
            pp_offset=0x10,
        ),
        extractor.GrpcSignature(
            path="/acme.notes.v1.NotesService/DeleteLocal",
            service="acme.notes.v1.NotesService",
            method="DeleteLocal",
            request_fqn=".acme.notes.v1.DeleteRequest",
            response_fqn=".acme.notes.v1.LocalEmptyResponse",
            pp_offset=0x30,
        ),
    ]


def test_csv_is_deterministic_and_contains_source_offset(tmp_path: Path) -> None:
    pp_path = _write(
        tmp_path,
        "pp.txt",
        """\
[pp+0x28] TypeArguments: <ZRequest, ZResponse>
[pp+0x30] String: "/z.v1.ZService/Zed"
[pp+0x8] TypeArguments: <ARequest, AResponse>
[pp+0x10] String: "/a.v1.AService/Alpha"
""",
    )
    symbols_path = _write(
        tmp_path,
        "addNames.py",
        _symbol("1000", "z.v1$z.pb_ZRequest::_i_1000")
        + _symbol("1001", "z.v1$z.pb_ZResponse::_i_1001")
        + _symbol("1002", "a.v1$a.pb_ARequest::_i_1002")
        + _symbol("1003", "a.v1$a.pb_AResponse::_i_1003"),
    )

    output = io.StringIO()
    extractor.write_csv(extractor.extract_signatures(pp_path, symbols_path), output)

    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert [row["path"] for row in rows] == [
        "/a.v1.AService/Alpha",
        "/z.v1.ZService/Zed",
    ]
    assert rows[0] == {
        "path": "/a.v1.AService/Alpha",
        "service": "a.v1.AService",
        "method": "Alpha",
        "request_fqn": ".a.v1.ARequest",
        "response_fqn": ".a.v1.AResponse",
        "pp_offset": "0x8",
    }


@pytest.mark.parametrize(
    ("symbols", "expected"),
    [
        ("", "unresolved generated-message type 'Request'"),
        (
            _symbol("1000", "one.v1$one.pb_Request::_i_1000")
            + _symbol("1001", "two.v1$two.pb_Request::_i_1001"),
            "ambiguous generated-message type 'Request'",
        ),
    ],
)
def test_rejects_unresolved_or_ambiguous_symbols(
    tmp_path: Path, symbols: str, expected: str
) -> None:
    pp_path = _write(
        tmp_path,
        "pp.txt",
        """\
[pp+0x8] TypeArguments: <Request, Response>
[pp+0x10] String: "/acme.v1.Service/Call"
""",
    )
    symbols_path = _write(
        tmp_path,
        "addNames.py",
        symbols + _symbol("2000", "google.protobuf$empty.pb_Response::_i_2000"),
    )

    with pytest.raises(extractor.ExtractionError, match=expected):
        extractor.extract_signatures(pp_path, symbols_path)


def test_rejects_conflicting_type_arguments_for_one_path(tmp_path: Path) -> None:
    pp_path = _write(
        tmp_path,
        "pp.txt",
        """\
[pp+0x8] TypeArguments: <Request, Response>
[pp+0x10] String: "/acme.v1.Service/Call"
[pp+0x18] TypeArguments: <OtherRequest, Response>
[pp+0x20] String: "/acme.v1.Service/Call"
""",
    )

    with pytest.raises(extractor.ExtractionError, match="conflicting type arguments"):
        extractor.parse_pp_methods(pp_path)
