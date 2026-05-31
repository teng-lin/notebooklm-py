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
import logging
import reprlib
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

logger = logging.getLogger(__name__)

# The interactive (studio-artifact) mind map exposes its ``{"name", "children"}``
# node tree at ``[0][9][3]`` of the ``GET_INTERACTIVE_HTML`` response (vs the HTML
# body at ``[0][9][0]``). The leaf at ``[3]`` is the only position that may be
# legitimately absent during the brief window after completion before the
# options block is fully populated.
_INTERACTIVE_TREE_LEAF_POS = 3


def extract_interactive_tree_leaf(result: Any, *, source: str) -> Any | None:
    """Return the raw ``[0][9][3]`` interactive mind-map tree leaf, or ``None``.

    Distinguishes a *genuinely absent leaf* (the options block is a list but
    its ``[3]`` tree slot is not populated yet — the legitimate "not ready"
    window) from *real shape drift* (``[0]`` or ``[0][9]`` moved out from under
    us, or ``[0][9]`` is no longer a list). Drift re-raises
    ``UnknownRPCMethodError`` so the library fails loud like the sibling HTML
    accessor ``_get_artifact_content`` (issue #1270); only the missing ``[3]``
    leaf within a present *list* options block is tolerated. A tolerated-but-
    absent leaf emits a WARNING with the rpcid/source so a reshape that drops
    just the leaf position still leaves a drift signal in the logs.
    """
    if result is None:
        return None
    # Descend to the options block (``[0][9]``) strictly: if Google moves the
    # interactive payload off ``[0][9]`` entirely, this raises and surfaces the
    # drift instead of masquerading as "not ready".
    options_block = safe_index(
        result,
        0,
        9,
        method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
        source=source,
    )
    # Only a *list* options block too short for index 3 is the legitimate
    # "tree not populated yet" window — a non-list ``[0][9]`` is genuine drift,
    # so fail loud rather than masking it as not-ready. (We raise explicitly
    # rather than via ``safe_index`` because some non-list types — e.g. ``str``
    # — are subscriptable and would not trip ``safe_index``'s descent guard.)
    if not isinstance(options_block, list):
        raise UnknownRPCMethodError(
            f"safe_index drift at path (0, 9): options block is "
            f"{type(options_block).__name__}, not a list",
            method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
            path=(0, 9),
            source=source,
            # ``reprlib.repr`` bounds the diagnostic preview without first
            # materialising the full repr of a pathologically large/deep
            # ``options_block`` (mirrors ``safe_index``'s own ``_truncate``).
            data_at_failure=reprlib.repr(options_block),
        )
    if len(options_block) <= _INTERACTIVE_TREE_LEAF_POS:
        logger.warning(
            "Interactive mind-map tree leaf absent at [0][9][%d] (rpcid=%s, source=%s); "
            "treating as not-yet-populated. If this persists, Google may have reshaped "
            "the %s response.",
            _INTERACTIVE_TREE_LEAF_POS,
            RPCMethod.GET_INTERACTIVE_HTML.value,
            source,
            RPCMethod.GET_INTERACTIVE_HTML.name,
        )
        return None
    return options_block[_INTERACTIVE_TREE_LEAF_POS]


