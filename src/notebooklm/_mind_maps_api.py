"""Unified mind-map API (``client.mind_maps``).

Hides the two backends (note-backed JSON vs interactive studio-artifact) behind a
single surface that dispatches each operation to the correct RPC family
(issue #1256). Note-backed maps use the note RPCs (``GENERATE_MIND_MAP`` /
``UPDATE_NOTE`` / ``DELETE_NOTE``); interactive maps use the studio-artifact RPCs
(``CREATE_ARTIFACT`` type-4/variant-4 / ``RENAME_ARTIFACT`` / ``DELETE_ARTIFACT`` /
``GET_INTERACTIVE_HTML``).
"""

from __future__ import annotations

import builtins
import json
from typing import TYPE_CHECKING, Any

from ._artifact_payloads import build_interactive_mind_map_artifact_params
from ._row_adapters_notes import NoteRow
from ._types.mind_maps import MindMap, MindMapKind
from .exceptions import ArtifactError, UnknownRPCMethodError
from .rpc import RPCMethod, safe_index
from .types import ArtifactType

if TYPE_CHECKING:
    from ._artifacts import ArtifactsAPI
    from ._mind_map import NoteBackedMindMapService
    from ._notebooks import NotebooksAPI
    from ._runtime_contracts import RpcCaller


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
    """Pull the new artifact id out of a ``CREATE_ARTIFACT`` response (``[[id, …]]``)."""
    if (
        isinstance(create_response, list)
        and create_response
        and isinstance(create_response[0], list)
        and create_response[0]
        and isinstance(create_response[0][0], str)
    ):
        return create_response[0][0]
    return None


