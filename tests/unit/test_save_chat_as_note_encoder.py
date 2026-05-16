"""Unit tests for the saved-from-chat CREATE_NOTE encoder (issue #660).

These tests exercise ``_mind_map.build_save_chat_as_note_params`` and pin
its output against the wire-captured payload at
``tests/unit/fixtures/save_chat_as_note_create_note_request.json``.

The golden test is the most important one: a byte-exact match (via
deep-equal of the decoded JSON structure) guarantees that the encoder
produces the same payload Google's web UI sends when its "Save to note"
button is clicked. Drift from that payload risks the server silently
dropping citation anchors and reverting the note to plain text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebooklm._mind_map import (
    _CITATION_MARKER_RE,
    _strip_citation_markers,
    build_save_chat_as_note_params,
)
from notebooklm.types import ChatReference

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_request_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "save_chat_as_note_create_note_request.json").read_text())


class TestStripCitationMarkers:
    """Tests for the ``[N]`` marker stripping helper."""

    def test_single_marker_with_leading_space(self):
        clean, positions = _strip_citation_markers("One fruit mentioned is apples [1].")
        assert clean == "One fruit mentioned is apples."
        # Marker [1] was at "apples"+space+marker position; after stripping,
        # the position in the clean text where the marker was is just after
        # "apples" = char 29 (exclusive end of "One fruit mentioned is apples").
        assert positions == [(1, 29)]

    def test_no_markers(self):
        clean, positions = _strip_citation_markers("plain answer, no citations")
        assert clean == "plain answer, no citations"
        assert positions == []

    def test_multiple_markers_segmented(self):
        clean, positions = _strip_citation_markers("X is true [1]. Y is also true [2].")
        # Stripping " [1]" then " [2]" yields "X is true. Y is also true."
        assert clean == "X is true. Y is also true."
        # [1] anchors clean[0..9] = "X is true"; [2] anchors clean[0..25] = the whole
        # text before the trailing period
        assert positions == [(1, 9), (2, 25)]

    def test_adjacent_markers(self):
        clean, positions = _strip_citation_markers("X [1][2].")
        # First match consumes " [1]" with leading space, leaving "X[2]."
        # Second match consumes "[2]" with NO leading space (regex space is optional)
        assert clean == "X."
        assert positions == [(1, 1), (2, 1)]

    def test_marker_at_start(self):
        clean, positions = _strip_citation_markers("[1] starts here.")
        # No leading space; the optional " " doesn't match
        assert clean == " starts here."
        assert positions == [(1, 0)]

    def test_regex_compiled(self):
        # Sanity: ensure the module-level compile didn't drift.
        assert _CITATION_MARKER_RE.pattern == r" ?\[(\d+)\]"


class TestBuildSaveChatAsNoteParamsGolden:
    """Byte-exact golden tests against the captured curl payload.

    The fixture was decoded from the user-supplied curl request sent by
    the NotebookLM web UI when "Save to note" was clicked on a chat
    answer with a single citation. The encoder MUST match it exactly:
    if our output diverges in shape, ordering, or scalar types
    (e.g. emitting ``false`` where the wire uses ``0``), the server may
    silently strip citation anchors.
    """

    def test_golden_single_citation(self):
        fixture = _load_request_fixture()
        expected_params = fixture["params"]
        notebook_id = expected_params[0]
        answer_text = expected_params[1]
        title = expected_params[4]

        # Reconstruct the ChatReference the web UI would have built from
        # the same chat response. Slot [3][0] of the captured request
        # carries the source-passage descriptor; we read source_id /
        # chunk_id / span / cited_text out of it.
        passage = expected_params[3][0]
        cited_text = passage[4][0][0][2][0][0][2][0]  # follow the nested wrapper
        # passage[3] = [[null, start, end]]; passage[5] = [[[passage_id], source_id]];
        # passage[6] = [chunk_id]
        start_char = passage[3][0][1]
        end_char = passage[3][0][2]
        passage_id = passage[5][0][0][0]
        source_id = passage[5][0][1]
        chunk_id = passage[6][0]

        references = [
            ChatReference(
                source_id=source_id,
                citation_number=1,
                cited_text=cited_text,
                start_char=start_char,
                end_char=end_char,
                chunk_id=chunk_id,
                passage_id=passage_id,
            )
        ]

        actual_params = build_save_chat_as_note_params(notebook_id, answer_text, references, title)

        # Deep-equal of the structured form. We also check the compact
        # JSON serialization to catch boolean / null divergence that
        # deep-equal would miss (Python's `False == 0` quirk).
        assert actual_params == expected_params

        # Serialize both with the same separators the rpc encoder uses
        # (rpc/encoder.py:39: separators=(",", ":")) and compare strings.
        # This is the strict byte-exact check.
        actual_json = json.dumps(actual_params, separators=(",", ":"))
        expected_json = json.dumps(expected_params, separators=(",", ":"))
        assert actual_json == expected_json

    def test_passage_id_falls_back_to_chunk_id_when_unset(self):
        """When ChatReference.passage_id is None (the production path,
        since the chat parser doesn't currently surface this UUID), the
        encoder fills the 4th-UUID slot with chunk_id as a placeholder."""
        ref = ChatReference(
            source_id="src-1",
            citation_number=1,
            cited_text="text",
            start_char=0,
            end_char=4,
            chunk_id="chunk-1",
            passage_id=None,
        )
        params = build_save_chat_as_note_params("nb-id", "X [1].", [ref], "Title")
        # Slot [3][0][5][0] = [[passage_id], source_id]
        # With passage_id=None, the encoder uses chunk_id.
        passage_descriptor = params[3][0]
        assert passage_descriptor[5][0][0] == ["chunk-1"]
        assert passage_descriptor[5][0][1] == "src-1"
        # The keyed version in rich_content uses the same placeholder.
        assert params[5][3][0][1][5][0][0] == ["chunk-1"]

    def test_passage_id_overrides_chunk_id_when_set(self):
        """When ChatReference.passage_id IS set (forward-compat path),
        the encoder uses it for the 4th-UUID slot regardless of chunk_id."""
        ref = ChatReference(
            source_id="src-1",
            citation_number=1,
            cited_text="text",
            start_char=0,
            end_char=4,
            chunk_id="chunk-1",
            passage_id="passage-real-uuid",
        )
        params = build_save_chat_as_note_params("nb-id", "X [1].", [ref], "Title")
        passage_descriptor = params[3][0]
        assert passage_descriptor[5][0][0] == ["passage-real-uuid"]
        assert passage_descriptor[5][0][1] == "src-1"

    def test_encoder_serializes_booleans_as_zero_not_false(self):
        """Regression guard: every "rendering flags" slot the ENCODER
        emits must serialize to ``0`` (integer), never ``false`` (boolean).
        Python's ``json.dumps(False)`` emits ``false`` while
        ``json.dumps(0)`` emits ``0`` — the wire payload uses ``0``, and
        the server's strict request channel won't normalize this. We
        assert on the encoder's actual output (not on the static fixture)
        so the check can't pass tautologically: if a future change makes
        any slot emit ``True``/``False``, this test catches it."""
        ref = ChatReference(
            source_id="src-1",
            citation_number=1,
            cited_text="passage text",
            start_char=0,
            end_char=12,
            chunk_id="chunk-1",
        )
        params = build_save_chat_as_note_params(
            "nb-id", "Answer with citation [1].", [ref], "Title"
        )
        actual_json = json.dumps(params, separators=(",", ":"))
        assert "false" not in actual_json
        assert "true" not in actual_json
        assert "[0,0,0,null,null,null,null,0,0]" in actual_json


class TestBuildSaveChatAsNoteParamsBehavior:
    """Behavioural tests beyond the single-citation golden."""

    def _make_ref(self, n: int, chunk_id: str, source_id: str) -> ChatReference:
        return ChatReference(
            source_id=source_id,
            citation_number=n,
            cited_text=f"passage text {n}",
            start_char=0,
            end_char=14,
            chunk_id=chunk_id,
        )

    def test_empty_references_raises(self):
        with pytest.raises(ValueError, match="non-empty references"):
            build_save_chat_as_note_params("nb-id", "no citations here.", [], "Title")

    def test_references_without_chunk_id_raises(self):
        ref = ChatReference(source_id="src-1", chunk_id=None)
        with pytest.raises(ValueError, match="chunk_id"):
            build_save_chat_as_note_params("nb-id", "X [1].", [ref], "Title")

    def test_multi_citation_produces_one_anchor_per_marker(self):
        refs = [
            self._make_ref(1, "chunk-a", "src-a"),
            self._make_ref(2, "chunk-b", "src-b"),
        ]
        params = build_save_chat_as_note_params(
            "nb-id", "First fact [1]. Second fact [2].", refs, "Title"
        )
        # Two unique chunks → two source_passages entries
        assert len(params[3]) == 2
        # Two markers → two chunk_refs in rich_content[0][1]
        chunk_refs = params[5][0][1]
        assert len(chunk_refs) == 2
        # Cumulative spans: [1] anchors clean[0..10] = "First fact"
        # [2] anchors clean[0..23] = "First fact. Second fact" (23 chars)
        assert chunk_refs[0] == [["chunk-a"], [None, 0, 10]]
        assert chunk_refs[1] == [["chunk-b"], [None, 0, 23]]

    def test_dedupes_source_passages_by_chunk_id(self):
        # Same chunk_id referenced twice → only one source_passages entry,
        # but each [N] marker still gets its own anchor.
        ref = self._make_ref(1, "chunk-shared", "src-shared")
        refs = [ref, ref]  # caller may legitimately repeat
        params = build_save_chat_as_note_params("nb-id", "A [1] B [2]", refs, "Title")
        assert len(params[3]) == 1  # deduped
        # Two markers found in answer_text → two chunk_refs
        # (the lookup of [2] falls back to references[1] = same ref)
        assert len(params[5][0][1]) == 2

    def test_marker_without_matching_reference_is_skipped(self, caplog):
        # Answer has [1] and [99] but references only covers [1]
        refs = [self._make_ref(1, "chunk-1", "src-1")]
        with caplog.at_level("WARNING"):
            params = build_save_chat_as_note_params("nb-id", "A [1] B [99]", refs, "Title")
        chunk_refs = params[5][0][1]
        # Only one anchor — the [99] marker is silently dropped with a warning
        assert len(chunk_refs) == 1
        assert any("[99]" in record.message or "99" in record.message for record in caplog.records)

    def test_seven_element_params_shape(self):
        """Sanity: top-level params is always exactly 7 elements."""
        refs = [self._make_ref(1, "c1", "s1")]
        params = build_save_chat_as_note_params("nb-id", "X [1].", refs, "T")
        assert len(params) == 7
        assert params[2] == [2]  # mode flag
        assert params[6] == [2]  # trailer flag
        assert params[0] == "nb-id"
        assert params[1] == "X [1]."
        assert params[4] == "T"

    def test_rich_content_has_five_slots(self):
        refs = [self._make_ref(1, "c1", "s1")]
        params = build_save_chat_as_note_params("nb-id", "X [1].", refs, "T")
        rich = params[5]
        assert len(rich) == 5
        assert rich[1] is None  # always-null slot
        assert rich[2] is None  # always-null slot
        assert rich[4] == 1  # trailer flag

    def test_source_span_and_local_text_wrapper_diverge_for_nonzero_start(self):
        """Regression: source-document span (slot [3]) uses ref.start_char
        / ref.end_char (absolute offsets in the source), but the text-passage
        wrapper (slot [4]) uses LOCAL offsets [0, len(cited_text)]. The
        captured single-citation fixture has start_char=0 and end_char ==
        len(cited_text), so they coincidentally match — but real refs from
        the chat parser commonly have non-zero source offsets (e.g.
        chars 100..200 in a long document). If the encoder used ref.end_char
        for both, the local wrapper would emit ``[[0, 200, ...]]`` for a
        100-char passage, breaking server-side hover anchoring."""
        ref = ChatReference(
            source_id="src-1",
            citation_number=1,
            cited_text="exactly-ten",  # 11 chars, deliberately != source span
            start_char=100,
            end_char=111,  # source offsets
            chunk_id="chunk-1",
        )
        params = build_save_chat_as_note_params("nb-id", "X [1].", [ref], "Title")
        passage = params[3][0]
        # Source span uses absolute offsets.
        assert passage[3] == [[None, 100, 111]]
        # Text wrapper uses LOCAL offsets — [0, len(cited_text)].
        passage_group = passage[4][0]
        assert passage_group[0][0] == 0
        assert passage_group[0][1] == 11  # len("exactly-ten")
        # The deeply-nested inner mirror must also use the local end.
        inner_offsets = passage_group[0][2][0][0]
        assert inner_offsets[0] == 0
        assert inner_offsets[1] == 11

    def test_empty_cited_text_collapses_span(self):
        """Regression: when ref.cited_text is empty, span must collapse to
        [0, 0] rather than emitting an invalid [None, start, 0] where
        start > 0 (e.g. start_char=10 + cited_text='')."""
        ref = ChatReference(
            source_id="src-1",
            citation_number=1,
            cited_text="",
            start_char=10,
            end_char=20,
            chunk_id="chunk-1",
        )
        params = build_save_chat_as_note_params("nb-id", "X [1].", [ref], "Title")
        passage = params[3][0]
        # Source span collapses to [0, 0], not [10, 0].
        assert passage[3] == [[None, 0, 0]]
        # Local text wrapper end is also 0.
        assert passage[4][0][0][1] == 0

    def test_title_passthrough(self):
        refs = [self._make_ref(1, "c1", "s1")]
        params = build_save_chat_as_note_params("nb-id", "X [1].", refs, "My Custom Title")
        assert params[4] == "My Custom Title"
