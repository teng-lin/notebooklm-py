"""Wire-shape projection helpers for the Studio ``generate`` / ``rename`` tools.

Split out of ``studio.py`` to keep that module under the ADR-0008 module-size
budget (#1914 — ``studio.py`` sat at the 1000-line ceiling after #1924/#1914 both
grew it). These are cohesive, pure projection functions — a typed
``GenerationExecutionResult`` / ``ArtifactRenameResult`` (plus the mind-map
sub-extractors) projected onto the tool's response dict — with NO ``click`` /
``rich`` / ``cli`` imports, matching the sibling ``_studio_items`` /
``_studio_download`` split.
"""

from __future__ import annotations

from typing import Any

from ..._app import artifacts as artifact_core
from ..._app import generate as generate_core
from ..._app.serialize import to_jsonable
from ..._types.mind_maps import MindMap
from ..._types.research import MindMapResult

__all__ = [
    "_artifact_rename_payload",
    "_generation_payload",
    "_mind_map_id",
    "_mind_map_tree",
]


def _artifact_rename_payload(
    notebook_id: str, result: artifact_core.ArtifactRenameResult, item_type: str
) -> dict[str, Any]:
    """Project an :class:`ArtifactRenameResult` onto the ``studio_rename`` wire shape.

    Shared by the two artifact-rename branches (the full-UUID carve-out and the
    resolved-artifact path), which differ only in the ``type`` label they surface —
    the carve-out can't know the type from a list it wasn't in, the resolved path
    carries ``resolved.type``.
    """
    return {
        "status": "renamed",
        "notebook_id": notebook_id,
        "item_id": result.artifact_id,
        "type": item_type,
        "new_title": result.new_title,
        "is_mind_map": result.is_mind_map,
    }


def _mind_map_tree(result_obj: MindMap | MindMapResult | None) -> dict[str, Any] | None:
    """Extract the bare ``{"name", "children"}`` node tree from a mind-map result.

    ``execute_generation`` puts a different object under ``.mind_map`` per ``map_kind``
    (type-coupling kept contained here): interactive → a ``MindMap`` (tree at ``.tree``,
    populated because the generate path polls to completion + fetches it); note-backed →
    a ``MindMapResult`` (tree at ``.mind_map``). Returns the tree dict (root readable as
    ``mind_map["name"]``), or ``None`` when absent — both attributes are typed ``Any``,
    so a non-dict value is coerced to ``None``, keeping the tree-or-``null`` contract safe.
    """
    tree: Any = None
    if isinstance(result_obj, MindMap):
        tree = result_obj.tree
    elif isinstance(result_obj, MindMapResult):
        tree = result_obj.mind_map
    return tree if isinstance(tree, dict) else None


def _mind_map_id(result_obj: MindMap | MindMapResult | None) -> str | None:
    """Return the map's id (``MindMap.id`` / ``MindMapResult.note_id``), or ``None``.

    Mind-map generation returns no ``task_id``, so this is the only handle to the
    created map (rename / delete / download) — surfaced as ``mind_map_id``."""
    if isinstance(result_obj, MindMap):
        return result_obj.id or None
    if isinstance(result_obj, MindMapResult):
        return result_obj.note_id
    return None


def _generation_payload(
    notebook_id: str, result: generate_core.GenerationExecutionResult
) -> dict[str, Any]:
    """Project a :class:`GenerationExecutionResult` onto the wire shape.

    Surfaces the ``task_id`` an agent polls with ``studio_status`` plus the
    generation outcome (status / url / error) or, for mind maps, the rendered
    map. Mind-map generation renders synchronously (no ``task_id`` to poll), so
    its payload carries the rendered map inline under ``mind_map`` and omits the
    poll fields — documented on ``studio_generate`` (#1908).
    """
    payload: dict[str, Any] = {
        "notebook_id": notebook_id,
        "kind": result.kind,
    }
    if result.kind == "mind-map":
        # Mind-map renders synchronously — no pollable ``task_id``. Branch on the
        # KIND (not a populated map) so every mind-map — interactive AND note-backed,
        # incl. an empty result — takes this path and never falls through to the
        # poll-shape below. ``_mind_map_tree`` normalizes the two result shapes to the
        # bare tree (``null`` when absent); ``mind_map_id`` preserves the map's handle
        # (no task_id to reference it by) so it stays rename/delete/download-able (#1914).
        payload["mind_map"] = to_jsonable(_mind_map_tree(result.mind_map))
        payload["mind_map_id"] = _mind_map_id(result.mind_map)
        return payload
    outcome = result.generation
    if outcome is not None:
        payload.update(
            {
                "task_id": outcome.task_id,
                "status": outcome.status,
                "url": outcome.url,
                "error": outcome.error,
            }
        )
    return payload
