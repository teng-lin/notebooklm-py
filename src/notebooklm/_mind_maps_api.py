"""Unified public mind-map facade over two semantic domain services."""

from __future__ import annotations

import builtins
import json
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

from ._backend_compat import project_backend_call
from ._lookup import unwrap_or_raise
from ._projectors import project_mind_map
from ._types.mind_maps import MindMap, MindMapKind
from ._web.codec.mind_maps import (
    decode_created_interactive_id,
    extract_interactive_tree_leaf,
)
from .exceptions import MindMapNotFoundError

if TYPE_CHECKING:
    from ._note_service import NoteService
    from ._studio import MindMapFamilyService

_T = TypeVar("_T")


def _parse_tree(content: Any) -> dict[str, Any] | None:
    """Parse a mind-map JSON object while preserving the soft-invalid contract."""
    if not isinstance(content, str) or not content:
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _tree_title(tree: dict[str, Any] | None, default: str = "Mind Map") -> str:
    """Return a non-empty textual tree title or the stable placeholder."""
    if tree is not None:
        name = tree.get("name")
        if isinstance(name, str) and name:
            return name
    return default


def _new_artifact_id(create_response: Any) -> str | None:
    """Compatibility alias for the retired facade-owned CREATE_ARTIFACT codec."""
    return decode_created_interactive_id(create_response)


class MindMapsAPI:
    """``client.mind_maps`` — one public surface over both representations."""

    def __init__(self, *, notes: NoteService, studio: MindMapFamilyService) -> None:
        self._notes = notes
        self._studio = studio

    @staticmethod
    async def _backend_call(awaitable: Awaitable[_T]) -> _T:
        return await project_backend_call(awaitable)

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """List note-backed mind maps without consulting the Studio catalog."""
        records = await self._backend_call(self._notes.list_mind_maps(notebook_id))
        return [project_mind_map(record) for record in records]

    async def list(self, notebook_id: str) -> builtins.list[MindMap]:
        """List both representations in stable note-backed-then-interactive order."""
        result = list(await self.list_note_backed(notebook_id))
        result.extend(await self._backend_call(self._studio.list_mind_maps(notebook_id)))
        return result

    async def get(self, notebook_id: str, mind_map_id: str) -> MindMap:
        """Return one exact mind map or raise ``MindMapNotFoundError``."""
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, mind_map_id),
            MindMapNotFoundError(mind_map_id),
        )

    async def get_or_none(self, notebook_id: str, mind_map_id: str) -> MindMap | None:
        """Return one exact mind map, or ``None`` for a genuine miss."""
        return next(
            (item for item in await self.list(notebook_id) if item.id == mind_map_id),
            None,
        )

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
        """Generate through the selected semantic note or Studio family."""
        if kind == MindMapKind.NOTE_BACKED:
            return project_mind_map(
                await self._backend_call(
                    self._notes.generate_mind_map(
                        notebook_id,
                        source_ids,
                        language,
                        instructions,
                    )
                )
            )
        return await self._backend_call(
            self._studio.generate(
                notebook_id,
                source_ids,
                instructions,
                wait=wait,
            )
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
        """Rename with note-first auto-detection and exact absence semantics."""
        if kind is None:
            note_maps = await self.list_note_backed(notebook_id)
            if any(item.id == mind_map_id for item in note_maps):
                await self._backend_call(
                    self._notes.rename_mind_map(notebook_id, mind_map_id, new_title)
                )
            elif (
                await self._backend_call(self._studio.get_or_none(notebook_id, mind_map_id))
                is not None
            ):
                await self._backend_call(self._studio.rename(notebook_id, mind_map_id, new_title))
            else:
                raise MindMapNotFoundError(mind_map_id)
        elif kind == MindMapKind.NOTE_BACKED:
            await self._backend_call(
                self._notes.rename_mind_map(notebook_id, mind_map_id, new_title)
            )
        else:
            if await self._backend_call(self._studio.get_or_none(notebook_id, mind_map_id)) is None:
                raise MindMapNotFoundError(mind_map_id)
            await self._backend_call(self._studio.rename(notebook_id, mind_map_id, new_title))
        return await self._hydrate_renamed(notebook_id, mind_map_id, return_object)

    async def _hydrate_renamed(
        self,
        notebook_id: str,
        mind_map_id: str,
        return_object: bool,
    ) -> MindMap | None:
        if not return_object:
            return None
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
        """Delete idempotently while retaining note-first kind auto-detection."""
        if kind is None:
            try:
                kind = await self._detect_kind(notebook_id, mind_map_id)
            except MindMapNotFoundError:
                return None
        if kind == MindMapKind.NOTE_BACKED:
            await self._backend_call(self._notes.delete_mind_map(notebook_id, mind_map_id))
        else:
            await self._backend_call(self._studio.delete(notebook_id, mind_map_id))

    async def get_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> dict[str, Any] | None:
        """Return a note JSON tree or an interactively fetched Studio tree."""
        if kind is None:
            note_map = next(
                (
                    item
                    for item in await self.list_note_backed(notebook_id)
                    if item.id == mind_map_id
                ),
                None,
            )
            if note_map is not None:
                return note_map.tree
            if await self._backend_call(self._studio.get_or_none(notebook_id, mind_map_id)) is None:
                return None
        elif kind == MindMapKind.NOTE_BACKED:
            note_record = await self._backend_call(
                self._notes.get_mind_map_or_none(notebook_id, mind_map_id)
            )
            # ``_parse_tree`` is the same soft-invalid parse ``project_mind_map``
            # applies, without constructing a public model to read one field.
            return None if note_record is None else _parse_tree(note_record.tree_json)
        return await self._backend_call(self._studio.get_tree(notebook_id, mind_map_id))

    async def _detect_kind(self, notebook_id: str, mind_map_id: str) -> MindMapKind:
        if any(item.id == mind_map_id for item in await self.list_note_backed(notebook_id)):
            return MindMapKind.NOTE_BACKED
        if await self._backend_call(self._studio.get_or_none(notebook_id, mind_map_id)) is not None:
            return MindMapKind.INTERACTIVE
        raise MindMapNotFoundError(mind_map_id)


__all__ = ["MindMapsAPI", "extract_interactive_tree_leaf"]
