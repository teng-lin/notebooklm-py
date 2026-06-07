"""Strict-decode tests for the LabelRow positional adapter."""

from __future__ import annotations

import pytest

from notebooklm._row_adapters.labels import LabelRow
from notebooklm.exceptions import UnknownRPCMethodError


def test_decodes_full_tuple() -> None:
    row = LabelRow.from_label_tuple(
        ["Topic", [["s1"], ["s2"]], "l1", "\U0001f4c1"], method_id="agX4Bc"
    )
    assert row.name == "Topic"
    assert row.source_ids == ("s1", "s2")
    assert row.id == "l1"
    assert row.emoji == "\U0001f4c1"


def test_empty_sources_none_is_empty_tuple_not_drift() -> None:
    row = LabelRow.from_label_tuple(["New Label", None, "l1", ""])
    assert row.source_ids == ()
    assert row.emoji == ""


@pytest.mark.parametrize(
    "tuple_",
    [
        pytest.param(["Topic", None, "l1"], id="short_tuple_missing_emoji"),
        pytest.param([123, None, "l1", ""], id="non_str_name"),
        pytest.param(["Topic", None, 5, ""], id="non_str_id"),
        pytest.param(["Topic", ["s1"], "l1", ""], id="member_not_wrapped"),
        pytest.param(["Topic", [[]], "l1", ""], id="member_empty_list"),
        pytest.param(["Topic", [["s1", "extra"]], "l1", ""], id="member_over_long"),
        pytest.param(["Topic", [[5]], "l1", ""], id="member_non_str_id"),
        pytest.param(["Topic", "nope", "l1", ""], id="sources_wrong_type"),
        pytest.param(["Topic", None, "l1", 5], id="non_str_emoji"),
    ],
)
def test_drift_raises(tuple_: list[object]) -> None:
    with pytest.raises(UnknownRPCMethodError):
        LabelRow.from_label_tuple(tuple_, method_id="agX4Bc")
