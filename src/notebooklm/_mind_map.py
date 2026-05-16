"""Mind-map RPC primitives shared by ``ArtifactsAPI`` and ``NotesAPI``.

Mind maps live in the "notes" backend (same ``GET_NOTES_AND_MIND_MAPS`` /
``CREATE_NOTE`` / ``UPDATE_NOTE`` / ``DELETE_NOTE`` RPCs as user notes) but
they are AI-generated artifacts from the caller's perspective. This module
hosts the low-level RPC primitives so neither :class:`NotesAPI` nor
:class:`ArtifactsAPI` needs to import the other.

Functions here take a :class:`ClientCore` as their first argument and return
raw RPC-shaped data (lists). Higher-level dataclass parsing stays in
:mod:`_notes` / :mod:`_artifacts`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from ._core import ClientCore
from .rpc import RPCMethod
from .types import Note

if TYPE_CHECKING:
    from .types import ChatReference

logger = logging.getLogger(__name__)

# Strong references for fire-and-forget cleanup tasks. ``asyncio.create_task``
# returns a Task that the event loop only holds via a weak reference, so an
# unrooted Task can be garbage-collected mid-execution — losing the orphan-row
# cleanup the cancel-safety shield is supposed to guarantee. Each created task
# adds itself here and removes itself in a done-callback so the set stays
# bounded.
_cleanup_tasks: set[asyncio.Task[Any]] = set()


async def _delete_note_best_effort(core: ClientCore, notebook_id: str, note_id: str) -> None:
    """Best-effort DELETE_NOTE cleanup for a partially-finalized create.

    Used as a fire-and-forget ``asyncio.create_task`` target when an
    outer cancel arrives mid-UPDATE_NOTE: we never block the re-raise on
    this call, and any failure (network, auth refresh, etc.) is logged
    and swallowed — the only side-effect we want is the orphan-row
    removal, not a secondary exception.
    """
    try:
        params = [notebook_id, None, [note_id]]
        await core.rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup, must not surface
        logger.warning(
            "Best-effort DELETE_NOTE cleanup failed for note %s in notebook %s",
            note_id,
            notebook_id,
            exc_info=True,
        )


async def fetch_all_notes_and_mind_maps(core: ClientCore, notebook_id: str) -> list[Any]:
    """Fetch all notes + mind maps in a notebook.

    Both notes and mind maps share the same backend collection; callers
    filter by content shape (``"children":`` / ``"nodes":`` for mind maps).

    Args:
        core: The shared :class:`ClientCore`.
        notebook_id: The notebook ID to query.

    Returns:
        Raw list of items; each item is a list whose first element is the
        item ID. Empty list on missing/unexpected payloads.
    """
    params = [notebook_id]
    result = await core.rpc_call(
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        params,
        source_path=f"/notebook/{notebook_id}",
        allow_null=True,
    )
    if result and isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
        notes_list = result[0]
        valid_notes = []
        for item in notes_list:
            if isinstance(item, list) and len(item) > 0 and isinstance(item[0], str):
                valid_notes.append(item)
        return valid_notes
    return []


def is_deleted(item: list[Any]) -> bool:
    """Return True if a note/mind-map item is soft-deleted (``status=2``).

    Deleted items have structure ``['id', None, 2]`` — content is None and
    the third position holds the status sentinel.
    """
    if not isinstance(item, list) or len(item) < 3:
        return False
    return item[1] is None and item[2] == 2


def extract_content(item: list[Any]) -> str | None:
    """Extract the content string from a note/mind-map item.

    Handles both the legacy ``[id, content]`` and the current
    ``[id, [id, content, metadata, None, title]]`` shapes.
    """
    if len(item) <= 1:
        return None

    if isinstance(item[1], str):
        return item[1]
    if isinstance(item[1], list) and len(item[1]) > 1 and isinstance(item[1][1], str):
        return item[1][1]
    return None


async def list_mind_maps(core: ClientCore, notebook_id: str) -> list[Any]:
    """List raw mind-map items in a notebook (excluding soft-deleted ones).

    Mind maps are stored in the same collection as notes but contain JSON
    data with ``"children"`` or ``"nodes"`` keys. This function filters that
    collection down to mind-map-shaped entries.

    Args:
        core: The shared :class:`ClientCore`.
        notebook_id: The notebook ID to query.

    Returns:
        List of raw mind-map items (each a list, first element is the ID).
    """
    all_items = await fetch_all_notes_and_mind_maps(core, notebook_id)
    mind_maps: list[Any] = []
    for item in all_items:
        if is_deleted(item):
            continue
        content = extract_content(item)
        if content and ('"children":' in content or '"nodes":' in content):
            mind_maps.append(item)
    return mind_maps


async def update_note(
    core: ClientCore,
    notebook_id: str,
    note_id: str,
    content: str,
    title: str,
) -> None:
    """Update a note/mind-map row's content and title in place."""
    logger.debug("Updating note %s in notebook %s", note_id, notebook_id)
    params = [
        notebook_id,
        note_id,
        [[[content, title, [], 0]]],
    ]
    await core.rpc_call(
        RPCMethod.UPDATE_NOTE,
        params,
        source_path=f"/notebook/{notebook_id}",
        allow_null=True,
    )