def _tree_title(tree: dict[str, Any] | None, default: str = "Mind Map") -> str:
    """Return a mind-map title from ``tree["name"]`` only when it is a non-empty ``str``.

    The frozen :class:`MindMap.title` is typed ``str``; a malformed tree with a
    ``null``/numeric ``name`` would otherwise smuggle a non-``str`` into it
    (issue #1270). Falls back to ``default`` for any non-string / empty name.
    """
    if tree is not None:
        name = tree.get("name")
        if isinstance(name, str) and name:
            return name
    return default


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
        with ``wait=True`` this polls to completion and then fetches the node
        tree (so the returned :class:`MindMap` carries ``tree`` for both kinds,
        a uniform surface). With ``wait=False`` it returns a pending
        :class:`MindMap` whose ``tree`` is ``None`` until completed.

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
            title = _tree_title(tree)
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
        # ``allow_unclassified=True``: we hold the concrete id from
        # CREATE_ARTIFACT, so id-match a settling type-4 row whose variant slot
        # has not yet filled rather than degrading to the placeholder MindMap.
        art = await self._find_interactive(notebook_id, new_id, allow_unclassified=True)
        # After completion, fetch the tree so interactive maps return the same
        # populated ``MindMap.tree`` as note-backed ones. Skip when not waiting
        # (still pending) — ``get_tree`` would have nothing to read yet.
        tree = (
            await self.get_tree(
                notebook_id, art.id if art is not None else new_id, kind=MindMapKind.INTERACTIVE
            )
            if wait
            else None
        )
        if art is not None:
            return MindMap(
                id=art.id,
                notebook_id=notebook_id,
                title=art.title,
                kind=MindMapKind.INTERACTIVE,
                created_at=art.created_at,
                tree=tree,
            )
        return MindMap(
            id=new_id,
            notebook_id=notebook_id,
            title="Mind Map",
            kind=MindMapKind.INTERACTIVE,
            tree=tree,
        )

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        kind: MindMapKind | None = None,
        return_object: bool = True,
    ) -> MindMap | None:
        """Rename a mind map (dispatches by kind: ``UPDATE_NOTE`` / ``RENAME_ARTIFACT``).

        Omitting ``kind`` triggers an extra list RPC (and possibly a second
        ``LIST_ARTIFACTS`` call) to auto-detect the backing; pass ``kind`` to skip it.

        Args:
            return_object: When ``True`` (default), re-fetch and return the
                renamed :class:`~notebooklm.types.MindMap`. When ``False``,
                skip the re-fetch and return ``None``.

        Returns:
            The renamed :class:`~notebooklm.types.MindMap`, or ``None`` when
            ``return_object=False``.

        Raises:
            ValueError: if no mind map with ``mind_map_id`` exists. (Mind maps
                have no dedicated ``*NotFoundError``; absence is detected via a
                content/list lookup, not a transport 404. The detection is the
                same fail-loud-on-missing policy as ``sources``/``artifacts``
                rename — issue #1255.)

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned ``None`` even on success.
            Now re-fetches and returns the renamed ``MindMap`` (issue #1255).
            Added the ``return_object`` opt-out.
        """
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Error precedence matches ``_detect_kind``:
            # note-backed first, then interactive, then ``ValueError``.
            for row in await self._mind_maps.list_mind_maps(notebook_id):
                if NoteRow(row).id == mind_map_id:
                    await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)
                    return await self._hydrate_renamed(notebook_id, mind_map_id, return_object)
            if await self._find_interactive(notebook_id, mind_map_id) is not None:
                # ``return_object=False`` on the artifact rename: hydration (if
                # requested) is done once below via ``self.get`` so the
                # interactive path doesn't also re-fetch.
                await self._artifacts.rename(
                    notebook_id, mind_map_id, new_title, return_object=False
                )
                return await self._hydrate_renamed(notebook_id, mind_map_id, return_object)
            raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")
        if kind == MindMapKind.NOTE_BACKED:
            await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)
        else:
            # Pre-validate the id on the explicit-interactive path. Without this,
            # ``RENAME_ARTIFACT`` silently no-ops on a wrong id (the RPC returns
            # null), diverging from the ``kind=None`` path which raises
            # ``ValueError`` for an unknown id. Fail loud instead (issue #1270;
            # aligns with the "fail loud + return object" direction of #1255).
            if await self._find_interactive(notebook_id, mind_map_id) is None:
                raise ValueError(
                    f"Interactive mind map {mind_map_id!r} not found in notebook {notebook_id!r}"
                )
            await self._artifacts.rename(notebook_id, mind_map_id, new_title, return_object=False)
        return await self._hydrate_renamed(notebook_id, mind_map_id, return_object)

    async def _hydrate_renamed(
        self, notebook_id: str, mind_map_id: str, return_object: bool
    ) -> MindMap | None:
        """Re-fetch the renamed map (or skip when ``return_object=False``).

        A ``None`` from ``get`` here means the map is absent — surface it as
        the same ``ValueError`` the missing-target dispatch paths raise rather
        than returning a stale/absent object. For paths that pre-validate the
        id (auto-detect and explicit-interactive) this is a vanished-between-
        rename-and-refetch race; for the explicit ``kind=NOTE_BACKED`` path it
        is the primary missing-target signal. Either way, absent → raise.
        """
        if not return_object:
            return None
        mind_map = await self.get(notebook_id, mind_map_id)
        if mind_map is None:
            raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")
        return mind_map

    async def delete(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> None:
        """Delete a mind map (dispatches by kind: ``DELETE_NOTE`` / ``DELETE_ARTIFACT``).

        Omitting ``kind`` triggers an extra list RPC (and possibly a second
        ``LIST_ARTIFACTS`` call) to auto-detect the backing; pass ``kind`` to skip it.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211).

        .. note::
            Unlike ``sources``/``artifacts``/``notes`` delete, this is **not**
            idempotent on a missing target: when ``kind`` is omitted it routes
            through ``_detect_kind``, which raises ``ValueError`` for an unknown
            id (it must list to pick the right RPC family). Pass ``kind`` to
            skip detection and delete idempotently.
        """
        kind = kind or await self._detect_kind(notebook_id, mind_map_id)
        if kind == MindMapKind.NOTE_BACKED:
            await self._mind_maps.delete_mind_map(notebook_id, mind_map_id)
        else:
            await self._artifacts.delete(notebook_id, mind_map_id)

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
        # ``extract_interactive_tree_leaf`` re-raises ``UnknownRPCMethodError``
        # on genuine ``[0][9]`` shape drift (failing loud like the sibling HTML
        # accessor) while tolerating an absent ``[3]`` leaf as the legitimate
        # "tree not populated yet" window (issue #1270).
        tree_json = extract_interactive_tree_leaf(result, source="_mind_maps_api.get_tree")
        return _parse_tree(tree_json)

    async def _detect_kind(self, notebook_id: str, mind_map_id: str) -> MindMapKind:
        """Resolve a bare id to its backing (note collection first, then studio)."""
        for row in await self._mind_maps.list_mind_maps(notebook_id):
            if NoteRow(row).id == mind_map_id:
                return MindMapKind.NOTE_BACKED
        if await self._find_interactive(notebook_id, mind_map_id) is not None:
            return MindMapKind.INTERACTIVE
        raise ValueError(f"Mind map {mind_map_id!r} not found in notebook {notebook_id!r}")

    async def _find_interactive(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        allow_unclassified: bool = False,
    ) -> Any | None:
        """Resolve a known interactive-mind-map id to its :class:`Artifact`.

        By default matches only a *confirmed* interactive map
        (``type 4 / variant 4``) so the auto-detect ``rename`` / ``delete`` /
        ``get_tree`` callers and the explicit-interactive ``rename`` validation
        never mistake a settling (or malformed) quiz/flashcard — also a type-4
        row that may transiently read ``variant=None`` — for a mind map.

        ``allow_unclassified=True`` additionally accepts a type-4 row whose
        ``variant`` slot has not yet populated (``variant=None``). Only the
        ``generate`` path passes this: it already holds the concrete id returned
        by ``CREATE_ARTIFACT`` for an interactive map, so id-matching the
        settling artifact is safe there and keeps ``generate(wait=True)`` from
        degrading to the ``title="Mind Map"`` placeholder (no ``created_at``)
        when completion is observed a tick before the variant slot fills
        (issue #1270).

        Lists unfiltered (rather than filtered to ``MIND_MAP``) because a
        ``variant=None`` type-4 row is *excluded* from the ``MIND_MAP`` filter
        and would otherwise be invisible during the settling window.
        """
        for art in await self._artifacts.list(notebook_id):
            if art.id != artifact_id:
                continue
            if art.is_interactive_mind_map or (allow_unclassified and art.is_unclassified_type4):
                return art
        return None
