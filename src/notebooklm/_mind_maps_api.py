"""Backend-neutral unified mind-map namespace contract."""

from __future__ import annotations

import builtins
import contextlib
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ._lookup import unwrap_or_raise
from ._runtime.call_supervisor import OperationLease
from ._types.mind_maps import MindMap, MindMapKind
from .exceptions import ArtifactNotReadyError, MindMapNotFoundError
from .types import Artifact

if TYPE_CHECKING:
    from ._artifacts import ArtifactsAPI
    from ._notes import NotesAPI


class MindMapsAPI(ABC):
    """``client.mind_maps`` — one surface over both mind-map backends."""

    _reject_unsuccessful_interactive_wait = False

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(self, *, artifacts: ArtifactsAPI, notes: NotesAPI) -> None:
        self._artifacts = artifacts
        self._notes = notes

    @abstractmethod
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
        async with self._operation_scope("mind_maps.list"):
            # Shallow-copy so appending interactive entries can never mutate a list
            # a (future) caching/overriding list_note_backed might share.
            result: builtins.list[MindMap] = list(await self.list_note_backed(notebook_id))
            for art in await self._list_studio_mind_map_rows(notebook_id):
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

        The historical terminal-failure behavior remains backend-specific:
        Android raises :class:`ArtifactNotReadyError` when a waited interactive
        task finishes failed or removed, while web continues hydration after
        its completion wait without adding that extra rejection.

        ``instructions`` is a free-text prompt that steers generation; it is sent
        for both kinds — note-backed via ``GENERATE_MIND_MAP`` and interactive at
        the ``[9][1][2]`` prompt slot of ``CREATE_ARTIFACT`` (the same slot
        quiz/flashcards use; the server honours it for variant 4, verified live).
        ``language`` applies to note-backed payloads on both backends and to
        Android interactive generation; the web interactive payload has no
        language slot and ignores it.

        Raises:
            ArtifactFeatureUnavailableError: if the interactive
                ``CREATE_ARTIFACT`` call returns no artifact id (null or
                unexpected response shape) — no generation task was created.
                A subclass of :class:`~notebooklm.exceptions.ArtifactError`, so
                ``except ArtifactError`` still catches it; aligns the interactive
                async kickoff with the sibling ``generate_*`` / ``retry_failed``
                null-create contract (ADR-0019; issue #1359).
        """
        async with self._operation_scope("mind_maps.generate"):
            if kind == MindMapKind.NOTE_BACKED:
                result = await self._artifacts.generate_mind_map(
                    notebook_id,
                    source_ids,
                    language,
                    instructions,
                )
                tree = result.mind_map if isinstance(result.mind_map, dict) else None
                return MindMap(
                    id=result.note_id or "",
                    notebook_id=notebook_id,
                    title=_tree_title(tree),
                    kind=MindMapKind.NOTE_BACKED,
                    created_at=result.created_at,
                    tree=tree,
                )

            new_id = await self._start_interactive_mind_map(
                notebook_id,
                source_ids,
                language=language,
                instructions=instructions,
            )
            if wait:
                terminal = await self._artifacts.wait_for_completion(notebook_id, new_id)
                if self._reject_unsuccessful_interactive_wait and (
                    terminal.is_failed or terminal.is_removed
                ):
                    raise ArtifactNotReadyError(
                        "mind_map",
                        artifact_id=new_id,
                        status=str(terminal.status),
                    )
            artifact = await self._find_interactive(
                notebook_id,
                new_id,
                allow_unclassified=True,
            )
            tree = (
                await self.get_tree(
                    notebook_id,
                    new_id,
                    kind=MindMapKind.INTERACTIVE,
                )
                if wait
                else None
            )
            return MindMap(
                id=new_id,
                notebook_id=notebook_id,
                title=artifact.title if artifact is not None else "Mind Map",
                kind=MindMapKind.INTERACTIVE,
                created_at=artifact.created_at if artifact is not None else None,
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
        async with self._operation_scope("mind_maps.rename"):
            return await self._rename_in_scope(
                notebook_id,
                mind_map_id,
                new_title,
                kind=kind,
                return_object=return_object,
            )

    async def _rename_in_scope(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        kind: MindMapKind | None = None,
        return_object: bool = True,
    ) -> MindMap | None:
        if kind is None:
            # Auto-detect inline so the note-backed list is fetched once rather
            # than twice (a separate ``_detect_kind`` call would re-issue
            # ``list_mind_maps``). Error precedence matches ``_detect_kind``:
            # note-backed first, then interactive, then ``MindMapNotFoundError``.
            for mind_map in await self.list_note_backed(notebook_id):
                if mind_map.id == mind_map_id:
                    await self._send_rename_note_backed(notebook_id, mind_map_id, new_title)
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
            await self._send_rename_note_backed(notebook_id, mind_map_id, new_title)
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
        async with self._operation_scope("mind_maps.delete"):
            await self._delete_in_scope(notebook_id, mind_map_id, kind=kind)

    async def _delete_in_scope(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> None:
        if kind is None:
            try:
                kind = await self._detect_kind(notebook_id, mind_map_id)
            except MindMapNotFoundError:
                # Already absent — deletion is idempotent (ADR-0019), matching
                # the kind-supplied path (whose delete RPCs are no-ops on a
                # missing id) and the sibling sources/artifacts/notes deletes.
                return None
        if kind == MindMapKind.NOTE_BACKED:
            await self._notes.delete_mind_map(notebook_id, mind_map_id)
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
        async with self._operation_scope("mind_maps.get_tree"):
            if kind is MindMapKind.INTERACTIVE:
                return await self._read_interactive_tree(notebook_id, mind_map_id)

            for mind_map in await self.list_note_backed(notebook_id):
                if mind_map.id == mind_map_id:
                    return mind_map.tree
            if kind is MindMapKind.NOTE_BACKED:
                return None

            if await self._find_interactive(notebook_id, mind_map_id) is not None:
                return await self._read_interactive_tree(notebook_id, mind_map_id)
            return None

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
        async with self._operation_scope("mind_maps._detect_kind"):
            for mind_map in await self.list_note_backed(notebook_id):
                if mind_map.id == mind_map_id:
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
        for art in await self._list_studio_mind_map_rows(notebook_id):
            if art.id != artifact_id:
                continue
            if art.is_interactive_mind_map or (allow_unclassified and art.is_unclassified_type4):
                return art
        return None

    @abstractmethod
    async def _list_studio_mind_map_rows(self, notebook_id: str) -> builtins.list[Artifact]:
        """Return unfiltered Studio rows used to classify interactive maps."""

    @abstractmethod
    async def _start_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> str:
        """Start an interactive mind map and return its artifact id."""

    @abstractmethod
    async def _read_interactive_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
    ) -> dict[str, Any] | None:
        """Read one interactive mind map's tree."""

    @abstractmethod
    async def _send_rename_note_backed(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Rename a note-backed mind map through the active backend."""


def _tree_title(tree: dict[str, Any] | None, default: str = "Mind Map") -> str:
    """Return the non-empty string title from a decoded tree."""
    if tree is not None:
        name = tree.get("name")
        if isinstance(name, str) and name:
            return name
    return default
