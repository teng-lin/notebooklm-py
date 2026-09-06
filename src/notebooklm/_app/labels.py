"""Transport-neutral source-label business logic.

This is the Click-free core of ``cli/label_cmd.py`` + the resolution half of
``cli/services/label_listing.py``: it owns the
``create`` / ``sources`` / ``generate`` / ``rename`` / ``emoji`` / ``add`` /
``remove`` / ``delete`` workflows and the composite ``<id|name>``
:func:`resolve_label_id` resolver (id / unambiguous-prefix first, then exact
name), returning typed results / errors instead of an adapter-shaped envelope.

The members→titles JOIN render (``execute_label_list`` + ``LabelListPlan``)
stays in the CLI service ``cli/services/label_listing.py`` because it depends on
the CLI ``listing`` presentation pipeline (``ListRender`` / ``prepare_list``);
only the resolution + mutation cores move here.

Two boundary-imposed seams are worth calling out:

* **The partial-id resolvers are injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` / ``resolve_source_ids`` reach into
  ``rich`` consoles, so the executors take them as callables (the CLI wrapper
  passes its own).
* **``resolve_label_id`` is self-contained** (it does NOT route through the
  CLI's ``resolve_partial_id_in_items``, which emits a ``rich`` "Matched: ..."
  diagnostic). It reproduces the label-specific rules — full-id passthrough
  disabled, ambiguous-prefix detection BEFORE the name fallback, candidate
  listing with emoji + source count — and raises the typed
  :class:`LabelResolutionError` the command layer maps through the ADR-0015
  error contract.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ..exceptions import ValidationError
from ..types import Label
from .resolve import near_miss_candidates, validate_id

if TYPE_CHECKING:
    from ..client import NotebookLMClient

#: Resolves a (possibly partial) notebook id to its full id (CLI injects
#: ``cli.resolve.resolve_notebook_id``).
ResolveNotebookIdFn = Callable[..., Awaitable[str]]

#: Resolves multiple (possibly partial) source ids to full ids (CLI injects
#: ``cli.resolve.resolve_source_ids``); returns ``None`` when none were given.
ResolveSourceIdsFn = Callable[..., Awaitable[list[str] | None]]


LabelResolutionReason = Literal["ambiguous_id", "ambiguous_name", "not_found"]


@dataclass(frozen=True)
class LabelResolutionMatch:
    """Label candidate carried by a semantic ambiguity."""

    id: str
    emoji: str | None
    source_count: int


class LabelResolutionError(ValidationError):
    """Typed label-resolution failure with adapter-neutral reason data."""

    def __init__(
        self,
        reason: LabelResolutionReason,
        *,
        notebook_id: str,
        token: str,
        matches: tuple[LabelResolutionMatch, ...] = (),
        candidates: Sequence[dict[str, str]] = (),
    ) -> None:
        self.reason = reason
        self.notebook_id = notebook_id
        self.token = token
        self.matches = matches
        self.candidates = candidates
        super().__init__(f"label resolution {reason.replace('_', ' ')}: {token}")


# ---------------------------------------------------------------------------
# resolve_label_id — the composite <id|name> resolver
# ---------------------------------------------------------------------------


def _resolution_matches(matches: Sequence[Label]) -> tuple[LabelResolutionMatch, ...]:
    return tuple(
        LabelResolutionMatch(label.id, label.emoji, len(label.source_ids)) for label in matches
    )


async def resolve_label_id(
    client: NotebookLMClient,
    notebook_id: str,
    token: str,
) -> str:
    """Resolve a label ``<id|name>`` token to a full label id.

    Resolution order: id / unambiguous-prefix first (full-id passthrough
    **disabled** so a UUID-shaped *name* is not blindly accepted as an id),
    then an explicit exact-name match. An ambiguous *prefix* (>1 id match)
    raises with code ``AMBIGUOUS_ID`` BEFORE the name fallback; an ambiguous
    *name* (>1 match) raises with code ``AMBIGUOUS_NAME``. Both
    :class:`LabelResolutionError` paths list each candidate's id, emoji, and
    source count. A short-but-complete id wins over a longer-id prefix (exact
    match precedence) so it is not reported ambiguous.
    """
    token = validate_id(token, "label")
    labels = await client.labels.list(notebook_id)

    # Pass 1: id / unambiguous-prefix (full-id passthrough disabled). Exact id
    # wins over prefix so a short-but-complete id is not reported ambiguous when
    # it is also a prefix of another label.
    token_lower = token.lower()
    for label in labels:
        if label.id.lower() == token_lower:
            return label.id
    prefix_matches = [label for label in labels if label.id.lower().startswith(token_lower)]
    if len(prefix_matches) == 1:
        return prefix_matches[0].id
    if len(prefix_matches) > 1:
        # A >1 prefix-match count here is a true ambiguity (exact-id matches
        # already returned above), so surface the candidate list rather than
        # silently falling through to the name pass and reporting NOT_FOUND.
        raise LabelResolutionError(
            "ambiguous_id",
            notebook_id=notebook_id,
            token=token,
            matches=_resolution_matches(prefix_matches),
        )

    # Pass 2: explicit exact-name match (no id / prefix match found above).
    name_matches = [label for label in labels if label.name == token]
    if len(name_matches) == 1:
        return name_matches[0].id
    if len(name_matches) > 1:
        raise LabelResolutionError(
            "ambiguous_name",
            notebook_id=notebook_id,
            token=token,
            matches=_resolution_matches(name_matches),
        )

    # Near-miss "did you mean" candidates (issue #1787): a name mistyped with a
    # hyphen for an em-dash or a bare prefix surfaces the real label(s). The
    # semantic ``.candidates`` are projected independently by CLI and MCP/REST.
    candidates = near_miss_candidates(
        token,
        labels,
        id_of=lambda label: label.id,
        title_of=lambda label: label.name,
    )
    raise LabelResolutionError(
        "not_found",
        notebook_id=notebook_id,
        token=token,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# label sources
# ---------------------------------------------------------------------------


async def execute_label_sources(
    client: NotebookLMClient,
    notebook_id: str,
    label_id: str,
):
    """Expand a label to its source objects (the ``label sources`` body)."""
    return await client.labels.sources(notebook_id, label_id)


# ---------------------------------------------------------------------------
# label generate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelGenerateResult:
    """Outcome of ``label generate``."""

    notebook_id: str
    scope: str
    labels: list[Label]


async def execute_label_generate(
    client: NotebookLMClient,
    notebook_id: str,
    scope: str,
) -> LabelGenerateResult:
    """AI-group sources into labels (the UI's "Reorganize").

    The ``--scope all`` destructive-confirm gate is a CLI presentation concern
    (it prompts / refuses in ``--json`` mode), so it stays in the command layer;
    this core only runs the generation once the gate has passed. ``scope`` is a
    ``str`` here (the adapter's lowercased Click ``Choice`` value) cast to the
    ``generate`` Literal — the Click ``Choice`` constrains it to a valid value.
    """
    labels = await client.labels.generate(
        notebook_id, scope=cast("Literal['all', 'unlabeled']", scope)
    )
    return LabelGenerateResult(notebook_id=notebook_id, scope=scope, labels=labels)


# ---------------------------------------------------------------------------
# label create
# ---------------------------------------------------------------------------


async def execute_label_create(
    client: NotebookLMClient,
    notebook_id: str,
    name: str,
    emoji: str,
) -> Label:
    """Create an empty, manually-named label."""
    return await client.labels.create(notebook_id, name, emoji)


# ---------------------------------------------------------------------------
# label rename / emoji
# ---------------------------------------------------------------------------


async def execute_label_rename(
    client: NotebookLMClient,
    notebook_id: str,
    label_id: str,
    new_name: str,
) -> Label:
    """Rename a label (preserves its emoji).

    ``return_object`` defaults to True, so the mutation returns a ``Label`` (or
    raises ``LabelNotFoundError``) — never ``None`` here.
    """
    return cast(Label, await client.labels.rename(notebook_id, label_id, new_name))


async def execute_label_set_emoji(
    client: NotebookLMClient,
    notebook_id: str,
    label_id: str,
    emoji_value: str,
) -> Label:
    """Set a label's emoji."""
    return cast(Label, await client.labels.set_emoji(notebook_id, label_id, emoji_value))


# ---------------------------------------------------------------------------
# label add / remove sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelMembershipResult:
    """Outcome of ``label add`` / ``label remove``."""

    label: Label
    source_ids: list[str]


async def execute_label_add_sources(
    client: NotebookLMClient,
    notebook_id: str,
    label_id: str,
    source_ids: Sequence[str],
) -> LabelMembershipResult:
    """Add source(s) to a label (append; existing members preserved)."""
    ids = list(source_ids)
    label = cast(Label, await client.labels.add_sources(notebook_id, label_id, ids))
    return LabelMembershipResult(label=label, source_ids=ids)


async def execute_label_remove_sources(
    client: NotebookLMClient,
    notebook_id: str,
    label_id: str,
    source_ids: Sequence[str],
) -> LabelMembershipResult:
    """Un-assign source(s) from a label (the inverse of ``add``)."""
    ids = list(source_ids)
    label = cast(Label, await client.labels.remove_sources(notebook_id, label_id, ids))
    return LabelMembershipResult(label=label, source_ids=ids)


# ---------------------------------------------------------------------------
# label delete
# ---------------------------------------------------------------------------


async def execute_label_delete(
    client: NotebookLMClient,
    notebook_id: str,
    label_ids: Sequence[str],
) -> None:
    """Delete one or more labels (the label only, not its sources)."""
    await client.labels.delete(notebook_id, list(label_ids))


__all__ = [
    "LabelGenerateResult",
    "LabelMembershipResult",
    "LabelResolutionError",
    "LabelResolutionMatch",
    "LabelResolutionReason",
    "ResolveNotebookIdFn",
    "ResolveSourceIdsFn",
    "execute_label_add_sources",
    "execute_label_create",
    "execute_label_delete",
    "execute_label_generate",
    "execute_label_remove_sources",
    "execute_label_rename",
    "execute_label_set_emoji",
    "execute_label_sources",
    "resolve_label_id",
]
