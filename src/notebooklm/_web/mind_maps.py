"""Concrete web mind-map backend and note-backed adapter service.

Hides the two backends (note-backed JSON vs interactive studio-artifact) behind a
single surface that dispatches each operation to the correct RPC family
(issue #1256). Note-backed generation uses ``GENERATE_MIND_MAP`` and then
persists with note RPCs (``CREATE_NOTE`` / ``UPDATE_NOTE``); note-backed
rename/delete use ``UPDATE_NOTE`` / ``DELETE_NOTE``. Interactive maps use the
studio-artifact RPCs (``CREATE_ARTIFACT`` type-4/variant-4 /
``RENAME_ARTIFACT`` / ``DELETE_ARTIFACT`` / ``GET_INTERACTIVE_HTML``).
"""

from __future__ import annotations

import builtins
import json
from typing import TYPE_CHECKING, Any

from .._idempotency import call_unconfirmed_on_transport_loss
from .._mind_maps_api import MindMapsAPI
from .._types.mind_maps import MindMap, MindMapKind
from ..exceptions import (
    ArtifactFeatureUnavailableError,
    MindMapNotFoundError,
)
from ..rpc import RPCMethod, safe_index
from ..types import Artifact, ArtifactType
from .notes import NoteRowKind, NoteService
from .rows.artifacts import extract_interactive_tree_leaf
from .rows.notes import NoteRow

if TYPE_CHECKING:
    from .._artifacts import ArtifactsAPI
    from .._notebooks import NotebooksAPI
    from .._notes import NotesAPI
    from .contracts import RpcCaller


__all__ = ["NoteBackedMindMapService", "WebMindMapsAPI"]


# ``CREATE_ARTIFACT`` returns the new artifact id wrapped as ``[[id, …]]``: the
# inner row sits at ``[0]`` of the envelope and the id is that row's ``[0]``
# leaf. Both descents are guarded for presence before ``safe_index`` reads them
# (see ``_new_artifact_id``).
_CREATE_ARTIFACT_ENVELOPE_POS = 0
_CREATE_ARTIFACT_ID_POS = 0


def _parse_tree(content: Any) -> dict[str, Any] | None:
    """Parse a mind-map JSON node tree, or ``None`` when not a JSON object."""
    if not isinstance(content, str) or not content:
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _new_artifact_id(create_response: Any) -> str | None:
    """Pull the new artifact id out of a ``CREATE_ARTIFACT`` response (``[[id, …]]``).

    Returns ``None`` for a null/degenerate response (no generation task created);
    the caller turns that into ``ArtifactFeatureUnavailableError``. The two
    envelope descents both go through ``safe_index`` *behind* a length guard that
    proves the slot present, so the strict helper is a no-op on every reachable
    input (it can only raise when the guarded slot is genuinely absent) while
    keeping the soft "degenerate response → ``None``" contract: an empty / non-list
    response, an empty / non-list ``inner`` row, or a non-``str`` id all return
    ``None`` rather than raising. This centralises the ``[0]`` / ``[0][0]``
    position knowledge on the shared ``safe_index`` seam instead of open-coding
    ``create_response[0]`` / ``inner[0]`` reads (issue #1491).
    """
    if not isinstance(create_response, list) or not create_response:
        return None
    # ``create_response`` is a non-empty list here, so this descent never raises;
    # it routes the read through the shared drift seam for telemetry parity.
    inner = safe_index(
        create_response,
        _CREATE_ARTIFACT_ENVELOPE_POS,
        method_id=RPCMethod.CREATE_ARTIFACT.value,
        source="_mind_maps_api._new_artifact_id",
    )
    if not isinstance(inner, list) or not inner:
        return None
    # ``inner`` is a non-empty list here, so this descent never raises either.
    head = safe_index(
        inner,
        _CREATE_ARTIFACT_ID_POS,
        method_id=RPCMethod.CREATE_ARTIFACT.value,
        source="_mind_maps_api._new_artifact_id",
    )
    return head if isinstance(head, str) else None


