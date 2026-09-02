"""Cross-type studio ref matching.

``_match_studio_ref`` decides which studio item a user-supplied string refers
to, and ``studio_delete`` acts on the answer. The dangerous case is a note
whose *title* happens to look like an id: a full UUID must never fall through
to title matching, or a delete aimed at an absent id would remove a
title-collision note instead.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastmcp", reason="requires the optional [mcp] extra")

from notebooklm._app.resolve import AmbiguousIdError, validate_id  # noqa: E402
from notebooklm.exceptions import ValidationError  # noqa: E402
from notebooklm.mcp.tools._studio_items import _match_studio_ref  # noqa: E402

UUID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
UUID_C = "cccccccc-3333-4333-8333-cccccccccccc"


#: Studio items carry a note's literal ``"note"`` or an artifact's *hyphenated*
#: kind (``hyphenated_type``), never a generic ``"artifact"`` — keeping that
#: vocabulary here is what makes the scoping cases relate to real behaviour.
NOTE = "note"
SLIDE_DECK = "slide-deck"
MIND_MAP = "mind-map"


def _item(item_id: str, title: str | None, kind: str = NOTE) -> dict[str, Any]:
    return {"id": item_id, "title": title, "type": kind}


def test_a_full_uuid_matches_its_item() -> None:
    items = [_item(UUID_A, "Notes"), _item(UUID_B, "Deck", SLIDE_DECK)]

    assert _match_studio_ref(items, UUID_A, None) == items[0]


def test_a_full_uuid_matches_case_insensitively_and_returns_the_canonical_row() -> None:
    items = [_item(UUID_A, "Notes")]

    match = _match_studio_ref(items, UUID_A.upper(), None)

    assert match is items[0]
    assert match["id"] == UUID_A


def test_an_absent_full_uuid_never_falls_through_to_a_title() -> None:
    """A note titled like a UUID must not absorb a delete aimed at that id."""
    items = [_item(UUID_A, UUID_B)]

    assert _match_studio_ref(items, UUID_B, None) is None


def test_a_unique_hex_prefix_resolves_to_the_canonical_id() -> None:
    items = [_item(UUID_A, "Notes"), _item(UUID_B, "Deck")]

    assert _match_studio_ref(items, "aaaaaaaa", None) == items[0]


def test_an_ambiguous_hex_prefix_is_surfaced_rather_than_guessed() -> None:
    items = [
        _item("aaaaaaaa-1111-4111-8111-000000000001", "One"),
        _item("aaaaaaaa-1111-4111-8111-000000000002", "Two"),
    ]

    with pytest.raises(AmbiguousIdError):
        _match_studio_ref(items, "aaaaaaaa", None)


def test_a_hex_prefix_that_matches_no_id_falls_through_to_the_title() -> None:
    """An all-hex title stays reachable — only a *full* UUID is id-only."""
    items = [_item(UUID_A, "beef")]

    assert _match_studio_ref(items, "beef", None) == items[0]


def test_an_exact_title_matches_case_insensitively() -> None:
    items = [_item(UUID_A, "Weekly Report")]

    assert _match_studio_ref(items, "weekly REPORT", None) == items[0]


def test_an_untitled_item_is_not_matched_by_an_unrelated_ref() -> None:
    items = [_item(UUID_A, None)]

    assert _match_studio_ref(items, "anything", None) is None


def test_the_matcher_itself_does_not_guard_an_empty_ref() -> None:
    """Characterization, not endorsement.

    A missing title normalises to ``""``, so an empty ref *does* match an
    untitled item at this level. That is safe only because the empty ref is
    rejected upstream — see the companion test below. Anything that calls
    ``_match_studio_ref`` directly has to do that rejection itself.
    """
    items = [_item(UUID_A, None)]

    assert _match_studio_ref(items, "", None) is items[0]


@pytest.mark.parametrize(
    "ref", [pytest.param("", id="empty"), pytest.param("   ", id="whitespace-only")]
)
def test_an_empty_ref_is_rejected_before_it_reaches_the_matcher(ref: str) -> None:
    """``resolve_studio_item`` validates the ref first, so a blank one cannot
    reach the title path and select an untitled note for deletion."""
    with pytest.raises(ValidationError):
        validate_id(ref, "item")


def test_a_miss_returns_none_for_the_caller_to_classify() -> None:
    items = [_item(UUID_A, "Notes")]

    assert _match_studio_ref(items, "absent", None) is None


def test_an_ambiguous_title_names_its_candidates() -> None:
    items = [_item(UUID_A, "Draft"), _item(UUID_B, "Draft")]

    with pytest.raises(AmbiguousIdError) as caught:
        _match_studio_ref(items, "Draft", None)

    message = str(caught.value)
    assert "matches 2 items" in message
    assert UUID_A[:12] in message
    assert "more specific title or the id" in message


def test_an_ambiguous_title_listing_is_truncated() -> None:
    items = [_item(f"{index:08d}-1111-4111-8111-aaaaaaaaaaaa", "Draft") for index in range(9)]

    with pytest.raises(AmbiguousIdError) as caught:
        _match_studio_ref(items, "Draft", None)

    message = str(caught.value)
    assert "matches 9 items" in message
    assert "... and 4 more" in message


@pytest.mark.parametrize(
    ("kind", "expected_id"),
    [
        pytest.param(NOTE, UUID_A, id="note-scope"),
        pytest.param(SLIDE_DECK, UUID_B, id="slide-deck-scope"),
    ],
)
def test_a_kind_scope_restricts_the_candidate_set(kind: str, expected_id: str) -> None:
    items = [_item(UUID_A, "Same", NOTE), _item(UUID_B, "Same", SLIDE_DECK)]

    match = _match_studio_ref(items, "Same", kind)

    assert match is not None
    assert match["id"] == expected_id


def test_a_kind_scope_hides_an_item_of_another_type() -> None:
    items = [_item(UUID_A, "Notes", NOTE)]

    assert _match_studio_ref(items, UUID_A, SLIDE_DECK) is None


def test_a_kind_scope_resolves_what_would_otherwise_be_ambiguous() -> None:
    items = [_item(UUID_A, "Draft", NOTE), _item(UUID_B, "Draft", MIND_MAP)]

    match = _match_studio_ref(items, "Draft", NOTE)

    assert match is not None
    assert match["id"] == UUID_A


def test_an_empty_item_list_matches_nothing() -> None:
    assert _match_studio_ref([], UUID_C, None) is None
