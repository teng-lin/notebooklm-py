"""Tag 3 remains experimental, separate from processing and Drive status."""

import json
from pathlib import Path

import pytest

from notebooklm._web.rows.source_models import source_from_row
from notebooklm._web.rows.sources import SourceRow
from notebooklm.types import Source


@pytest.mark.parametrize("code", [1, 3, 5, 6, 999])
def test_web_retains_positive_scalar_failure_diagnostic(code):
    row = SourceRow.from_entry([["id"], "Title", [], [None, 3, [None] * 6 + [[code]]]])
    source = source_from_row(Source, row)
    assert source.experimental_failure_code == code
    assert source.is_error


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        [None, 3],
        [None, 3, None],
        [None, 3, 1],
        [None, 3, [None] * 7],
        [None, 3, [None] * 6 + [[]]],
        [None, 3, [None] * 6 + [[True]]],
        [None, 3, [None] * 6 + [["1"]]],
        [None, 3, [None] * 6 + [[0]]],
        [None, 3, True],
        [None, 3, "1"],
        [None, 3, [1]],
        [None, 3, 0],
        [None, 3, -1],
        [None, 2, 1],
        [None, 2, [None, None, None, []]],
    ],
)
def test_web_unknown_shapes_and_healthy_rows_have_no_failure_claim(settings):
    row = SourceRow.from_entry([["id"], "Title", [], settings])
    assert row.experimental_failure_code is None


@pytest.mark.parametrize(
    ("status", "wire", "expected"),
    [
        (3, b"\x1a\x04\x3a\x02\x08\x01", 1),
        (3, b"\x1a\x04\x3a\x02\x08\x03", 3),
        (3, b"\x1a\x04\x3a\x02\x08\x00", None),
        (3, b"\x1a\x02\x38\x01", None),
        (3, b"\x1a\x06\x3a\x04\x08\x01\x08\x01", None),
        (3, b"\x18\x01", None),
        (3, b"\x18\x00", None),
        (2, b"\x18\x01", None),
        (3, b"\x1a\x01\x01", None),
        (3, b"\x18\x01\x18\x01", None),
        (3, b"", None),
    ],
)
def test_android_unknown_field_scalar_only(status, wire, expected):
    pytest.importorskip("google.protobuf")
    from notebooklm._android.codecs.source_failure import experimental_failure_code
    from notebooklm._android.proto.google.internal.labs.tailwind.v1.source_settings_pb2 import (
        SourceSettings,
    )

    settings = SourceSettings(status=status)
    settings.MergeFromString(wire)
    assert experimental_failure_code(settings) == expected


def test_live_connection_failure_settings_decode():
    captured = json.loads(
        (Path(__file__).parent / "fixtures/source_failure_settings.json").read_text()
    )
    row = SourceRow.from_entry([["id"], "Unreachable URL", [], captured["settings"]])
    assert source_from_row(Source, row).experimental_failure_code == 1
