"""Tests for ``notebooklm._app.resolve``."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from notebooklm._app.resolve import (
    AmbiguousIdError,
    Resolution,
    resolve_ref,
    validate_id,
)
from notebooklm.exceptions import NotebookLMError, ValidationError


@dataclass(frozen=True)
class Item:
    id: str
    title: str | None = None


def _id_of(item: Item) -> str:
    return item.id


def _title_of(item: Item) -> str | None:
    return item.title


# A canonical 8-4-4-4-12 UUID for the full-id fast-path.
FULL_ID = "abc12345-6789-4abc-def0-1234567890ab"


# --- validate_id ------------------------------------------------------------


def test_validate_id_strips_and_returns() -> None:
    assert validate_id("  abc  ") == "abc"


def test_validate_id_default_name() -> None:
    assert validate_id("xyz", "notebook") == "xyz"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_validate_id_raises_validation_error_on_blank(blank: str) -> None:
    with pytest.raises(ValidationError) as caught:
        validate_id(blank, "notebook")
    assert "notebook ID cannot be empty" in str(caught.value)


def test_validate_id_raises_public_validation_error_type() -> None:
    # The whole point of the _app core is that it raises the public exception
    # hierarchy (so adapters can re-shape it), never a transport exception. Pin
    # the exact raised type rather than a base class to lock the contract.
    with pytest.raises(ValidationError) as caught:
        validate_id("")
    assert type(caught.value) is ValidationError
    assert isinstance(caught.value, NotebookLMError)


# --- resolve_ref ------------------------------------------------------------


def test_resolve_ref_full_id_fast_path_skips_items() -> None:
    # An empty item list still resolves a full UUID — proving no scan happened.
    result = resolve_ref(FULL_ID, [], id_of=_id_of)

    assert result == Resolution(id=FULL_ID, matched_title=None)


def test_resolve_ref_full_id_returned_verbatim_even_with_items() -> None:
    items = [Item(id="other")]
    result = resolve_ref(FULL_ID, items, id_of=_id_of, title_of=_title_of)

    assert result.id == FULL_ID
    assert result.matched_title is None


def test_resolve_ref_exact_match_case_insensitive() -> None:
    items = [Item(id="ABCDEF", title="A"), Item(id="abcdef00", title="B")]
    # "abcdef" is an exact (case-insensitive) match AND a prefix of "abcdef00";
    # exact must win and not report ambiguity.
    result = resolve_ref("abcdef", items, id_of=_id_of, title_of=_title_of)

    assert result.id == "ABCDEF"
    assert result.matched_title is None


def test_resolve_ref_unique_prefix_carries_title() -> None:
    items = [Item(id="abc123", title="First"), Item(id="zzz999", title="Second")]
    result = resolve_ref("abc", items, id_of=_id_of, title_of=_title_of)

    assert result.id == "abc123"
    assert result.matched_title == "First"


def test_resolve_ref_unique_prefix_without_title_accessor() -> None:
    items = [Item(id="abc123")]
    result = resolve_ref("abc", items, id_of=_id_of)

    assert result.id == "abc123"
    assert result.matched_title is None


def test_resolve_ref_ambiguous_prefix_raises_with_candidates() -> None:
    items = [
        Item(id="abc111", title="One"),
        Item(id="abc222", title="Two"),
    ]
    with pytest.raises(AmbiguousIdError) as caught:
        resolve_ref("abc", items, id_of=_id_of, title_of=_title_of)

    err = caught.value
    assert err.partial_id == "abc"
    assert set(err.candidate_ids) == {"abc111", "abc222"}
    # AmbiguousIdError must remain catchable as ValidationError.
    assert isinstance(err, ValidationError)
    assert "Ambiguous ID 'abc'" in str(err)


def test_resolve_ref_ambiguous_truncates_to_five_candidates() -> None:
    items = [Item(id=f"abc{i}", title=f"T{i}") for i in range(7)]
    with pytest.raises(AmbiguousIdError) as caught:
        resolve_ref("abc", items, id_of=_id_of, title_of=_title_of)

    # All 7 ids are tracked; the message lists at most 5 + a "more" line.
    assert len(caught.value.candidate_ids) == 7
    assert "and 2 more" in str(caught.value)


def test_resolve_ref_no_match_raises_validation_error() -> None:
    items = [Item(id="abc123")]
    with pytest.raises(ValidationError) as caught:
        resolve_ref("zzz", items, id_of=_id_of)
    assert "No item found starting with 'zzz'" in str(caught.value)
    assert not isinstance(caught.value, AmbiguousIdError)


def test_resolve_ref_blank_token_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        resolve_ref("   ", [Item(id="abc")], id_of=_id_of)


def test_resolve_ref_partial_token_with_empty_items_is_no_match() -> None:
    # A non-full-id token against an empty list takes the no-match path (not the
    # fast-path), raising the plain "no match" ValidationError, not ambiguity.
    with pytest.raises(ValidationError) as caught:
        resolve_ref("abc", [], id_of=_id_of)
    assert "No item found starting with 'abc'" in str(caught.value)
    assert not isinstance(caught.value, AmbiguousIdError)


def test_resolve_ref_strips_token_before_matching() -> None:
    items = [Item(id="abc123", title="First")]
    result = resolve_ref("  abc  ", items, id_of=_id_of, title_of=_title_of)

    assert result.id == "abc123"
