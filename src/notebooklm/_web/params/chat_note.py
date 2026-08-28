"""Web saved-from-chat note encoder (private; ADR-0013).

Owns the positional CREATE_NOTE payload used by the web UI's "Save to note"
button. Text/citation preparation lives in the backend-neutral ChatAPI.
"""

from __future__ import annotations

from typing import Any

from ..._types.documents import utf16_len
from ...types import ChatReference

# Rendering-flag trailer used inside every text-passage wrapper of the
# saved-from-chat CREATE_NOTE payload (issue #660). Integers, NOT booleans:
# json.dumps(False) emits ``false`` but the captured wire payload uses ``0``,
# and the byte-exact golden test (``test_encoder_serializes_booleans_as_zero
# _not_false``) guards this invariant.
#
# Stored as a tuple so module-level identity is immutable; call sites copy
# into a fresh list via ``list(_TEXT_RENDER_FLAGS)`` when embedding so that
# downstream mutation of an emitted params tree can't corrupt this constant.
_TEXT_RENDER_FLAGS: tuple[int | None, ...] = (0, 0, 0, None, None, None, None, 0, 0)


def _build_passage_group(text: str, end_char: int) -> list[Any]:
    """Build a single passage-group (text + offsets + render flags).

    Used both as the content of slot ``[5][0][0]`` (the cleaned-answer
    passage group) and as one entry of slot ``[3][0][4]`` (each source's
    passage-group list).
    """
    return [
        [
            0,
            end_char,
            [[[0, end_char, [text, list(_TEXT_RENDER_FLAGS)]]], [None, 1]],
        ]
    ]


def _build_source_passage_descriptor(ref: ChatReference) -> list[Any]:
    """Build one entry of the ``source_passages`` array (slot ``[3]``).

    The 4th-UUID slot (``[3][0][5][0][0]`` in wire terms) carries a
    per-passage UUID that NotebookLM's web UI sends but our chat parser
    does not currently surface (it's absent from the streaming chat
    response shape — see ``ChatReference.passage_id`` docstring). We use
    ``ref.passage_id`` when set; otherwise fall back to ``ref.chunk_id``
    as a best-effort placeholder. Empirical observation (issue #660 PR):
    the server accepts ``chunk_id`` here and citation anchors still work.
    """
    cited_text = ref.cited_text or ""
    # Source-document span (slot [3]) is absolute in the source's coordinate
    # space. Text-wrapper offsets (slot [4]) are LOCAL to cited_text — they
    # always start at 0 and end at its width. Both are UTF-16 code units, which
    # is why the width comes from ``utf16_len`` and never ``len`` (#2120). The
    # captured fixture has start_char=0 and end_char equal to that width,
    # masking the distinction in the golden test; real chat refs commonly have
    # non-zero source offsets, so the two ``end`` values diverge.
    if cited_text:
        source_start = ref.start_char if ref.start_char is not None else 0
        source_end = ref.end_char if ref.end_char is not None else utf16_len(cited_text)
    else:
        # Empty cited_text: collapse the source span to [0, 0] to avoid
        # emitting an invalid ``[None, start, 0]`` when start>0.
        source_start = 0
        source_end = 0
    # UTF-16 code units, like every other TailwindDoc offset (#2120). Reachable
    # now that ``cited_text`` spans the whole fragment rather than its first
    # block: a single emoji anywhere in it would end this local range one unit
    # short and misalign — or get the server to reject — the saved note.
    local_end = utf16_len(cited_text)
    # Use explicit `is not None` check so an empty-string passage_id
    # (falsy but explicitly set by a caller) doesn't silently fall
    # through to chunk_id.
    fourth_uuid = ref.passage_id if ref.passage_id is not None else ref.chunk_id
    return [
        None,
        None,
        None,
        [[None, source_start, source_end]],
        [_build_passage_group(cited_text, local_end)],
        [[[fourth_uuid], ref.source_id]],
        [ref.chunk_id],
    ]


def build_save_chat_as_note_params(
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
    title: str,
    *,
    clean_answer: str,
    citation_anchors: list[tuple[ChatReference, int]],
) -> list[Any]:
    """Build CREATE_NOTE params for the saved-from-chat variant.

    Produces the 7-element params array used by the web UI's "Save to
    note" button. The resulting note has hover-anchored ``[N]`` citations
    in the NotebookLM UI.

    Args:
        notebook_id: Target notebook UUID.
        answer_text: AI answer text WITH ``[N]`` citation markers.
        references: Citation list from ``AskResult.references``. Must be
            non-empty — callers with no citations should use plain
            ``notes.create()`` instead.
        title: User-requested note title. The server may apply
            smart-title generation for ``[2]``-mode notes; the title in
            the returned ``Note`` reflects the server-assigned value.

    Returns:
        7-element params list ready to pass to ``RPCMethod.CREATE_NOTE``.

    Raises:
        ValueError: If ``references`` is empty.
    """
    if not references:
        raise ValueError(
            "save_chat_answer_as_note requires non-empty references; "
            "use notes.create() for plain-text notes."
        )

    # Per-unique-chunk_id source-passage descriptors, in first-seen order.
    seen_chunks: list[str] = []
    chunk_to_ref: dict[str, ChatReference] = {}
    for ref in references:
        if ref.chunk_id and ref.chunk_id not in chunk_to_ref:
            seen_chunks.append(ref.chunk_id)
            chunk_to_ref[ref.chunk_id] = ref
    if not seen_chunks:
        raise ValueError(
            "save_chat_answer_as_note requires references with chunk_id set; "
            "got references without any usable chunk_id."
        )
    # Build the source-passage descriptor for each unique chunk ONCE and
    # reuse it in both ``source_passages`` (slot [3]) and
    # ``source_passages_keyed`` (slot [5][3] of rich_content). The two
    # consumers want the same descriptor wrapped differently; building
    # twice is purely wasted allocation work for large citation sets.
    descriptors = {c: _build_source_passage_descriptor(chunk_to_ref[c]) for c in seen_chunks}
    source_passages = [descriptors[c] for c in seen_chunks]

    # Cleaned-answer passage group.
    answer_segments = _build_passage_group(clean_answer, utf16_len(clean_answer))

    # Per-marker chunk anchors. Cumulative-span heuristic: each [N] anchors
    # clean_text[0..position_of_marker]. This matches the single-citation
    # capture exactly; multi-citation behavior is unverified — see issue #660
    # follow-up. We emit one anchor per [N] marker; markers without a
    # resolvable reference are skipped with a logged warning.
    chunk_refs: list[Any] = []
    for anchor_ref, position in citation_anchors:
        assert anchor_ref.chunk_id is not None
        chunk_refs.append([[anchor_ref.chunk_id], [None, 0, position]])

    # source_passages_keyed: same descriptors as slot [3], each wrapped
    # with its chunk_id as a leading key (slot [5][3] of rich_content).
    # Reuse the cached descriptors built above so we don't pay the build
    # cost twice per chunk.
    source_passages_keyed = [[[c], descriptors[c]] for c in seen_chunks]

    rich_content = [
        [answer_segments, chunk_refs],
        None,
        None,
        source_passages_keyed,
        1,
    ]

    return [
        notebook_id,
        answer_text,
        [2],
        source_passages,
        title,
        rich_content,
        [2],
    ]


__all__ = ["build_save_chat_as_note_params"]
