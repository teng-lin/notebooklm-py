"""Tests for the Label value type and its from_api_response builder."""

from __future__ import annotations

import pytest

from notebooklm._types.labels import Label
from notebooklm.exceptions import UnknownRPCMethodError


def test_from_api_response_populated() -> None:
    label = Label.from_api_response(
        ["Topic", [["s1"], ["s2"]], "l1", "\U0001f4c1"],
        notebook_id="nb_1",
        method_id="agX4Bc",
    )
    assert label.id == "l1"
    assert label.name == "Topic"
    assert label.notebook_id == "nb_1"
    assert label.emoji == "\U0001f4c1"
    assert label.source_ids == ["s1", "s2"]


def test_empty_emoji_becomes_none() -> None:
    label = Label.from_api_response(["Topic", None, "l1", ""])
    assert label.emoji is None
    assert label.source_ids == []
    assert label.notebook_id is None


def test_notebook_id_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        Label.from_api_response(["Topic", None, "l1", ""], "nb_1")  # type: ignore[misc]


def test_drift_propagates_from_row_adapter() -> None:
    with pytest.raises(UnknownRPCMethodError):
        Label.from_api_response(["Topic", None, 5, ""])
