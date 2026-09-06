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
from typing import TYPE_CHECKING, Literal, cast

from ..exceptions import ValidationError
from ..types import Collection
from .resolve import near_miss_candidates, validate_id

if TYPE_CHECKING:
    from ..client import NotebookLMClient


CollectionResolutionReason = Literal["ambiguous_id", "ambiguous_name", "not_found"]


@dataclass(frozen=True)
class CollectionResolutionMatch:
    """Collection candidate carried by a semantic ambiguity."""

    id: str
    emoji: str | None
    notebook_count: int


class CollectionResolutionError(ValidationError):
    """Typed collection-resolution failure with adapter-neutral reason data."""

    def __init__(
        self,
        reason: CollectionResolutionReason,
        *,
        token: str,
        matches: tuple[CollectionResolutionMatch, ...] = (),
        candidates: Sequence[dict[str, str]] = (),
    ) -> None:
        self.reason = reason
        self.token = token
        self.matches = matches
        self.candidates = candidates
        super().__init__(f"collection resolution {reason.replace('_', ' ')}: {token}")


# ---------------------------------------------------------------------------
# resolve_collection_id — the composite <id|name> resolver
# ---------------------------------------------------------------------------


def _resolution_matches(
    matches: Sequence[Collection],
) -> tuple[CollectionResolutionMatch, ...]:
    return tuple(
        CollectionResolutionMatch(
            collection.id,
            collection.emoji,
            len(collection.notebook_ids),
        )
        for collection in matches
    )


async def resolve_collection_id(
    client: NotebookLMClient,
    token: str,
    *,
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
            "ambiguous_id",
            token=token,
            matches=_resolution_matches(prefix_matches),
        )

    # Pass 2: explicit exact-name match.
    name_matches = [c for c in collections if c.name == token]
    if len(name_matches) == 1:
        return name_matches[0].id
    if len(name_matches) > 1:
        raise CollectionResolutionError(
            "ambiguous_name",
            token=token,
            matches=_resolution_matches(name_matches),
        )

    # Near-miss "did you mean" candidates (issue #1787 parity).
    candidates = near_miss_candidates(
        token,
        collections,
        id_of=lambda collection: collection.id,
        title_of=lambda collection: collection.name,
    )
    raise CollectionResolutionError("not_found", token=token, candidates=candidates)


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
    "CollectionResolutionMatch",
    "CollectionResolutionReason",
    "execute_collection_add_notebooks",
    "execute_collection_create",
    "execute_collection_delete",
    "execute_collection_list",
    "execute_collection_notebooks",
    "execute_collection_remove_notebooks",
    "execute_collection_rename",
    "resolve_collection_id",
]
