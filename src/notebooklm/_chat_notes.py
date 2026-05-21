"""Saved-from-chat note encoder (private; refactor-history.md Step 8, ADR-013).

Owns the CREATE_NOTE wire format used by the web UI's "Save to note"
button on a chat answer (issue #660). Moved out of ``_mind_map.py`` in
Phase 6 so the saved-chat path lives next to ``ChatAPI`` rather than
the mind-map module — the two paths are unrelated despite both being
CREATE_NOTE variants.

The exported call site is :func:`save_chat_answer_as_note`, invoked
exclusively by :meth:`ChatAPI.save_answer_as_note`. All other names
in this module are private (underscore-prefix per ADR-012) and exist
only to keep the encoder testable in isolation.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Protocol

from .rpc import RPCMethod
from .types import Note

if TYPE_CHECKING:
    from .types import ChatReference

logger = logging.getLogger(__name__)


class SaveChatNoteRpc(Protocol):
    """RPC surface needed to persist a saved-from-chat note.

    Mirrors the dispatch shape ``Session.rpc_call`` exposes; a concrete
    :class:`Session` (or any structural equivalent in tests)
    satisfies this protocol.
    """

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
    ) -> Any: ...


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
    # Source-document span (slot [3]) is absolute in the source's char
    # offsets. Text-wrapper offsets (slot [4]) are LOCAL to cited_text —
    # they always start at 0 and end at len(cited_text). The captured
    # fixture has start_char=0 + end_char==len(cited_text), masking this
    # in the golden test; real chat refs commonly have non-zero source
    # offsets, so the two ``end`` values diverge.
    if cited_text:
        source_start = ref.start_char if ref.start_char is not None else 0
        source_end = ref.end_char if ref.end_char is not None else len(cited_text)
    else:
        # Empty cited_text: collapse the source span to [0, 0] to avoid
        # emitting an invalid ``[None, start, 0]`` when start>0.
        source_start = 0
        source_end = 0
    local_end = len(cited_text)
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
    exclusive end of the text the marker was anchoring.

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
        clean_offset += len(chunk)
        positions.append((int(match.group(1)), clean_offset))
        last_end = match.end()
    clean_parts.append(answer_text[last_end:])
    return "".join(clean_parts), positions


def _resolve_reference(
    references: list[ChatReference],
    citation_number: int,
) -> ChatReference | None:
    """Look up the ChatReference that backs citation marker ``[N]``.

    Prefers an exact ``citation_number`` match; falls back to positional
    lookup (``references[N-1]``) when ``citation_number`` is unset on
    the reference. Returns ``None`` if neither path resolves to a
    reference with a usable ``chunk_id``.
    """
    for ref in references:
        if ref.citation_number == citation_number and ref.chunk_id:
            return ref
    idx = citation_number - 1
    if 0 <= idx < len(references) and references[idx].chunk_id:
        return references[idx]
    return None


def build_save_chat_as_note_params(
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
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
    answer_segments = _build_passage_group(clean_answer, len(clean_answer))

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


async def save_chat_answer_as_note(
    rpc: SaveChatNoteRpc,
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
    title: str,
) -> Note:
    """Save a chat answer as a citation-rich note via the saved-from-chat
    CREATE_NOTE variant (issue #660).

    Unlike the plain note ``create_note()`` flow, this is a single
    CREATE_NOTE round-trip (no follow-up UPDATE_NOTE). The 7-element
    params payload carries the answer text, source-passage metadata, and
    per-citation anchors in one request. The server stores the note in
    its ``[2]`` mode so the NotebookLM UI renders ``[N]`` markers as
    hover-able passage links.

    Args:
        rpc: RPC dispatcher implementing :class:`SaveChatNoteRpc`.
        notebook_id: Target notebook UUID.
        answer_text: AI answer text including ``[N]`` citation markers.
        references: Citation list from ``AskResult.references``.
        title: User-requested title. The server may override with a
            smart-generated title; the returned ``Note.title`` reflects
            what the server stored.

    Returns:
        The created ``Note``. The ``content`` field holds the original
        answer text (with markers); the rich citation anchors live
        server-side and are exposed via the NotebookLM web UI rather than
        through this dataclass.

    Raises:
        ValueError: If ``references`` is empty (caller should use
            ``notes.create()`` for plain-text notes instead).
    """
    logger.debug(
        "Saving chat answer as note in notebook %s (%d refs)",
        notebook_id,
        len(references),
    )
    params = build_save_chat_as_note_params(notebook_id, answer_text, references, title)
    result = await rpc.rpc_call(
        RPCMethod.CREATE_NOTE,
        params,
        source_path=f"/notebook/{notebook_id}",
    )

    # The captured server response wraps the 6-element note in an outer
    # list (``[[note_id, ..., title, rich_content]]``), but some response
    # paths return the note flat (``[note_id, ...]``) — see existing
    # ``create_note`` which handles both. Unwrap defensively.
    note_data: list[Any] | None = None
    if isinstance(result, list) and len(result) > 0:
        if isinstance(result[0], list):
            note_data = result[0]
        elif isinstance(result[0], str):
            note_data = result

    note_id: str | None = None
    server_title = title
    if note_data is not None and len(note_data) > 0 and isinstance(note_data[0], str):
        note_id = note_data[0]
        # Slot [4] of the note carries the server-stored title, which
        # may differ from the requested title (smart-title generation).
        if len(note_data) > 4 and isinstance(note_data[4], str):
            server_title = note_data[4]

    if not note_id:
        raise RuntimeError("CREATE_NOTE returned no note ID for saved-from-chat request")

    return Note(
        id=note_id,
        notebook_id=notebook_id,
        title=server_title,
        content=answer_text,
    )