class MindMapsAPI:
    """``client.mind_maps`` — one surface over both mind-map backends."""

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        mind_maps: NoteBackedMindMapService,
        artifacts: ArtifactsAPI,
        notebooks: NotebooksAPI,
    ) -> None:
        self._rpc = rpc
        self._mind_maps = mind_maps
        self._artifacts = artifacts
        self._notebooks = notebooks

    async def list(self, notebook_id: str) -> builtins.list[MindMap]:
        """List all mind maps in a notebook — both backings, as distinct entries."""
        result: builtins.list[MindMap] = []
        for row in await self._mind_maps.list_mind_maps(notebook_id):
            note_row = NoteRow(row)
            result.append(
                MindMap(
                    id=note_row.id,
                    notebook_id=notebook_id,
                    title=note_row.title,
                    kind=MindMapKind.NOTE_BACKED,
                    tree=_parse_tree(self._mind_maps.extract_content(row)),
                )
            )
        for art in await self._artifacts.list(notebook_id, ArtifactType.MIND_MAP):
            if art.is_interactive_mind_map:
                result.append(
                    MindMap(
                        id=art.id,
                        notebook_id=notebook_id,
                        title=art.title,
                        kind=MindMapKind.INTERACTIVE,
                        created_at=art.created_at,
                    )
                )
        return result

    async def get(self, notebook_id: str, mind_map_id: str) -> MindMap | None:
        """Return the mind map with ``mind_map_id``, or ``None`` if absent."""
        for mind_map in await self.list(notebook_id):
            if mind_map.id == mind_map_id:
                return mind_map
        return None

    async def generate(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        *,
        kind: MindMapKind,
        language: str | None = "en",
        instructions: str | None = None,
        wait: bool = True,
    ) -> MindMap:
        """Generate a mind map of the requested ``kind``.

        ``NOTE_BACKED`` is synchronous (``GENERATE_MIND_MAP`` returns the tree).
        ``INTERACTIVE`` is async (``CREATE_ARTIFACT`` returns a pending artifact);
        with ``wait=True`` this polls to completion, otherwise it returns a
        pending :class:`MindMap` whose ``tree`` is ``None`` until completed.

        ``language`` and ``instructions`` only apply to ``NOTE_BACKED`` maps; the
        interactive ``CREATE_ARTIFACT`` payload does not accept them, so they are
        ignored when ``kind=INTERACTIVE``.

        Raises:
            ArtifactError: if the interactive ``CREATE_ARTIFACT`` call returns no
                artifact id (null or unexpected response shape).
        """
        if kind == MindMapKind.NOTE_BACKED:
            res = await self._artifacts.generate_mind_map(
                notebook_id, source_ids, language, instructions
            )
            tree = res.mind_map if isinstance(res.mind_map, dict) else None
            title = tree["name"] if tree and "name" in tree else "Mind Map"
            return MindMap(
                id=res.note_id or "",
                notebook_id=notebook_id,
                title=title,
                kind=MindMapKind.NOTE_BACKED,
                tree=tree,
            )

        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)
        # CREATE_ARTIFACT is classified in ``_idempotency.py``. ``operation_variant=None``
        # is passed explicitly to match the other CREATE_ARTIFACT / GENERATE_MIND_MAP
        # call sites (the registry resolves the same entry either way; the explicit
        # kwarg documents the no-variant default).
        create_response = await self._rpc.rpc_call(
            RPCMethod.CREATE_ARTIFACT,
            build_interactive_mind_map_artifact_params(notebook_id, source_ids),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,
        )
        new_id = _new_artifact_id(create_response)
        if new_id is None:
            raise ArtifactError(
                "CREATE_ARTIFACT returned no artifact id for the interactive mind map "
                f"in notebook {notebook_id!r} (null or unexpected response shape)."
            )
        if wait:
            await self._artifacts.wait_for_completion(notebook_id, new_id)
        art = await self._find_interactive(notebook_id, new_id)
        if art is not None:
            return MindMap(
                id=art.id,
                notebook_id=notebook_id,
                title=art.title,
                kind=MindMapKind.INTERACTIVE,
                created_at=art.created_at,
            )
        return MindMap(
            id=new_id,
            notebook_id=notebook_id,
            title="Mind Map",
            kind=MindMapKind.INTERACTIVE,
        )

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        kind: MindMapKind | None = None,
    ) -> None:
        """Rename a mind map (dispatches by kind: ``UPDATE_NOTE`` / ``RENAME_ARTIFACT``).

        Omitting ``kind`` triggers an extra list RPC (and possibly a second
        ``LIST_ARTIFACTS`` call) to auto-detect the backing; pass ``kind`` to skip it.
        """
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Error precedence matches ``_detect_kind``:
            # note-backed first, then interactive, then ``ValueError``.
            for row in await self._mind_maps.list_mind_maps(notebook_id):
                if NoteRow(row).id == mind_map_id:
                    await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)
                    return
            if await self._find_interactive(notebook_id, mind_map_id) is not None:
                await self._artifacts.rename(notebook_id, mind_map_id, new_title)
                return
            raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")
        if kind == MindMapKind.NOTE_BACKED:
            await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)
        else:
            await self._artifacts.rename(notebook_id, mind_map_id, new_title)

    async def delete(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> bool:
        """Delete a mind map (dispatches by kind: ``DELETE_NOTE`` / ``DELETE_ARTIFACT``).

        Omitting ``kind`` triggers an extra list RPC (and possibly a second
        ``LIST_ARTIFACTS`` call) to auto-detect the backing; pass ``kind`` to skip it.
        """
        kind = kind or await self._detect_kind(notebook_id, mind_map_id)
        if kind == MindMapKind.NOTE_BACKED:
            return await self._mind_maps.delete_mind_map(notebook_id, mind_map_id)
        return await self._artifacts.delete(notebook_id, mind_map_id)

    async def get_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> dict[str, Any] | None:
        """Return the ``{"name", "children"}`` node tree for a mind map.

        Note-backed maps parse the tree from their note content; interactive maps
        fetch it via ``GET_INTERACTIVE_HTML`` (the tree is at ``[0][9][3]``).

        Omitting ``kind`` triggers an extra list RPC (and possibly a second
        ``LIST_ARTIFACTS`` call) to auto-detect the backing; pass ``kind`` to skip it.
        """
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Error precedence matches ``_detect_kind``:
            # note-backed first (return its parsed tree), then interactive
            # (fall through to the RPC), then ``ValueError``.
            for row in await self._mind_maps.list_mind_maps(notebook_id):
                if NoteRow(row).id == mind_map_id:
                    return _parse_tree(self._mind_maps.extract_content(row))
            if await self._find_interactive(notebook_id, mind_map_id) is None:
                raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")
        elif kind == MindMapKind.NOTE_BACKED:
            for row in await self._mind_maps.list_mind_maps(notebook_id):
                if NoteRow(row).id == mind_map_id:
                    return _parse_tree(self._mind_maps.extract_content(row))
            return None
        result = await self._rpc.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [mind_map_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        try:
            tree_json = safe_index(
                result,
                0,
                9,
                3,
                method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
                source="_mind_maps_api.get_tree",
            )
        except UnknownRPCMethodError:
            return None
        return _parse_tree(tree_json)

    async def _detect_kind(self, notebook_id: str, mind_map_id: str) -> MindMapKind:
        """Resolve a bare id to its backing (note collection first, then studio)."""
        for row in await self._mind_maps.list_mind_maps(notebook_id):
            if NoteRow(row).id == mind_map_id:
                return MindMapKind.NOTE_BACKED
        if await self._find_interactive(notebook_id, mind_map_id) is not None:
            return MindMapKind.INTERACTIVE
        raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")

    async def _find_interactive(self, notebook_id: str, artifact_id: str) -> Any | None:
        for art in await self._artifacts.list(notebook_id, ArtifactType.MIND_MAP):
            if art.id == artifact_id and art.is_interactive_mind_map:
                return art
        return None
