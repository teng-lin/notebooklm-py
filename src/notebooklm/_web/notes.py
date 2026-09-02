"""Web note backend: row primitives and the concrete notes namespace.

This module owns the backend note-row operations shared by ``NotesAPI``
(plain notes + saved-from-chat notes) and ``ArtifactsAPI`` (mind maps,
which the server stores in the same note collection). It deliberately
sits *below* both feature facades so neither has to import the other,
and so the mind-map adapter (``_web.mind_maps.NoteBackedMindMapService``)
has a single seam to delegate through.

``NoteRowKind`` is a private classification of the raw row shapes
returned by the ``GET_NOTES_AND_MIND_MAPS`` RPC. It is intentionally
NOT part of the public ``notebooklm`` surface — the public ``Note``
dataclass and ``client.notes`` / ``client.artifacts`` facades remain
the only stable contract.

Risk-mitigation note (refactor-history.md §Risks): saved-chat note metadata is
not always reliably present on the wire. When the classifier cannot
positively identify a row as a saved-from-chat note it defaults to
``NOTE`` (not ``UNKNOWN``) so the NotesAPI list path keeps surfacing
the row — losing a chat-mode tag is preferable to dropping the note.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from .._lookup import unwrap_or_raise
from .._notes import NotesAPI
from ..exceptions import DecodingError, NoteNotFoundError, RPCError
from ..rpc import safe_index
from ..rpc.types import RPCMethod
from ..types import Note
from .note_tasks import NoteTaskRegistry
from .rows.notes import NoteRow

if TYPE_CHECKING:
    from .._runtime.call_supervisor import CallSupervisor
    from .contracts import RpcCaller
    from .mind_maps import NoteBackedMindMapService

__all__ = ["NoteService", "WebNotesAPI"]  # NoteRowKind is intentionally NOT exported

logger = logging.getLogger("notebooklm._note_service")
notes_logger = logging.getLogger("notebooklm._notes")


class NoteRowKind(Enum):
    """Private classification of rows from ``GET_NOTES_AND_MIND_MAPS``.

    Not part of the public API — kept private so the wire-shape
    classification can evolve without a SemVer hit. ``SAVED_CHAT`` is reserved
    for future reliable saved-from-chat detection; current saved-chat rows fall
    back to ``NOTE``.
    """

    NOTE = "note"
    SAVED_CHAT = "saved_chat"
    MIND_MAP = "mind_map"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class NoteService:
    """Backend note-row primitives — fetch + classify + CRUD.

    Owns the ``GET_NOTES_AND_MIND_MAPS`` / ``CREATE_NOTE`` /
    ``UPDATE_NOTE`` / ``DELETE_NOTE`` RPC family. Shared by
    ``NotesAPI`` and by ``NoteBackedMindMapService`` (the adapter
    that powers ``ArtifactsAPI`` mind-map paths).

    Takes the narrow :class:`RpcCaller` capability for wire dispatch and the
    root :class:`CallSupervisor` for the create/finalize workflow, child-task
    admission, and close-time settlement.
    """

    def __init__(self, rpc: RpcCaller, *, supervisor: CallSupervisor) -> None:
        self._rpc = rpc
        self._supervisor = supervisor
        self._task_registry = NoteTaskRegistry(supervisor)
        self._supervisor.register_drain_hook("notes.background", self._task_registry.drain)

    # ------------------------------------------------------------------
    # Row fetch + classification
    # ------------------------------------------------------------------

    async def fetch_note_rows(self, notebook_id: str) -> list[Any]:
        """Fetch all note + mind-map rows for a notebook.

        Returns the raw row list (each row is itself a list whose first
        element is the row ID). Soft-deleted rows are included — callers
        decide whether to filter via :meth:`classify_row`.
        """
        params = [notebook_id]
        result = await self._rpc.rpc_call(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        rows = self._extract_note_row_container(result)
        if not rows:
            return []

        normalized: list[Any] = []
        for item in rows:
            row = self._normalize_note_row(item)
            if row is not None:
                normalized.append(row)
        return normalized

    def _extract_note_row_container(self, result: Any) -> list[Any]:
        """Return the list that contains raw note rows.

        Historical responses wrap rows as ``[[row, ...]]``. Newer web
        responses use the same first response field for rows and a second
        timestamp field, so this helper also accepts a flat row list.
        """
        if not result:
            return []
        if not isinstance(result, list):
            # A truthy non-list payload is schema drift, not a legitimately empty
            # notebook — raise so notes/mind_maps get()/get_or_none can tell a
            # miss from drift instead of silently collapsing to ``[]``.
            raise DecodingError(
                "Unrecognized GET_NOTES_AND_MIND_MAPS payload shape",
                raw_response=repr(result),
                method_id=RPCMethod.GET_NOTES_AND_MIND_MAPS.value,
            )

        # ``result`` is a non-empty list here (guarded by the ``not result`` and
        # ``isinstance(result, list)`` checks above), so this ``[0]`` read cannot
        # fail; routing it through ``safe_index`` keeps the position knowledge on
        # the sanctioned schema-drift seam without changing behaviour.
        first = safe_index(
            result,
            0,
            method_id=RPCMethod.GET_NOTES_AND_MIND_MAPS.value,
            source="NoteService._extract_note_row_container",
        )
        if self._is_note_row_like(first):
            return result
        if isinstance(first, list):
            return first
        return []

    def _normalize_note_row(self, item: Any) -> list[Any] | None:
        """Normalize supported note wrapper shapes into parser rows.

        Current NotebookLM front-end code wraps live notes as
        ``[None, [note_id, content, metadata, ..., title]]``. The public
        parsers expect ``[note_id, nested_note]``, so normalize that wrapper
        before classification/parsing while preserving legacy rows and
        soft-deleted rows such as ``[note_id, None, 2]``.
        """
        if not self._is_note_row_like(item):
            return None

        # ``_is_note_row_like`` guarantees ``item`` is a non-empty list; the
        # ``None``-nested branch additionally guarantees ``len(item) > 1`` and a
        # non-empty ``item[1]`` nested list, so every read below is on a slot the
        # guard already proved present — ``safe_index`` routes the position
        # knowledge through the schema-drift seam without changing behaviour.
        method_id = RPCMethod.GET_NOTES_AND_MIND_MAPS.value
        head = safe_index(item, 0, method_id=method_id, source="NoteService._normalize_note_row")
        if isinstance(head, str):
            return item

        nested = safe_index(item, 1, method_id=method_id, source="NoteService._normalize_note_row")
        nested_head = safe_index(
            nested, 0, method_id=method_id, source="NoteService._normalize_note_row"
        )
        return [nested_head, nested, *item[2:]]

    def _is_note_row_like(self, item: Any) -> bool:
        if not isinstance(item, list) or len(item) == 0:
            return False
        # ``item`` is a non-empty list here, so ``[0]`` cannot fail; the ``[1]``
        # read below is gated by ``len(item) <= 1``. Both descents route through
        # ``safe_index`` (the sanctioned schema-drift seam) without changing the
        # historical shape-detection behaviour.
        method_id = RPCMethod.GET_NOTES_AND_MIND_MAPS.value
        head = safe_index(item, 0, method_id=method_id, source="NoteService._is_note_row_like")
        if isinstance(head, str):
            return True
        # ``[None, [id, ...], ...]`` shape: bind the ``[1]`` nested row so the
        # id-type check is a single-level ``nested[0]`` read on the nested list.
        # A non-list/empty nested row simply means "not a note row" (False).
        if head is not None or len(item) <= 1:
            return False
        nested = safe_index(item, 1, method_id=method_id, source="NoteService._is_note_row_like")
        nested_head = (
            safe_index(nested, 0, method_id=method_id, source="NoteService._is_note_row_like")
            if isinstance(nested, list) and len(nested) > 0
            else None
        )
        return isinstance(nested, list) and len(nested) > 0 and isinstance(nested_head, str)

    def classify_row(self, row: list[Any]) -> NoteRowKind:
        """Identify what kind of row this is.

        Wire shapes encountered:
        * deleted: ``["id", None, 2]`` — content is ``None`` and the
          slot at position 2 is the soft-delete sentinel.
        * mind-map: content payload parses as JSON with ``"children":``
          or ``"nodes":`` keys (regardless of legacy vs current shape).
        * saved-chat: reserved for future reliable saved-from-chat detection.
          Current saved-chat rows do not carry a stable discriminator, so they
          fall through to ``NOTE`` rather than ``UNKNOWN``.
        * plain note: default for any other content-bearing row.

        Position knowledge (the deletion sentinel and the
        legacy-vs-current content dispatch) lives in
        :class:`notebooklm._web.rows.notes.NoteRow`. This classifier reads
        named properties on the adapter and does not touch raw indices.
        """
        if not isinstance(row, list) or len(row) == 0:
            return NoteRowKind.UNKNOWN

        note_row = NoteRow(row)
        if note_row.is_deleted:
            return NoteRowKind.DELETED

        content = note_row.content
        if NoteRow.is_mind_map_content(content):
            return NoteRowKind.MIND_MAP

        if content is None:
            return NoteRowKind.UNKNOWN

        # Saved-chat detection may grow later; for now default to NOTE
        # so a chat-mode note never silently drops out of NotesAPI.list().
        return NoteRowKind.NOTE

    def extract_content(self, row: list[Any]) -> str | None:
        """Get the JSON content payload of a row, or ``None``.

        Thin facade over :attr:`NoteRow.content`. Kept on
        :class:`NoteService` so existing callers
        (``NotesAPI._extract_content``, ``NoteBackedMindMapService.extract_content``,
        and the tests pinning ``service.extract_content`` behaviour)
        continue to work unchanged while position knowledge moves to
        the adapter.
        """
        if not isinstance(row, list):
            return None
        return NoteRow(row).content

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_note(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
        *,
        operation_variant: str = "plain",
    ) -> Note:
        """Create and finalize one Web note under root workflow admission."""
        async with self._supervisor.operation_scope("notes.create"):
            return await self._create_note_admitted(
                notebook_id,
                title=title,
                content=content,
                operation_variant=operation_variant,
            )

    async def _create_note_admitted(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
        *,
        operation_variant: str = "plain",
    ) -> Note:
        """Create a note row and finalize its content + title.

        ``CREATE_NOTE`` ignores the title param server-side, so we follow
        up with ``UPDATE_NOTE`` to set both content and title. Returns a
        :class:`Note` dataclass for consistency with ``NotesAPI``.

        Cancellation behaviour: the UPDATE_NOTE finalize is wrapped in
        ``asyncio.shield`` so an outer cancel
        cannot abort an in-flight finalize. If ``CancelledError``
        propagates while the shielded UPDATE_NOTE is still running, a
        best-effort DELETE_NOTE cleanup is scheduled (NOT awaited —
        re-raise must not block on cleanup) to honour the caller's
        cancel intent without leaving an orphan row behind.
        """
        params = [notebook_id, "", [1], None, title]
        result = await self._rpc.rpc_call(
            RPCMethod.CREATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            operation_variant=operation_variant,
        )

        note_id: str | None = None
        # The CREATE_NOTE row carries the creation timestamp in the same
        # metadata sub-structure NoteRow decodes for list rows; capture the
        # bare inner envelope here so we can read it through the adapter
        # (rather than the field staying silently ``None`` — issue #1529).
        created_inner_row: list[Any] | None = None
        if result and isinstance(result, list) and len(result) > 0:
            # CREATE_NOTE returns either ``[[id, ...], ...]`` (id-envelope row) or
            # a bare ``[id, ...]``. Bind the first element so the id read is a
            # single-level index rather than a chained ``result[0][0]`` descent;
            # a degenerate shape leaves note_id None and raises below.
            # ``result`` is a non-empty list here (guarded above), so this ``[0]``
            # read cannot fail; ditto ``first[0]`` under its ``len(first) > 0``
            # guard. ``safe_index`` keeps the position knowledge on the sanctioned
            # schema-drift seam without changing behaviour.
            method_id = RPCMethod.CREATE_NOTE.value
            first = safe_index(result, 0, method_id=method_id, source="NoteService.create_note")
            if isinstance(first, list) and len(first) > 0:
                note_id = safe_index(
                    first, 0, method_id=method_id, source="NoteService.create_note"
                )
                created_inner_row = first
            elif isinstance(first, str):
                note_id = first
                # Flat shape: ``result`` IS the inner envelope
                # (``[id, content, metadata, None, title]``), so the
                # timestamp lives at ``result[2][2][0]`` — the same slot the
                # wrapped path reads at ``first[2][2][0]``. Capture ``result``
                # so the NoteRow([note_id, inner]) wrapping descent below
                # decodes it (issue #1529).
                created_inner_row = result

        if not note_id:
            # CREATE_NOTE returned a payload we cannot extract a note id
            # from. Returning ``Note(id="")`` would be a success-shaped
            # lie: the title/content were never finalized via UPDATE_NOTE,
            # and any later operation keyed on the empty id misbehaves.
            # Raise instead, matching the sibling create paths
            # (``_web.sources.add`` / ``notebooks.create``) which surface an
            # error rather than fabricate a degenerate resource.
            raise RPCError(
                "CREATE_NOTE returned no usable note id; the note was not created",
                method_id=RPCMethod.CREATE_NOTE.value,
            )

        # Shield the UPDATE_NOTE finalize from outer cancellation:
        # CREATE_NOTE has already persisted a row server-side; without
        # the shield, a cancel arriving between CREATE_NOTE and
        # UPDATE_NOTE completion leaves an orphan row with no
        # title/content.
        #
        # ``update_task`` is a freestanding ``asyncio.Task`` (not a
        # bare coroutine) so the cancel-time cleanup branch can await
        # it before issuing the best-effort DELETE_NOTE. If we instead
        # fired DELETE_NOTE in parallel with the still-running
        # shielded UPDATE_NOTE, delete could complete first and update could then
        # write to an already-soft-deleted row — observable as an
        # inconsistent row state on the server side and a swallowed
        # exception in the cleanup task.
        update_task: asyncio.Task[None] | None = None
        try:
            # Keep publication inside the cancellation handler. The child can
            # enter UPDATE_NOTE before ``spawn`` returns it to this parent; a
            # cancel in that window makes the supervisor cancel and settle the
            # unpublished child before raising. CREATE_NOTE has still persisted
            # its row, so that path needs the same orphan cleanup as a cancel at
            # the shield below.
            update_task = await self._task_registry.spawn(
                f"note-update-{notebook_id}-{note_id}",
                lambda: self.update_note(notebook_id, note_id, content, title),
            )
            await asyncio.shield(update_task)
        except asyncio.CancelledError:
            # Ordered fire-and-forget cleanup: first wait for the
            # published, shielded UPDATE_NOTE to finish (success OR error),
            # THEN issue the best-effort DELETE_NOTE. If cancellation landed
            # during child publication, ``spawn_child`` has already cancelled
            # and settled that unpublished UPDATE_NOTE, so cleanup can delete
            # directly. The re-raise MUST NOT await the wrapper task. The
            # per-service registry strongly retains it and the root drain hook
            # settles it before Web transport teardown.
            async def _finalize_then_cleanup() -> None:
                try:
                    if update_task is not None:
                        try:
                            await update_task
                        except Exception:  # noqa: BLE001 — log and proceed to delete
                            logger.debug(
                                "Shielded UPDATE_NOTE failed before cleanup for note %s in notebook %s",
                                note_id,
                                notebook_id,
                                exc_info=True,
                            )
                finally:
                    await self._delete_note_best_effort(notebook_id, note_id)

            try:
                await self._task_registry.spawn(
                    f"note-cleanup-{notebook_id}-{note_id}",
                    _finalize_then_cleanup,
                )
            except BaseException:
                # The caller's cancellation owns precedence. A concurrent root
                # close may already have fenced child admission; in that case
                # the registry/supervisor still settle the admitted update.
                logger.debug(
                    "Could not admit DELETE_NOTE cleanup for note %s in notebook %s",
                    note_id,
                    notebook_id,
                    exc_info=True,
                )
            raise

        # Wrap the bare CREATE_NOTE inner envelope into the current row
        # shape (``[id, [id, content, metadata, None, title]]``) so the
        # NoteRow adapter's centralised ``row[1][2][2][0]`` descent reads
        # the creation timestamp; absent / legacy shapes degrade to None.
        created_at = (
            NoteRow([note_id, created_inner_row]).created_at
            if created_inner_row is not None
            else None
        )
        return Note(
            id=note_id,
            notebook_id=notebook_id,
            title=title,
            content=content,
            created_at=created_at,
        )

    async def _delete_note_best_effort(self, notebook_id: str, note_id: str) -> None:
        """Best-effort DELETE_NOTE cleanup for a partially-finalized create.

        Used as a supervised background-child target when an
        outer cancel arrives mid-UPDATE_NOTE: we never block the
        re-raise on this call, and any failure (network, auth refresh,
        etc.) is logged and swallowed. The only desired side effect is
        orphan-row removal.
        """
        try:
            await self.delete_note(notebook_id, note_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup, must not surface
            logger.warning(
                "Best-effort DELETE_NOTE cleanup failed for note %s in notebook %s",
                note_id,
                notebook_id,
                exc_info=True,
            )

    async def update_note(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Update a note row's content and title in place."""
        params = [
            notebook_id,
            note_id,
            [[[content, title, [], 0]]],
        ]
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )

    async def delete_note(self, notebook_id: str, note_id: str) -> None:
        """Soft-delete a note row.

        Returns ``None``. Idempotent: a missing note still succeeds
        (``DELETE_NOTE`` is ``allow_null=True`` with no missing-signal). The
        public facade (``client.notes.delete`` /
        ``NoteBackedMindMapService.delete_mind_map``) returns ``None`` as of
        v0.7.0 (issue #1211).
        """
        params = [notebook_id, None, [note_id]]
        await self._rpc.rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )


class WebNotesAPI(NotesAPI):
    """Operations on NotebookLM notes.

    Notes are user-created content, distinct from AI-generated artifacts.
    Notes support operations like export to Docs/Sheets and conversion to sources.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Create and update notes
            note = await client.notes.create(notebook_id, "My Note", "Content here")
            await client.notes.update(notebook_id, note.id, "Updated content", "New Title")

            # List and delete
            notes = await client.notes.list(notebook_id)
            await client.notes.delete(notebook_id, note.id)
    """

    def __init__(
        self,
        *,
        notes: NoteService,
        mind_maps: NoteBackedMindMapService,
    ):
        """Initialize the notes API.

        Args:
            notes: Backend note-row primitives. Owns
                ``fetch_note_rows`` / ``classify_row`` / ``create_note``
                / ``update_note`` / ``delete_note``.
            mind_maps: Mind-map-only facade backed by ``notes``. Owns
                the ``list_mind_maps`` / ``delete_mind_map`` paths the
                public ``NotesAPI`` surface forwards through.
        """
        self._notes = notes
        self._mind_maps = mind_maps

    async def list(self, notebook_id: str) -> list[Note]:
        """List all text notes in the notebook.

        This excludes:
        - Mind maps (stored in same structure but contain JSON with 'children'/'nodes')
        - Deleted notes (status=2, content cleared but ID persists)

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of Note objects.
        """
        notes_logger.debug("Listing notes in notebook: %s", notebook_id)
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        notes: list[Note] = []
        for item in all_items:
            kind = self._notes.classify_row(item)
            if kind in (NoteRowKind.DELETED, NoteRowKind.MIND_MAP):
                continue
            notes.append(self._parse_note(item, notebook_id))
        return notes

    async def get(self, notebook_id: str, note_id: str) -> Note:
        """Get a specific note by ID.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            The :class:`~notebooklm.types.Note`.

        Raises:
            NoteNotFoundError: If no note with ``note_id`` exists (matches
                ``notebooks.get``; issue #1247). Use :meth:`get_or_none` for the
                sanctioned ``None``-on-miss lookup.
        """
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, note_id),
            NoteNotFoundError(note_id),
        )

    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a note by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which now raises ``NoteNotFoundError`` on a miss (#1247) — this
        returns ``None`` for a genuine absence and emits no
        deprecation warning. Transport, auth, and decode faults raised by the
        underlying note listing are **not** swallowed; only a real "not found"
        yields ``None``.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            The :class:`~notebooklm.types.Note`, or ``None`` if not found.
        """
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        for item in all_items:
            if isinstance(item, list) and len(item) > 0 and NoteRow(item).id == note_id:
                return self._parse_note(item, notebook_id)
        return None

    _get_or_none = get_or_none

    async def create(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Note:
        """Create a new note in the notebook.

        Args:
            notebook_id: The notebook ID.
            title: The note title.
            content: The note content.

        Returns:
            The created Note object.
        """
        return await self._notes.create_note(
            notebook_id,
            title=title,
            content=content,
        )

    async def update(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Update a note's content and title.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.
            content: The new content.
            title: The new title.

        Raises:
            NoteNotFoundError: When ``note_id`` does not exist. The
                ``UPDATE_NOTE`` RPC is ``allow_null=True`` and silently no-ops
                on a missing note, so a public-facade existence preflight runs
                first to make a mutate-existing op fail loud per ADR-0019
                Class 5.

        .. versionchanged:: 0.8.0
            **Breaking change:** updating a missing note now raises
            :class:`NoteNotFoundError` instead of silently "succeeding" via the
            ``allow_null=True`` no-op (#1362).
        """
        if await self.get_or_none(notebook_id, note_id) is None:
            raise NoteNotFoundError(note_id)
        await self._notes.update_note(notebook_id, note_id, content, title)

    async def delete(self, notebook_id: str, note_id: str) -> None:
        """Delete a note from the notebook.

        Note: This clears the note content/title rather than removing it
        from the list entirely. Google may garbage collect cleared notes later.

        Idempotent: deleting an already-absent note succeeds (returns
        ``None``) and never raises. Real failures (``403``/``5xx``/auth/
        transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). ``if await notes.delete(...):``
            no longer enters its block.
        """
        notes_logger.debug("Deleting note %s from notebook %s", note_id, notebook_id)
        await self._notes.delete_note(notebook_id, note_id)

    async def list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """List all mind maps in the notebook.

        Mind maps are stored in the same internal structure as notes but
        contain JSON data with 'children' or 'nodes' keys.

        Note: For most use cases, prefer `client.artifacts.list()` which returns
        mind maps as Artifact objects alongside other AI-generated content.

        This excludes deleted mind maps (status=2).

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of raw mind map data.
        """
        return await self._mind_maps.list_mind_maps(notebook_id)

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Delete a mind map from the notebook.

        Idempotent: deleting an already-absent mind map succeeds (returns
        ``None``) and never raises. Real failures (``403``/``5xx``/auth/
        transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            mind_map_id: The mind map ID.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211).
        """
        await self._mind_maps.delete_mind_map(notebook_id, mind_map_id)

    async def _get_all_notes_and_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Fetch all notes and mind maps from the API."""
        return await self._notes.fetch_note_rows(notebook_id)

    def _is_deleted(self, item: builtins.list[Any]) -> bool:
        """Check if a note/mind map item is deleted (status=2).

        Delegates to :meth:`NoteService.classify_row`, which reads the
        deletion sentinel via :attr:`NoteRow.is_deleted`. The wire
        shape (``[id, None, 2]`` — content slot ``None`` plus the
        soft-delete sentinel at position 2) is documented on
        :class:`NoteRow`; this method exists only as the historical
        ``NotesAPI`` private surface.

        Args:
            item: Raw note/mind map data.

        Returns:
            True if the item is deleted (soft-deleted with status=2).
        """
        return self._notes.classify_row(item) == NoteRowKind.DELETED

    def _extract_content(self, item: builtins.list[Any]) -> str | None:
        """Extract content string from note/mind map item."""
        return self._notes.extract_content(item)

    def _parse_note(self, item: builtins.list[Any], notebook_id: str) -> Note:
        """Parse a raw note item into a Note object.

        Position knowledge (legacy ``[id, content]`` vs current
        ``[id, [id, content, metadata, None, title]]`` dispatch, and
        the title slot at ``raw[1][4]``) lives in
        :class:`notebooklm._web.rows.notes.NoteRow` — this method just
        reads the named properties. ``content`` defaults to ``""``
        (not ``None``) here to preserve the v0.4.1 :class:`Note`
        contract.
        """
        row = NoteRow(item)
        return Note(
            id=row.id,
            notebook_id=notebook_id,
            title=row.title,
            content=row.content or "",
            created_at=row.created_at,
        )