class NoteBackedMindMapService:
    """Mind-map-only facade over :class:`NoteService`.

    Adapter that knows mind maps share storage with notes. Consumers
    (``ArtifactsAPI`` download path, ``NotesAPI`` mind-map surface)
    talk to this class instead of reaching into ``NoteService``
    directly, so the "mind maps are notes under the hood" detail
    stays localized.

    The download path doesn't need ``create_mind_map`` — mind-map
    creation goes through :meth:`NoteService.create_note` directly
    from ``ArtifactsAPI.generate_mind_map`` (a one-shot
    GENERATE_MIND_MAP + persist pipeline). The methods exposed here
    are exactly the ones the artifact download path and ``NotesAPI``
    ``list_mind_maps`` / ``delete_mind_map`` need.
    """

    def __init__(self, notes: NoteService) -> None:
        self._notes = notes

    async def list_mind_maps(self, notebook_id: str) -> list[Any]:
        """Return mind-map rows for a notebook (deleted rows excluded)."""
        rows = await self._notes.fetch_note_rows(notebook_id)
        return [r for r in rows if self._notes.classify_row(r) == NoteRowKind.MIND_MAP]

    def extract_content(self, row: list[Any]) -> str | None:
        """Return the JSON content payload of a mind-map row.

        Delegates to :meth:`NoteService.extract_content` so the download
        path doesn't have to know mind maps share storage with notes.
        """
        return self._notes.extract_content(row)

    async def delete_mind_map(self, notebook_id: str, note_id: str) -> None:
        """Soft-delete a mind-map row.

        Delegates to :meth:`NoteService.delete_note`. Returns ``None`` as of
        v0.7.0 (``NotesAPI.delete_mind_map(...) -> None``, issue #1211).
        """
        await self._notes.delete_note(notebook_id, note_id)

    async def rename_mind_map(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Rename a note-backed mind map by retitling its backing note.

        Note-backed mind maps are renamed via ``UPDATE_NOTE`` (re-sending the
        existing content with the new title) — notes have no title-only field
        mask. (Interactive studio-artifact mind maps rename via
        ``RENAME_ARTIFACT`` instead; see ``MindMapsAPI``.)

        Raises:
            MindMapNotFoundError: if no note-backed mind map with
                ``mind_map_id`` exists.
        """
        for row in await self.list_mind_maps(notebook_id):
            if NoteRow(row).id == mind_map_id:
                content = self.extract_content(row) or ""
                await self._notes.update_note(notebook_id, mind_map_id, content, new_title)
                return
        raise MindMapNotFoundError(mind_map_id)


class WebMindMapsAPI(MindMapsAPI):
    """``client.mind_maps`` — one surface over both mind-map backends."""

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        mind_maps: NoteBackedMindMapService,
        artifacts: ArtifactsAPI,
        notebooks: NotebooksAPI,
        notes: NotesAPI,
    ) -> None:
        super().__init__(artifacts=artifacts, notes=notes)
        self._rpc = rpc
        self._mind_maps = mind_maps
        self._notebooks = notebooks

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """List only the **note-backed** mind maps in a notebook.

        A single ``GET_NOTES_AND_MIND_MAPS`` RPC — no ``LIST_ARTIFACTS`` — so
        callers that only need the note-backed membership (e.g. the artifact
        ``delete`` carve-out probe) pay exactly one round-trip. Returns
        note-backed entries only (every ``kind`` is
        :attr:`MindMapKind.NOTE_BACKED`); interactive (studio-artifact) maps
        never appear here — use :meth:`list` for the union. Deleted rows
        (status ``2``) are already excluded by the underlying
        ``list_mind_maps`` classification, and ``MindMap.tree`` is populated
        for free from the already-listed note content.
        """
        result: builtins.list[MindMap] = []
        for row in await self._mind_maps.list_mind_maps(notebook_id):
            note_row = NoteRow(row)
            result.append(
                MindMap(
                    id=note_row.id,
                    notebook_id=notebook_id,
                    title=note_row.title,
                    kind=MindMapKind.NOTE_BACKED,
                    created_at=note_row.created_at,
                    tree=_parse_tree(self._mind_maps.extract_content(row)),
                )
            )
        return result

    async def _send_rename_note_backed(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Rename a note-backed mind map through the web note-row service."""
        await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)

    async def _list_studio_mind_map_rows(self, notebook_id: str) -> builtins.list[Artifact]:
        """Return unfiltered Studio rows for interactive-map classification."""
        return await self._artifacts.list(notebook_id)

    async def _start_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> str:
        """Start the web ``CREATE_ARTIFACT`` interactive-map operation."""
        del language  # Interactive web payloads have no language slot.
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)
        # Imported lazily to keep the web mind-map facade import-safe while
        # ``_artifact`` re-exports its injected note-backed service identity.
        from .params.artifacts import build_interactive_mind_map_artifact_params

        # CREATE_ARTIFACT is classified in ``_web.policy``. ``operation_variant=None``
        # is passed explicitly to match the other CREATE_ARTIFACT / GENERATE_MIND_MAP
        # call sites (the registry resolves the same entry either way; the explicit
        # kwarg documents the no-variant default).
        create_response = await call_unconfirmed_on_transport_loss(
            lambda: self._rpc.rpc_call(
                RPCMethod.CREATE_ARTIFACT,
                build_interactive_mind_map_artifact_params(
                    notebook_id, source_ids, instructions=instructions
                ),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                operation_variant=None,
            ),
            method=RPCMethod.CREATE_ARTIFACT,
            what="CreateArtifact interactive mind map",
        )
        new_id = _new_artifact_id(create_response)
        if new_id is None:
            # ADR-0019 async-kickoff null contract: a null/degenerate
            # CREATE_ARTIFACT means no generation task was created, so raise
            # ArtifactFeatureUnavailableError (a subclass of ArtifactError, so
            # ``except ArtifactError`` still catches it) rather than the bare
            # ArtifactError, matching the sibling generate_* / retry_failed
            # null-create paths (issue #1359).
            raise ArtifactFeatureUnavailableError(
                ArtifactType.MIND_MAP.value,
                method_id=RPCMethod.CREATE_ARTIFACT.value,
            )
        return new_id

    async def _read_interactive_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
    ) -> dict[str, Any] | None:
        """Read one interactive map through ``GET_INTERACTIVE_HTML``."""
        result = await self._rpc.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [mind_map_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # ``extract_interactive_tree_leaf`` re-raises ``UnknownRPCMethodError``
        # on genuine ``[0][9]`` shape drift (failing loud like the sibling HTML
        # accessor) while tolerating an absent ``[3]`` leaf as the legitimate
        # "tree not populated yet" window (issue #1270).
        tree_json = extract_interactive_tree_leaf(result, source="_mind_maps_api.get_tree")
        return _parse_tree(tree_json)