async def create_note(
    core: ClientCore,
    notebook_id: str,
    title: str = "New Note",
    content: str = "",
) -> Note:
    """Create a note (or mind-map row) and set its content + title.

    Google's ``CREATE_NOTE`` ignores the title param, so this function
    always follows up with an ``UPDATE_NOTE`` to set both content and
    title. Returns a :class:`Note` dataclass for consistency with the
    higher-level :class:`NotesAPI`.

    Args:
        core: The shared :class:`ClientCore`.
        notebook_id: The notebook ID to create the note in.
        title: The desired title.
        content: The desired body content (mind-map JSON, plain text, ...).

    Returns:
        The created :class:`Note` with the assigned ID.
    """
    logger.debug("Creating note in notebook %s: %s", notebook_id, title)
    # The server currently ignores this slot (the follow-up UPDATE_NOTE is
    # what actually persists the title) but pass the caller-supplied title
    # rather than a hardcoded literal so the wire payload reflects the user
    # intent if Google ever starts honoring it.
    params = [notebook_id, "", [1], None, title]
    result = await core.rpc_call(
        RPCMethod.CREATE_NOTE,
        params,
        source_path=f"/notebook/{notebook_id}",
    )

    note_id: str | None = None
    if result and isinstance(result, list) and len(result) > 0:
        if isinstance(result[0], list) and len(result[0]) > 0:
            note_id = result[0][0]
        elif isinstance(result[0], str):
            note_id = result[0]

    if note_id:
        # CREATE_NOTE ignores the title param server-side, so set it via
        # UPDATE_NOTE alongside the actual content payload.
        #
        # Shield the UPDATE_NOTE finalize from outer cancellation.
        # CREATE_NOTE has already persisted a row server-side; without the
        # shield, a cancel arriving between CREATE_NOTE and UPDATE_NOTE
        # completion leaves an orphan row with no title/content.
        #
        # When CancelledError lands here, the shielded UPDATE_NOTE Task is
        # still running on the loop. Fire a best-effort DELETE_NOTE that
        # covers both sub-cases:
        #   (a) UPDATE_NOTE hasn't applied yet → orphan-row cleanup.
        #   (b) UPDATE_NOTE completes between the cancel and DELETE_NOTE →
        #       the now-applied note is deleted; caller's cancel intent
        #       (note should not exist) is honoured.
        # Strong-ref the cleanup task in ``_cleanup_tasks`` so the loop's
        # weak-ref Task storage cannot GC it mid-flight (RUF006); the
        # done-callback discards on completion so the set stays bounded.
        # The re-raise never awaits the cleanup task.
        try:
            await asyncio.shield(update_note(core, notebook_id, note_id, content, title))
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(_delete_note_best_effort(core, notebook_id, note_id))
            _cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(_cleanup_tasks.discard)
            raise

    return Note(
        id=note_id or "",
        notebook_id=notebook_id,
        title=title,
        content=content,
    )


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
    source_passages = [_build_source_passage_descriptor(chunk_to_ref[c]) for c in seen_chunks]

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
    source_passages_keyed = [
        [[c], _build_source_passage_descriptor(chunk_to_ref[c])] for c in seen_chunks
    ]

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
    core: ClientCore,
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
    title: str,
) -> Note:
    """Save a chat answer as a citation-rich note via the saved-from-chat
    CREATE_NOTE variant (issue #660).

    Unlike ``create_note()``, this is a single CREATE_NOTE round-trip
    (no follow-up UPDATE_NOTE). The 7-element params payload carries the
    answer text, source-passage metadata, and per-citation anchors in one
    request. The server stores the note in its ``[2]`` mode so the
    NotebookLM UI renders ``[N]`` markers as hover-able passage links.

    Args:
        core: The shared ``ClientCore``.
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
    result = await core.rpc_call(
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
