"""Transport-neutral collection business logic.

The Click-free core of ``cli/collection_cmd.py``: it owns the
``create`` / ``notebooks`` / ``rename`` / ``add`` / ``remove`` / ``delete``
workflows and the composite ``<id|name>`` :func:`resolve_collection_id` resolver
(id / unambiguous-prefix first, then exact name), returning typed results /
errors instead of an adapter-shaped envelope.

Collections are **account-level** (no notebook scope), so — unlike
``_app/labels.py`` — the resolver and executors take no ``notebook_id``. This
module is transport-neutral: no ``click`` / ``rich`` / ``cli`` / ``fastmcp``
imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..exceptions import ValidationError
from ..types import Collection
from .resolve import near_miss_candidates, validate_id

if TYPE_CHECKING:
    from ..client import NotebookLMClient


class CollectionResolutionError(ValidationError):
    """Typed collection-resolution error for command-layer rendering and exit policy.

    Mirrors :class:`~notebooklm._app.labels.LabelResolutionError`: subclasses
    :class:`~notebooklm.exceptions.ValidationError` so ``_app.errors.classify``
    covers it uniformly, carries a human ``message``, an ADR-0015 ``code``
    (``NOT_FOUND`` / ``AMBIGUOUS_ID`` / ``AMBIGUOUS_NAME`` / ``VALIDATION_ERROR``),
    and an optional ``extra`` payload. The command layer catches it explicitly
    and maps it through ``output_error`` so the typed ``--json`` envelope + its
    ADR-0015 code are preserved.
    """

    #: Near-miss ``{"id", "title"}`` candidates for a failed name lookup
    #: (mirrors :attr:`LabelResolutionError.candidates`).
    candidates: Sequence[dict[str, str]] = ()

    def __init__(
        self,
        message: str,
        code: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.extra = extra
        super().__init__(f"{message} (code={code})")


# ---------------------------------------------------------------------------
# resolve_collection_id — the composite <id|name> resolver
# ---------------------------------------------------------------------------


def _candidate_payload(matches: Sequence[Collection]) -> list[dict[str, Any]]:
    """Build the structured candidate list (id + emoji + notebook count)."""
    return [
        {
            "id": collection.id,
            "emoji": collection.emoji,
            "notebook_count": len(collection.notebook_ids),
        }
        for collection in matches
    ]


def _append_candidate_lines(lines: list[str], matches: Sequence[Collection]) -> None:
    """Append the per-candidate ``id emoji (N notebooks)`` lines (capped at 5)."""
    for collection in matches[:5]:
        emoji = f"{collection.emoji} " if collection.emoji else ""
        count = len(collection.notebook_ids)
        lines.append(f"  {collection.id} {emoji}({count} notebook{'s' if count != 1 else ''})")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")


def _ambiguous_id_message(partial_id: str, matches: Sequence[Collection]) -> str:
    """Build the ambiguous-prefix error listing each candidate."""
    lines = [f"Ambiguous collection id '{partial_id}' matches {len(matches)} collections:"]
    _append_candidate_lines(lines, matches)
    lines.append("Specify more characters to disambiguate.")
    return "\n".join(lines)


def _ambiguous_name_message(name: str, matches: Sequence[Collection]) -> str:
    """Build the ambiguous-name error listing each candidate."""
    lines = [f"Name '{name}' matches {len(matches)} collections. Use a collection id instead:"]
    _append_candidate_lines(lines, matches)
    lines.append("Specify the collection id to disambiguate.")
    return "\n".join(lines)


async def resolve_collection_id(
    client: NotebookLMClient,
    token: str,
    *,
    json_output: bool = False,
    collections: Sequence[Collection] | None = None,
) -> str:
    """Resolve a collection ``<id|name>`` token to a full collection id.

    Resolution order: id / unambiguous-prefix first (full-id passthrough
    **disabled** so a UUID-shaped *name* is not blindly accepted as an id), then
    an explicit exact-name match. An ambiguous *prefix* (>1 id match) raises with
    code ``AMBIGUOUS_ID`` BEFORE the name fallback; an ambiguous *name* (>1 match)
    raises ``AMBIGUOUS_NAME``. Mirrors :func:`resolve_label_id` but account-level
    (no notebook scope).

    Pass a pre-fetched ``collections`` snapshot to resolve multiple refs against
    one shared ``list()`` call (mirrors ``cli/collection_cmd.py``'s
    ``_resolve_notebook_ids``) instead of the default one-``list()``-per-call
    behaviour, used by every single-ref call site.
    """
    token = validate_id(token, "collection")
    if collections is None:
        collections = await client.collections.list()

    # Pass 1: id / unambiguous-prefix (full-id passthrough disabled). Exact id
    # wins over prefix so a short-but-complete id is not reported ambiguous.
    token_lower = token.lower()
    for collection in collections:
        if collection.id.lower() == token_lower:
            return collection.id
    prefix_matches = [c for c in collections if c.id.lower().startswith(token_lower)]
    if len(prefix_matches) == 1:
        return prefix_matches[0].id
    if len(prefix_matches) > 1:
        raise CollectionResolutionError(
            _ambiguous_id_message(token, prefix_matches),
            "AMBIGUOUS_ID",
            {"id": token, "candidates": _candidate_payload(prefix_matches)},
        )

    # Pass 2: explicit exact-name match.
    name_matches = [c for c in collections if c.name == token]
    if len(name_matches) == 1:
        return name_matches[0].id
    if len(name_matches) > 1:
        raise CollectionResolutionError(
            _ambiguous_name_message(token, name_matches),
            "AMBIGUOUS_NAME",
            {"name": token, "candidates": _candidate_payload(name_matches)},
        )

    # Near-miss "did you mean" candidates (issue #1787 parity).
    extra: dict[str, Any] = {"id": token}
    candidates = near_miss_candidates(
        token,
        collections,
        id_of=lambda collection: collection.id,
        title_of=lambda collection: collection.name,
    )
    if candidates:
        extra["candidates"] = candidates
    error = CollectionResolutionError(
        f"No collection found matching '{token}'. "
        f"Run 'notebooklm collection list' to see available collections.",
        "NOT_FOUND",
        extra,
    )
    error.candidates = candidates
    raise error


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------


async def execute_collection_list(client: NotebookLMClient) -> list[Collection]:
    """List all collections in the account."""
    return await client.collections.list()


async def execute_collection_notebooks(client: NotebookLMClient, collection_id: str):
    """Expand a collection to its notebook objects (the ``collection notebooks`` body)."""
    return await client.collections.notebooks(collection_id)


async def execute_collection_create(client: NotebookLMClient, name: str) -> Collection:
    """Create an empty, named collection."""
    return await client.collections.create(name)


async def execute_collection_rename(
    client: NotebookLMClient, collection_id: str, new_name: str
) -> Collection:
    """Rename a collection (preserves its emoji).

    ``return_object`` defaults to True, so the mutation returns a ``Collection``
    (or raises ``CollectionNotFoundError``) — never ``None`` here.
    """
    return cast(Collection, await client.collections.rename(collection_id, new_name))


@dataclass(frozen=True)
class CollectionMembershipResult:
    """Outcome of ``collection add`` / ``collection remove``."""

    collection: Collection
    notebook_ids: list[str]


async def execute_collection_add_notebooks(
    client: NotebookLMClient, collection_id: str, notebook_ids: Sequence[str]
) -> CollectionMembershipResult:
    """Add notebook(s) to a collection (append; existing members preserved)."""
    ids = list(notebook_ids)
    collection = cast(Collection, await client.collections.add_notebooks(collection_id, ids))
    return CollectionMembershipResult(collection=collection, notebook_ids=ids)


async def execute_collection_remove_notebooks(
    client: NotebookLMClient, collection_id: str, notebook_ids: Sequence[str]
) -> CollectionMembershipResult:
    """Un-assign notebook(s) from a collection (the inverse of ``add``)."""
    ids = list(notebook_ids)
    collection = cast(Collection, await client.collections.remove_notebooks(collection_id, ids))
    return CollectionMembershipResult(collection=collection, notebook_ids=ids)


async def execute_collection_delete(
    client: NotebookLMClient, collection_ids: Sequence[str]
) -> None:
    """Delete one or more collections (the collection only, not its notebooks)."""
    await client.collections.delete(list(collection_ids))


__all__ = [
    "CollectionMembershipResult",
    "CollectionResolutionError",
    "execute_collection_add_notebooks",
    "execute_collection_create",
    "execute_collection_delete",
    "execute_collection_list",
    "execute_collection_notebooks",
    "execute_collection_remove_notebooks",
    "execute_collection_rename",
    "resolve_collection_id",
]
