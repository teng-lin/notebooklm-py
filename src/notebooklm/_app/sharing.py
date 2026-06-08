"""Transport-neutral sharing business logic.

This is the Click-free core of ``cli/share_cmd.py``: it owns the
``status`` / ``public`` / ``view-level`` / ``add`` / ``update`` / ``remove``
workflows — each one resolving the notebook id and driving the
``client.sharing`` RPC family — and returns the typed
:class:`~notebooklm.types.ShareStatus` (or the resolved ids) the command layer
renders. Every transport adapter (the Click CLI today, the FastMCP server /
future HTTP later) drives this core and renders into its own surface.

Two boundary-imposed shapes are worth calling out:

* **The partial-notebook-id resolver is injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` reaches into ``rich`` consoles, so the
  executors take it as a callable (the CLI wrapper passes its own).
* **The permission/view-level *display* serializers stay in the CLI.** The
  ``str -> SharePermission`` / ``str -> ShareViewLevel`` parsing also stays at
  the command layer (it is Click-``Choice``-bound input parsing); this core
  takes the already-parsed enums.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import SharePermission, ShareStatus, ShareViewLevel

#: Resolves a (possibly partial) notebook id to its full id (CLI injects
#: ``cli.resolve.resolve_notebook_id``; read at call time for the seam).
ResolveNotebookIdFn = Callable[..., Awaitable[str]]


async def execute_share_status(
    client: NotebookLMClient,
    notebook_id: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> ShareStatus:
    """Resolve the notebook + fetch its sharing status."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    return await client.sharing.get_status(resolved_id)


async def execute_share_set_public(
    client: NotebookLMClient,
    notebook_id: str,
    enable: bool,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> ShareStatus:
    """Resolve the notebook + enable/disable public link sharing."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    return await client.sharing.set_public(resolved_id, enable)


async def execute_share_set_view_level(
    client: NotebookLMClient,
    notebook_id: str,
    view_level: ShareViewLevel,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> tuple[str, ShareStatus]:
    """Resolve the notebook + set what viewers can access.

    Returns ``(resolved_notebook_id, status)`` — the command layer's ``--json``
    envelope keys ``notebook_id`` off the resolved id (not ``status``), so it is
    surfaced alongside the returned status.
    """
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    status = await client.sharing.set_view_level(resolved_id, view_level)
    return resolved_id, status


async def execute_share_add_user(
    client: NotebookLMClient,
    notebook_id: str,
    email: str,
    *,
    permission: SharePermission,
    notify: bool,
    welcome_message: str,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> str:
    """Resolve the notebook + share it with a user. Returns the resolved id."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    await client.sharing.add_user(
        resolved_id,
        email,
        permission=permission,
        notify=notify,
        welcome_message=welcome_message,
    )
    return resolved_id


async def execute_share_update_user(
    client: NotebookLMClient,
    notebook_id: str,
    email: str,
    permission: SharePermission,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> str:
    """Resolve the notebook + update a user's permission. Returns the resolved id."""
    resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    await client.sharing.update_user(resolved_id, email, permission)
    return resolved_id


async def execute_share_remove_user(
    client: NotebookLMClient,
    notebook_id: str,
    email: str,
) -> None:
    """Remove a user's access to the notebook (raises on real failure)."""
    await client.sharing.remove_user(notebook_id, email)


__all__ = [
    "ResolveNotebookIdFn",
    "execute_share_add_user",
    "execute_share_remove_user",
    "execute_share_set_public",
    "execute_share_set_view_level",
    "execute_share_status",
    "execute_share_update_user",
]
