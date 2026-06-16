"""Unified mind-map API (``client.mind_maps``).

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
import logging
import reprlib
from typing import TYPE_CHECKING, Any

from ._artifact.payloads import build_interactive_mind_map_artifact_params
from ._lookup import unwrap_or_raise
from ._row_adapters.notes import NoteRow
from ._types.mind_maps import MindMap, MindMapKind
from .exceptions import (
    ArtifactFeatureUnavailableError,
    MindMapNotFoundError,
    UnknownRPCMethodError,
)
from .rpc import RPCMethod, safe_index
from .types import ArtifactType

if TYPE_CHECKING:
    from ._artifacts import ArtifactsAPI
    from ._mind_map import NoteBackedMindMapService
    from ._notebooks import NotebooksAPI
    from ._runtime.contracts import RpcCaller

logger = logging.getLogger(__name__)

# ``GET_INTERACTIVE_HTML`` is the live ``GetArtifact`` — a generic
# single-artifact getter, not an interactive-HTML-specific endpoint. For an
# interactive (studio-artifact) mind map its response carries the
# ``{"name", "children"}`` node tree at ``[0][9][3]`` (vs the HTML body at
# ``[0][9][0]``). The leaf at ``[3]`` is the only position that may be
# legitimately absent during the brief window after completion before the
# options block is fully populated.
_INTERACTIVE_TREE_LEAF_POS = 3

# ``CREATE_ARTIFACT`` returns the new artifact id wrapped as ``[[id, …]]``: the
# inner row sits at ``[0]`` of the envelope and the id is that row's ``[0]``
# leaf. Both descents are guarded for presence before ``safe_index`` reads them
# (see ``_new_artifact_id``).
_CREATE_ARTIFACT_ENVELOPE_POS = 0
_CREATE_ARTIFACT_ID_POS = 0


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

    async def list(self, notebook_id: str) -> builtins.list[MindMap]:
        """List all mind maps in a notebook — both backings, as distinct entries.

        ``MindMap.tree`` is populated only for **note-backed** entries (parsed
        for free from the already-listed note content). **Interactive** entries
        carry ``tree=None``: fetching each tree would cost a separate
        ``GET_INTERACTIVE_HTML`` per map, so ``list`` leaves it unfetched. A
        ``None`` ``tree`` on an interactive entry therefore means "not fetched",
        not "empty" — call :meth:`get_tree` with ``kind=INTERACTIVE`` to fetch
        an individual interactive tree.
        """
        # Shallow-copy so appending interactive entries can never mutate a list
        # a (future) caching/overriding list_note_backed might share.
        result: builtins.list[MindMap] = list(await self.list_note_backed(notebook_id))
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

    async def get(self, notebook_id: str, mind_map_id: str) -> MindMap:
        """Return the mind map with ``mind_map_id``.

        Returns:
            The :class:`~notebooklm.types.MindMap`.

        Raises:
            MindMapNotFoundError: If no mind map with ``mind_map_id`` exists
                (matches ``notebooks.get``; issue #1247). Use :meth:`get_or_none`
                for the sanctioned ``None``-on-miss lookup.
        """
        # ``_lookup.unwrap_or_raise`` single-sources the raise-on-miss decision
        # (#1247). Internal callers that need the silent optional-lookup must
        # use ``_get_or_none`` directly.
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, mind_map_id),
            MindMapNotFoundError(mind_map_id),
        )

    async def get_or_none(self, notebook_id: str, mind_map_id: str) -> MindMap | None:
        """Get a mind map by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019), spanning both
        backings (note-backed JSON + interactive studio-artifact). Unlike
        :meth:`get` — which now raises
        :class:`~notebooklm.exceptions.MindMapNotFoundError` on a miss
        (#1247) — this returns ``None`` for an absence and emits no
        deprecation warning. It scans :meth:`list`, so it reflects only what
        ``list`` confirms: a just-created interactive map whose variant slot has
        not yet populated is briefly excluded from ``list`` and therefore reads
        as ``None`` until it settles (the same settling window ``list`` and
        ``get_tree`` see). Transport, auth, and decode faults raised while
        listing either backing are **not** swallowed.

        Args:
            notebook_id: The notebook ID.
            mind_map_id: The mind map ID.

        Returns:
            The :class:`~notebooklm.types.MindMap`, or ``None`` if not found.
        """
        for mind_map in await self.list(notebook_id):
            if mind_map.id == mind_map_id:
                return mind_map
        return None

    # Private alias for internal optional-lookup callers, mirroring
    # ``sources``/``artifacts``/``notes``: the library calls ``_get_or_none``
    # for a ``None``-on-miss lookup rather than the raising ``get()`` (#1358).
    _get_or_none = get_or_none

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

        ``instructions`` is a free-text prompt that steers generation; it is sent
        for both kinds — note-backed via ``GENERATE_MIND_MAP`` and interactive at
        the ``[9][1][2]`` prompt slot of ``CREATE_ARTIFACT`` (the same slot
        quiz/flashcards use; the server honours it for variant 4, verified live).
        ``language`` applies to the note-backed payload only.

        Raises:
            ArtifactFeatureUnavailableError: if the interactive
                ``CREATE_ARTIFACT`` call returns no artifact id (null or
                unexpected response shape) — no generation task was created.
                A subclass of :class:`~notebooklm.exceptions.ArtifactError`, so
                ``except ArtifactError`` still catches it; aligns the interactive
                async kickoff with the sibling ``generate_*`` / ``retry_failed``
                null-create contract (ADR-0019; issue #1359).
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
                created_at=res.created_at,
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
            build_interactive_mind_map_artifact_params(
                notebook_id, source_ids, instructions=instructions
            ),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,
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
            MindMapNotFoundError: if no mind map with ``mind_map_id`` exists.
                Absence is detected via a content/list lookup, not a transport
                404, but is still surfaced as a ``*NotFoundError`` so callers can
                ``except NotFoundError`` (or ``except MindMapError``) uniformly
                across namespaces (ADR-0019; issues #1255, #1291).

        .. note::
            Mind maps detect absence via a content/list lookup before
            dispatching the rename RPC, matching the v0.8.0 existence-preflight
            contract for sources/artifacts rename.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned ``None`` even on success.
            Now re-fetches and returns the renamed ``MindMap`` (issue #1255).
            Added the ``return_object`` opt-out.
        """
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Error precedence matches ``_detect_kind``:
            # note-backed first, then interactive, then ``MindMapNotFoundError``.
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
            raise MindMapNotFoundError(mind_map_id)
        if kind == MindMapKind.NOTE_BACKED:
            await self._mind_maps.rename_mind_map(notebook_id, mind_map_id, new_title)
        else:
            # Pre-validate the id on the explicit-interactive path. Without this,
            # ``RENAME_ARTIFACT`` silently no-ops on a wrong id (the RPC returns
            # null), diverging from the ``kind=None`` path which raises
            # ``MindMapNotFoundError`` for an unknown id. Fail loud instead
            # (issue #1270; aligns with the "fail loud + return object" direction
            # of #1255).
            if await self._find_interactive(notebook_id, mind_map_id) is None:
                raise MindMapNotFoundError(mind_map_id)
            await self._artifacts.rename(notebook_id, mind_map_id, new_title, return_object=False)
        return await self._hydrate_renamed(notebook_id, mind_map_id, return_object)

    async def _hydrate_renamed(
        self, notebook_id: str, mind_map_id: str, return_object: bool
    ) -> MindMap | None:
        """Re-fetch the renamed map (or skip when ``return_object=False``).

        A ``None`` from ``_get_or_none`` here means the map is absent — surface it as
        the same ``MindMapNotFoundError`` the missing-target dispatch paths
        raise rather than returning a stale/absent object. For paths that
        pre-validate the id (auto-detect and explicit-interactive) this is a
        vanished-between-rename-and-refetch race; for the explicit
        ``kind=NOTE_BACKED`` path it is the primary missing-target signal.
        Either way, absent → raise.
        """
        if not return_object:
            return None
        # ``_get_or_none`` is used so the internal re-fetch can convert a
        # vanished map into ``MindMapNotFoundError`` itself.
        mind_map = await self._get_or_none(notebook_id, mind_map_id)
        if mind_map is None:
            raise MindMapNotFoundError(mind_map_id)
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

        Idempotent on a missing target: like ``sources``/``artifacts``/``notes``
        delete, deleting an already-absent mind map is a no-op that returns
        ``None`` (ADR-0019). When ``kind`` is omitted, ``_detect_kind`` lists to
        pick the right RPC family and raises ``MindMapNotFoundError`` for an
        unknown id; that already-absent signal is swallowed here.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). Auto-detect (``kind=None``) is
            now idempotent on a missing target rather than raising (issue #1291).
        """
        if kind is None:
            try:
                kind = await self._detect_kind(notebook_id, mind_map_id)
            except MindMapNotFoundError:
                # Already absent — deletion is idempotent (ADR-0019), matching
                # the kind-supplied path (whose delete RPCs are no-ops on a
                # missing id) and the sibling sources/artifacts/notes deletes.
                return None
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

        As a derived read (ADR-0019), this does **not** police parent existence:
        a missing mind map and an existing-but-unpopulated (not-ready) one both
        return ``None``. Use :meth:`get` to distinguish absence from emptiness.
        Shape-drift in the interactive payload still raises
        :class:`~notebooklm.exceptions.UnknownRPCMethodError` (issue #1270).

        .. note::
            The ``kind=None`` (auto-detect) and ``kind=NOTE_BACKED`` paths
            enforce the ``None``-on-missing contract client-side (they confirm
            the id exists before reading). The explicit
            ``kind=MindMapKind.INTERACTIVE`` path instead **delegates absence
            detection to the RPC**: it does no pre-validation and passes the id
            straight to ``GET_INTERACTIVE_HTML`` (with ``allow_null=True``), so a
            missing id's value is server-dependent — the server returns null
            today, which flows through to ``None``, but that is not enforced
            client-side. Skipping the pre-validation avoids an extra
            ``LIST_ARTIFACTS`` round-trip on the explicit-kind fast path (issue
            #1355).
        """
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Precedence matches ``_detect_kind``: note-backed
            # first (return its parsed tree), then interactive (fall through to the
            # RPC). A miss in both backings returns ``None`` rather than raising —
            # derived reads return the uniform-empty value on a missing parent.
            for row in await self._mind_maps.list_mind_maps(notebook_id):
                if NoteRow(row).id == mind_map_id:
                    return _parse_tree(self._mind_maps.extract_content(row))
            if await self._find_interactive(notebook_id, mind_map_id) is None:
                return None
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
        """Resolve a bare id to its backing (note collection first, then studio).

        Used by ``delete(kind=None)``, which swallows a missing-id
        :class:`~notebooklm.exceptions.MindMapNotFoundError` to ``None``. The
        ``rename`` / ``get_tree`` auto-detect paths do **not** call this — they
        inline the same note-first/interactive-second resolution to avoid a
        second ``list_mind_maps`` RPC, but mirror its precedence and raise type
        (ADR-0019: one resolution rule, interpreted per operation class —
        mutate-existing re-raises, derived reads return the uniform-empty
        value, idempotent delete swallows it).
        """
        for row in await self._mind_maps.list_mind_maps(notebook_id):
            if NoteRow(row).id == mind_map_id:
                return MindMapKind.NOTE_BACKED
        if await self._find_interactive(notebook_id, mind_map_id) is not None:
            return MindMapKind.INTERACTIVE
        raise MindMapNotFoundError(mind_map_id)

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
