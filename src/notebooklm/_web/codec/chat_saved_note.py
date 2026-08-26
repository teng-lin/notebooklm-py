"""Web encoder for the seven-element saved-from-chat note variant.

Owns the CREATE_NOTE wire format used by the web UI's "Save to note"
button on a chat answer (issue #660). It is consumed by the semantic web
binding for ``chat.save_note`` and is unrelated to the plain/mind-map
CREATE_NOTE variant.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..._semantic.records import ChatReferenceRecord
from ..._types.documents import utf16_len

logger = logging.getLogger(__name__)


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

# Matches a citation marker plus the single space that typically precedes it
# in the answer text (e.g. " [1]"). The leading space is *optional* so a
# marker at the very start of the answer or directly after punctuation still
# matches. Captures the citation number for downstream lookup.
_CITATION_MARKER_RE = re.compile(r" ?\[(\d+)\]")


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


def _build_source_passage_descriptor(ref: ChatReferenceRecord) -> list[Any]:
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


def _strip_citation_markers(answer_text: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip ``[N]`` citation markers from ``answer_text``.

    Returns the cleaned text plus a list of ``(citation_number,
    position_in_clean_text)`` tuples in marker-appearance order. The
    position is where the marker WAS in the clean text — i.e. the
    exclusive end of the text the marker was anchoring — counted in **UTF-16
    code units**, because that is the unit the TailwindDoc ranges these
    positions are written into use (#2120).

    Example::

        >>> _strip_citation_markers("One fruit is apples [1].")
        ('One fruit is apples.', [(1, 19)])

    The space before ``[N]`` is consumed when present (matches the web
    UI's behavior in the captured fixture: clean text drops the space).
    """
    positions: list[tuple[int, int]] = []
    clean_parts: list[str] = []
    last_end = 0
    clean_offset = 0
    for match in _CITATION_MARKER_RE.finditer(answer_text):
        chunk = answer_text[last_end : match.start()]
        clean_parts.append(chunk)
        clean_offset += utf16_len(chunk)
        positions.append((int(match.group(1)), clean_offset))
        last_end = match.end()
    clean_parts.append(answer_text[last_end:])
    return "".join(clean_parts), positions


def _resolve_reference(
    references: tuple[ChatReferenceRecord, ...],
    citation_number: int,
) -> ChatReferenceRecord | None:
    """Look up the ChatReference that backs citation marker ``[N]``.

    Prefers an exact ``citation_number`` match. The positional fallback
    (``references[N-1]``) applies ONLY when that positional candidate has no
    ``citation_number`` set — the legacy unset-number contract it was built
    for. It must NOT fire for a *numbered* list with a hole: the wire parser
    keeps raw ordinals when it skips a malformed citation row, so after
    ``[good#1, skipped#2, good#3]`` a positional fallback for marker ``[2]``
    would anchor raw citation #3 — a WRONG anchor. Returns ``None`` instead
    (the caller skips that marker's anchor with a warning): a missing anchor
    is recoverable, a mis-anchored note is silently wrong.
    """
    for ref in references:
        if ref.citation_number == citation_number and ref.chunk_id:
            return ref
    idx = citation_number - 1
    if 0 <= idx < len(references):
        candidate = references[idx]
        if candidate.citation_number is None and candidate.chunk_id:
            return candidate
    return None


def build_save_note_params(
    notebook_id: str,
    answer_text: str,
    references: tuple[ChatReferenceRecord, ...],
    title: str,
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

    clean_answer, marker_positions = _strip_citation_markers(answer_text)

    # Per-unique-chunk_id source-passage descriptors, in first-seen order.
    seen_chunks: list[str] = []
    chunk_to_ref: dict[str, ChatReferenceRecord] = {}
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
    for citation_number, position in marker_positions:
        anchor_ref = _resolve_reference(references, citation_number)
        if anchor_ref is None or anchor_ref.chunk_id is None:
            logger.warning(
                "Citation marker [%d] in answer has no matching reference; "
                "skipping anchor for this marker",
                citation_number,
            )
            continue
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
