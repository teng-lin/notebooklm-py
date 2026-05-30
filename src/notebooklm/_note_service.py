"""Private note-row primitives — classifier + CRUD.

This module owns the backend note-row operations shared by ``NotesAPI``
(plain notes + saved-from-chat notes) and ``ArtifactsAPI`` (mind maps,
which the server stores in the same note collection). It deliberately
sits *below* both feature facades so neither has to import the other,
and so the mind-map adapter (``_mind_map.NoteBackedMindMapService``)
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
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from ._row_adapters_notes import NoteRow
from .exceptions import RPCError
from .rpc.types import RPCMethod
from .types import Note

if TYPE_CHECKING:
    from ._runtime_contracts import RpcCaller

__all__ = ["NoteService"]  # NoteRowKind is intentionally NOT exported

logger = logging.getLogger(__name__)


# Module-level strong-ref anchor for fire-and-forget cleanup tasks (RUF006).
# ``asyncio.create_task`` returns a Task that the event loop only holds via a
# weak reference, so an unrooted Task can be garbage-collected mid-execution —
# losing the orphan-row cleanup the cancel-safety shield is supposed to
# guarantee. Each created task adds itself here and removes itself in a
# done-callback so the set stays bounded.
#
# Intentionally module-level (not per-instance): the cleanup tasks are
# detached fire-and-forget work whose only purpose is to keep the loop's
# Task storage from GC-ing them mid-flight. Sharing one set across all
# ``NoteService`` instances is correct and simpler than per-instance
# bookkeeping — there is no per-instance state on the tasks themselves.
# Audit CC6: single-loop-per-client invariant per ADR-004; not safe for multi-loop fan-out.
_cleanup_tasks: set[asyncio.Task[Any]] = set()


class NoteRowKind(Enum):
    """Private classification of rows from ``GET_NOTES_AND_MIND_MAPS``.

    Not part of the public API — kept private so the wire-shape
    classification can evolve without a SemVer hit. Phase 6 may add
    further variants (e.g. distinct treatment for saved-from-chat
    notes) without breaking external callers.
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

    Takes the narrow :class:`RpcCaller` capability — note CRUD only
    needs ``rpc_call(...)``; everything else (drain hooks, transport,
    loop-affinity guards) is irrelevant to this service.
    """

    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc

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
        if not result or not isinstance(result, list):
            return []

        first = result[0]
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

        if isinstance(item[0], str):
            return item

        nested = item[1]
        return [nested[0], nested, *item[2:]]

    def _is_note_row_like(self, item: Any) -> bool:
        if not isinstance(item, list) or len(item) == 0:
            return False
        if isinstance(item[0], str):
            return True
        return (
            item[0] is None
            and len(item) > 1
            and isinstance(item[1], list)
            and len(item[1]) > 0
            and isinstance(item[1][0], str)
        )

    def classify_row(self, row: list[Any]) -> NoteRowKind:
        """Identify what kind of row this is.

        Wire shapes encountered:
        * deleted: ``["id", None, 2]`` — content is ``None`` and the
          slot at position 2 is the soft-delete sentinel.
        * mind-map: content payload parses as JSON with ``"children":``
          or ``"nodes":`` keys (regardless of legacy vs current shape).
        * saved-chat: a plain note row whose metadata flags chat mode.
          That metadata is not reliably present on the wire, so when we
          cannot positively confirm chat mode we fall through to
          ``NOTE`` rather than ``UNKNOWN`` (refactor-history.md §Risks).
        * plain note: default for any other content-bearing row.

        Position knowledge (the deletion sentinel and the
        legacy-vs-current content dispatch) lives in
        :class:`notebooklm._row_adapters_notes.NoteRow`. This classifier reads
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

        # Phase 6 may grow saved-chat detection; for now default to NOTE
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
        """Create a note row and finalize its content + title.

        ``CREATE_NOTE`` ignores the title param server-side, so we follow
        up with ``UPDATE_NOTE`` to set both content and title. Returns a
        :class:`Note` dataclass for consistency with ``NotesAPI``.

        Cancellation behaviour (audit item §28): the UPDATE_NOTE
        finalize is wrapped in ``asyncio.shield`` so an outer cancel
        cannot abort an in-flight finalize. If ``CancelledError``
        propagates while the shielded UPDATE_NOTE is still running, a
        best-effort DELETE_NOTE cleanup is scheduled (NOT awaited —
        re-raise must not block on cleanup) to honour the caller's
        cancel intent without leaving an orphan row behind. The legacy
        ``_mind_map.MindMapService.create_note`` path that previously
        owned this contract was retired; the contract itself now lives here.
        """
        params = [notebook_id, "", [1], None, title]
        result = await self._rpc.rpc_call(
            RPCMethod.CREATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            operation_variant=operation_variant,
        )

        note_id: str | None = None
        if result and isinstance(result, list) and len(result) > 0:
            if isinstance(result[0], list) and len(result[0]) > 0:
                note_id = result[0][0]
            elif isinstance(result[0], str):
                note_id = result[0]

        if not note_id:
            # CREATE_NOTE returned a payload we cannot extract a note id
            # from. Returning ``Note(id="")`` would be a success-shaped
            # lie: the title/content were never finalized via UPDATE_NOTE,
            # and any later operation keyed on the empty id misbehaves.
            # Raise instead, matching the sibling create paths
            # (``_source_add`` / ``notebooks.create``) which surface an
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
        update_task = asyncio.create_task(self.update_note(notebook_id, note_id, content, title))
        try:
            await asyncio.shield(update_task)
        except asyncio.CancelledError:
            # Ordered fire-and-forget cleanup: first wait for the
            # shielded UPDATE_NOTE to finish (success OR error),
            # THEN issue the best-effort DELETE_NOTE. The re-raise
            # MUST NOT await the wrapper task. Strong-ref via
            # ``_cleanup_tasks`` so the loop's weak-ref Task storage
            # cannot GC the wrapper mid-flight (RUF006); the
            # done-callback discards on completion so the set stays
            # bounded.
            async def _finalize_then_cleanup() -> None:
                try:
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

            cleanup_task = asyncio.create_task(_finalize_then_cleanup())
            _cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(_cleanup_tasks.discard)
            raise

        return Note(
            id=note_id,
            notebook_id=notebook_id,
            title=title,
            content=content,
        )

    async def _delete_note_best_effort(self, notebook_id: str, note_id: str) -> None:
        """Best-effort DELETE_NOTE cleanup for a partially-finalized create.

        Used as a fire-and-forget ``asyncio.create_task`` target when an
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
        )

    async def delete_note(self, notebook_id: str, note_id: str) -> bool:
        """Soft-delete a note row.

        Returns ``True`` on success, mirroring the v0.4.1 NotesAPI
        contract (``client.notes.delete(...) -> bool``). The bool flow
        is preserved so the public facade can keep returning ``True``
        unconditionally via :class:`NoteBackedMindMapService.delete_mind_map`
        without callers seeing a behavioral change.
        """
        params = [notebook_id, None, [note_id]]
        await self._rpc.rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True
