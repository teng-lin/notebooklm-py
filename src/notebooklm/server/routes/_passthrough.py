"""Pass-through resolvers shared by the REST route adapters.

The transport-neutral ``_app`` executors take injected ``resolve_notebook_id`` /
``resolve_source_id`` / source-list / download resolver callables shaped for the
CLI (which turns a human ``<id|name>`` reference into a canonical id). The REST
server works in full ids end to end, so it hands the executors trivial resolvers
that return the already-resolved id(s) unchanged.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = [
    "passthrough_artifact_id",
    "passthrough_download_notebook",
    "passthrough_notebook_id",
    "passthrough_source_ids",
]


async def passthrough_notebook_id(
    _client: NotebookLMClient, notebook_id: str, *, json_output: bool = False
) -> str:
    """Return ``notebook_id`` unchanged (the REST adapter works in full ids)."""
    return notebook_id


async def passthrough_source_ids(
    _client: NotebookLMClient,
    _notebook_id: str,
    source_ids: Any,
    *,
    json_output: bool = False,
) -> Any:
    """Return the full source ids, or ``None`` when none were supplied.

    The REST adapter already works in full ids, so resolution is a pass-through —
    except for the empty case, which mirrors ``cli.resolve.resolve_source_ids``:
    no selection resolves to ``None``, not an empty tuple. The client treats
    ``None`` as "scope to all sources"; an empty sequence as "no sources", which
    the API rejects for quiz/flashcards (``… generation is unavailable``). So a
    bare ``POST .../artifacts`` (no ``source_ids``) generates over all sources,
    matching the CLI's no-``--source`` behavior.
    """
    return source_ids or None


async def passthrough_download_notebook(notebook_id: str) -> str:
    """Async pass-through notebook resolver for the download core."""
    return notebook_id


def passthrough_artifact_id(_artifacts: list[Any], artifact_id: str) -> str:
    """Artifact-id resolver for the download core (REST passes a full id)."""
    return artifact_id
