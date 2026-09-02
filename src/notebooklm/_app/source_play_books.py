"""Transport-neutral Google Play Books ("Expert Intelligence") source ops (#2292).

The Click-free / fastmcp-free core shared by the CLI (``source books`` /
``source add-book``) and the MCP tools. Listing and adding both delegate to the
public ``client.sources`` facade, so backend-specific Web/Android protocol
details stay out of this module.

Transport-neutral — no ``click`` / ``rich`` / ``cli`` / ``fastmcp`` imports
(enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..types import PlayBook, Source

if TYPE_CHECKING:
    from ..client import NotebookLMClient


async def fetch_play_books(client: NotebookLMClient) -> list[PlayBook]:
    """Return the account's Play Books library eligible to be added as sources."""
    return await client.sources.list_play_books()


@dataclass(frozen=True)
class SourceAddPlayBookPlan:
    """Prepared inputs for :func:`execute_source_add_play_book`."""

    notebook_id: str
    content_id: str
    wait: bool = False
    wait_timeout: float = 120.0


@dataclass(frozen=True)
class SourceAddPlayBookResult:
    """Outcome of ``source add-book``.

    Typed-fields-only: the ``--json`` envelope is assembled by the surface
    adapter (CLI renderer / MCP tool) from these fields. ``content_id`` is the
    Play Books volume id the caller passed, echoed for provenance.
    """

    source: Source
    notebook_id: str
    content_id: str


async def execute_source_add_play_book(
    client: NotebookLMClient,
    plan: SourceAddPlayBookPlan,
) -> SourceAddPlayBookResult:
    """Add a Google Play Book as a source.

    Thin executor over the public client (boundary-legal): library lookup,
    export-eligibility refusal
    (:class:`~notebooklm.exceptions.PlayBookNotExportableError`), spec build and
    ingest all live in ``SourcesAPI.add_play_book`` on both supported backends.
    """
    src = await client.sources.add_play_book(
        plan.notebook_id,
        plan.content_id,
        wait=plan.wait,
        wait_timeout=plan.wait_timeout,
    )
    return SourceAddPlayBookResult(
        source=src,
        notebook_id=plan.notebook_id,
        content_id=plan.content_id,
    )


__all__ = [
    "SourceAddPlayBookPlan",
    "SourceAddPlayBookResult",
    "execute_source_add_play_book",
    "fetch_play_books",
]
