"""Transport-neutral notebook business logic.

This is the Click-free core of ``cli/notebook_cmd.py``: it owns the
``create`` / ``delete`` / ``rename`` / ``describe`` (summary) / ``metadata``
workflows and returns typed result dataclasses instead of an adapter-shaped
envelope dict. Every transport adapter (the Click CLI today, the FastMCP
server / future HTTP later) drives this core and renders the typed result into
its own surface + exit-code policy.

Two boundary-imposed seams are worth calling out:

* **The partial-notebook-id resolver is injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` reaches into ``rich`` consoles for its
  "Matched: ..." diagnostic, so this module cannot import it without breaking
  the ``_app`` boundary. Instead the executors take a ``resolve_notebook_id``
  callable (the CLI wrapper passes its own). Reading the resolver off the
  wrapper at call time also preserves the historical ``monkeypatch`` seam.
* **The summary/metadata *serializers* stay in the CLI.** This core only
  fetches/computes the typed ``NotebookDescription`` / ``NotebookMetadata``
  payloads; the text rendering + ``--json`` envelope build live in the command
  layer (the survey: "the serializer STAYS in CLI; the fetch/compute moves").

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import Notebook, NotebookDescription, NotebookMetadata

logger = logging.getLogger(__name__)

#: Resolves a (possibly partial) notebook id to its full id. The CLI adapter
#: injects ``cli.resolve.resolve_notebook_id``; it is read off the wrapper at
#: call time so the ``monkeypatch`` test seam keeps landing.
ResolveNotebookIdFn = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# notebook create
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookCreateResult:
    """Outcome of ``notebook create``."""

    notebook: Notebook


async def execute_notebook_create(
    client: NotebookLMClient,
    title: str,
) -> NotebookCreateResult:
    """Create a new notebook.

    The ``--use`` context switch is a CLI-side side effect (it writes the
    persisted active-notebook pointer), so it stays in the command layer; this
    core only creates the notebook and returns the typed result.

    The returned notebook has its ``created_at`` / ``modified_at`` backfilled
    best-effort (see :func:`_backfill_create_timestamps`) so every adapter
    driving this core — CLI ``notebook create --json``, the REST create route,
    and the MCP ``notebook_create`` tool — surfaces populated timestamps on
    creation rather than ``null`` (#1705, lifting the MCP-only fix from #1699).
    """
    notebook = await client.notebooks.create(title)
    await _backfill_create_timestamps(client, notebook)
    return NotebookCreateResult(notebook=notebook)


async def _backfill_create_timestamps(
    client: NotebookLMClient,
    notebook: Notebook,
) -> None:
    """Best-effort: fill ``notebook``'s null ``created_at`` / ``modified_at``.

    ``CREATE_NOTEBOOK`` (``CCqFvf``) returns a notebook whose ``meta[5]`` /
    ``meta[8]`` timestamp slots are not yet populated, even though
    ``GET_NOTEBOOK`` / ``notebook_list`` carry them (#1699). Do ONE re-read to
    backfill just those two keys, skipping it when both are already present (no
    wasted RPC) or the id is empty (no ``get("")``).

    The fill is PER-KEY and strictly additive: a slot is filled only when the
    create left it ``None`` AND the re-read has a value, so a populated create
    timestamp is never touched and a lagging re-read that returns ``None``
    cannot REGRESS a populated slot back to ``None``. The create already
    committed server-side, so a re-read failure (eventual-consistency
    ``NotebookNotFoundError``, a transport blip) degrades to the create
    timestamps rather than failing the create; ``except Exception`` still lets
    ``asyncio.CancelledError`` (a ``BaseException``) propagate.

    Mutates ``notebook`` in place — it is the freshly created, unaliased object
    this core is about to return, so an in-place fill is safe.
    """
    if not notebook.id:
        return
    if notebook.created_at is not None and notebook.modified_at is not None:
        return
    try:
        fresh = await client.notebooks.get(notebook.id)
    except Exception:
        logger.debug(
            "notebook create: timestamp re-read failed; returning create "
            "result with unpopulated timestamps",
            exc_info=True,
        )
        return
    if notebook.created_at is None and fresh.created_at is not None:
        notebook.created_at = fresh.created_at
    if notebook.modified_at is None and fresh.modified_at is not None:
        notebook.modified_at = fresh.modified_at


# ---------------------------------------------------------------------------
# notebook delete
# ---------------------------------------------------------------------------


async def execute_notebook_delete(
    client: NotebookLMClient,
    notebook_id: str,
) -> None:
    """Delete a notebook by its full id.

    ``delete()`` now returns ``None`` and raises on real failure (issue #1211);
    reaching here without an exception means success.
    """
    await client.notebooks.delete(notebook_id)


# ---------------------------------------------------------------------------
# notebook rename
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookRenameResult:
    """Outcome of ``notebook rename``."""

    notebook_id: str
    new_title: str


async def execute_notebook_rename(
    client: NotebookLMClient,
    notebook_id: str,
    new_title: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> NotebookRenameResult:
    """Resolve + rename a notebook.

    ``resolve_notebook_id`` is injected so this core stays free of the
    ``rich``-coupled resolver and the CLI's ``monkeypatch`` seam keeps landing.
    """
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    await client.notebooks.rename(resolved_id, new_title)
    return NotebookRenameResult(notebook_id=resolved_id, new_title=new_title)


# ---------------------------------------------------------------------------
# notebook summary (describe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookDescribeResult:
    """Outcome of ``notebook summary``.

    Carries the resolved id + the typed :class:`~notebooklm.types.NotebookDescription`
    (or ``None``); the CLI renders both the text and ``--json`` views from it.
    """

    notebook_id: str
    description: NotebookDescription | None


async def execute_notebook_describe(
    client: NotebookLMClient,
    notebook_id: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> NotebookDescribeResult:
    """Resolve + fetch a notebook's AI-generated description."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    description = await client.notebooks.get_description(resolved_id)
    return NotebookDescribeResult(notebook_id=resolved_id, description=description)


# ---------------------------------------------------------------------------
# notebook metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookMetadataResult:
    """Outcome of ``notebook metadata``.

    Carries the resolved id + the typed :class:`~notebooklm.types.NotebookMetadata`;
    the CLI renders the text and ``--json`` (``metadata.to_dict()``) views.
    """

    notebook_id: str
    metadata: NotebookMetadata


async def execute_notebook_metadata(
    client: NotebookLMClient,
    notebook_id: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> NotebookMetadataResult:
    """Resolve + fetch a notebook's metadata (details + sources list)."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    metadata = await client.notebooks.get_metadata(resolved_id)
    return NotebookMetadataResult(notebook_id=resolved_id, metadata=metadata)


__all__ = [
    "NotebookCreateResult",
    "NotebookDescribeResult",
    "NotebookMetadataResult",
    "NotebookRenameResult",
    "ResolveNotebookIdFn",
    "execute_notebook_create",
    "execute_notebook_delete",
    "execute_notebook_describe",
    "execute_notebook_metadata",
    "execute_notebook_rename",
]
