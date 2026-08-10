"""Tests for the Collection value type and its from_api_response builder."""

from __future__ import annotations

import pytest

from notebooklm._types.collections import Collection
from notebooklm.exceptions import UnknownRPCMethodError


def test_from_api_response_populated() -> None:
    # Live-captured (PR #2009): populated members are BARE strings, not the
    # label-style ``[[id], ...]`` wrapped singletons — the wire tuple is only
    # label-shaped while a collection is empty.
    collection = Collection.from_api_response(
        ["Research Q3", ["nb1", "nb2"], "c1", "\U0001f4c1"],
        method_id="I3xc3c",
    )
    assert collection.id == "c1"
    assert collection.name == "Research Q3"
    assert collection.emoji == "\U0001f4c1"
    assert collection.notebook_ids == ["nb1", "nb2"]


def test_empty_emoji_becomes_none_and_empty_membership() -> None:
    collection = Collection.from_api_response(["Empty", None, "c1", ""])
    assert collection.emoji is None
    assert collection.notebook_ids == []


def test_drift_propagates_from_row_adapter() -> None:
    with pytest.raises(UnknownRPCMethodError):
        Collection.from_api_response(["Bad", None, 5, ""])


def test_wrapped_singleton_members_are_drift_not_a_tolerated_form() -> None:
    # The label-style wrapped shape is NOT a legitimate alternate encoding for
    # collections — it never appears on the wire — so it must raise, not
    # silently decode, per the strict-decode default (ADR-0019/0011).
    with pytest.raises(UnknownRPCMethodError):
        Collection.from_api_response(["Research Q3", [["nb1"], ["nb2"]], "c1", ""])
