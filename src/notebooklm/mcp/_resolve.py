"""Name / partial-id resolution for MCP tools.

MCP tools accept a human-friendly ``notebook`` / ``source`` reference and turn it
into a canonical backend id. The matching rules build on the neutral
:func:`notebooklm._app.resolve.resolve_ref` (full/partial-UUID fast-path, exact
id, unique prefix, ambiguous-prefix -> :class:`AmbiguousIdError`) and ADD
case-insensitive **title** matching for human references: an exact title, or —
when nothing matches exactly — a unique title *prefix* (#1786), so a name is as
prefix-resolvable as an id and the surface stays consistent across notebook /
source / note / artifact refs.

Routing is by token shape:

* A full canonical UUID is returned verbatim with **no list call** (so a tool
  invoked with a concrete id never pays for a list).
* A hex-ish token (``^[0-9a-fA-F-]+$``) takes the id/prefix path via
  ``resolve_ref`` against the listed items, then **falls back to the title path**
  if the id/prefix path finds nothing — so an item whose title is all-hex
  (``"beef"``, ``"1234"``) is still reachable by name. An *ambiguous* hex prefix
  raises :class:`AmbiguousIdError` and never falls through to title.
* Anything else takes the title path: a case-insensitive **exact** title match
  wins first; failing that, a case-insensitive unique title **prefix** match.
  0 matches raises the public ``*NotFoundError``; an ambiguous exact title or an
  ambiguous prefix (>1) raises :class:`AmbiguousIdError` carrying the colliding
  ids. Exact wins over prefix, so a title that is also a prefix of a longer one
  (``"Report"`` vs ``"Report Q1"``) resolves to itself rather than reporting
  ambiguity — mirroring the id resolver's exact-over-prefix rule.

Sources are resolved within their notebook's source list. The prefix path's
no-match (``ValidationError`` from ``resolve_ref``) is re-raised as the
domain-specific ``*NotFoundError`` so every miss surfaces uniformly as
``NOT_FOUND`` regardless of which path produced it.

This module imports NO ``click`` / ``rich`` / ``cli`` — only the ``_app``
resolve core and the public exception hierarchy.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .._app.resolve import (
    FULL_ID_PATTERN,
    AmbiguousIdError,
    near_miss_candidates,
    resolve_ref,
    validate_id,
)
from ..exceptions import (
    ArtifactNotFoundError,
    NotebookNotFoundError,
    NoteNotFoundError,
    SourceNotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    from ..client import NotebookLMClient

__all__ = [
    "resolve_artifact",
    "resolve_note",
    "resolve_notebook",
    "resolve_source",
    "resolve_sources",
]

#: A token made only of hex digits and dashes routes to the id/prefix path; any
#: other character (a space, a letter outside ``a-f``, punctuation) routes to the
#: title path. Mirrors the plan's ``^[0-9a-fA-F-]+$`` discriminator.
_HEX_ISH = re.compile(r"^[0-9a-fA-F-]+$")

#: Max candidate ids surfaced in an ambiguous-title error message.
_MAX_AMBIGUOUS_CANDIDATES = 5


def _ambiguous_title_error(token: str, matches: Sequence[Any], *, kind: str) -> AmbiguousIdError:
    """Build an :class:`AmbiguousIdError` listing each colliding candidate.

    ``kind`` is the human word for how ``token`` matched — ``"title"`` for an
    exact-title collision, ``"prefix"`` for a title-prefix collision — so the
    message tells the caller which axis was ambiguous.
    """
    candidate_ids = [str(item.id) for item in matches]
    lines = [f"Ambiguous {kind} '{token}' matches {len(matches)} items:"]
    for item in matches[:_MAX_AMBIGUOUS_CANDIDATES]:
        lines.append(f"  {str(item.id)[:12]}... {item.title or '(untitled)'}")
    if len(matches) > _MAX_AMBIGUOUS_CANDIDATES:
        lines.append(f"  ... and {len(matches) - _MAX_AMBIGUOUS_CANDIDATES} more")
    lines.append("\nUse a more specific title or the id.")
    return AmbiguousIdError(token, candidate_ids, "\n".join(lines))


def _resolve_by_title(
    token: str,
    items: Sequence[Any],
    *,
    not_found: type[
        NotebookNotFoundError | SourceNotFoundError | NoteNotFoundError | ArtifactNotFoundError
    ],
) -> str:
    """Resolve ``token`` by title over ``items``: exact match wins, else unique prefix.

    Matching order (mirrors the id resolver's "exact wins over prefix"):

    1. Case-insensitive **exact** title: exactly one -> that id; more than one ->
       :class:`AmbiguousIdError`.
    2. No exact match -> case-insensitive title **prefix** (#1786): exactly one ->
       that id; more than one -> :class:`AmbiguousIdError` listing the candidates;
       none -> ``not_found(token)``.

    Exact precedence means a title that is also a prefix of a longer one
    (``"Report"`` vs ``"Report Q1"``) resolves to itself rather than being
    reported ambiguous.

    Items with no title (``item.title`` is ``None`` — e.g. an untitled source)
    fold to ``""`` and so never match a non-empty ``token`` on either axis.

    ``token`` is assumed already stripped and non-empty (callers run
    :func:`~notebooklm._app.resolve.validate_id` first), so an empty prefix
    cannot match every item.
    """
    # casefold (not lower) for correct non-ASCII case-insensitive matching, e.g.
    # German ß folds to "ss" so "STRASSE" matches a title "Straße". Fold each
    # title once up front so the two passes below don't re-casefold every item.
    token_folded = token.casefold()
    folded = [((item.title or "").casefold(), item) for item in items]

    exact = [item for title_folded, item in folded if title_folded == token_folded]
    if len(exact) == 1:
        (match,) = exact  # unpack (not exact[0]) — these are typed items, not an RPC row
        return str(match.id)
    if len(exact) > 1:
        raise _ambiguous_title_error(token, exact, kind="title")

    # No exact title -> fall back to a unique title prefix so a name is as
    # prefix-resolvable as an id (issue #1786). Kept as a second pass (rather than
    # a single fused loop) so exact-wins-over-prefix reads directly off the code.
    # This pass casefolds but deliberately does NOT dash/space-normalize (unlike
    # near_miss_candidates below): a punctuation-slip token like "Acme - X" for a
    # title "Acme — X" must stay a true miss here so it reaches the near-miss
    # "did you mean" path (#1787) rather than silently resolving to a near match.
    prefix = [item for title_folded, item in folded if title_folded.startswith(token_folded)]
    if len(prefix) == 1:
        (match,) = prefix
        return str(match.id)
    if len(prefix) > 1:
        raise _ambiguous_title_error(token, prefix, kind="prefix")

    # Neither an exact title nor a unique/ambiguous prefix matched -> a true miss.
    # Enrich it with near-miss "did you mean" candidates (issue #1787): a title
    # mistyped with a hyphen for an em-dash, a non-breaking space for a normal
    # one, or a not-quite prefix would otherwise force a blind retry loop.
    error = not_found(token)
    error.candidates = near_miss_candidates(
        token,
        items,
        id_of=lambda item: str(item.id),
        title_of=lambda item: item.title,
    )
    raise error


def _resolve_by_id_or_prefix(
    token: str,
    items: Sequence[Any],
    *,
    not_found: type[
        NotebookNotFoundError | SourceNotFoundError | NoteNotFoundError | ArtifactNotFoundError
    ],
) -> str:
    """Resolve a hex-ish ``token`` via ``resolve_ref``, mapping no-match to NotFound."""
    try:
        resolution = resolve_ref(
            token,
            items,
            id_of=lambda item: str(item.id),
            title_of=lambda item: item.title,
        )
    except AmbiguousIdError:
        # AmbiguousIdError subclasses ValidationError, so it MUST be caught and
        # re-raised before the ValidationError branch below — otherwise an
        # ambiguous prefix would be silently rewritten into a NotFound.
        raise
    except ValidationError as exc:
        # resolve_ref raises a bare ValidationError on no-match; surface it as the
        # domain-specific NotFound so every miss classifies as NOT_FOUND.
        raise not_found(token) from exc
    return resolution.id


def _resolve_hex(
    token: str,
    items: Sequence[Any],
    *,
    not_found: type[
        NotebookNotFoundError | SourceNotFoundError | NoteNotFoundError | ArtifactNotFoundError
    ],
) -> str:
    """Resolve a hex-ish ``token``, preferring id/prefix but falling back to title.

    A token like ``"beef"`` / ``"1234"`` is BOTH a valid hex id-prefix shape AND a
    plausible all-hex title. We keep id/prefix precedence (a concrete id must win),
    but when the id/prefix path finds nothing we fall back to a title match before
    giving up — otherwise an item titled with hex digits would be permanently
    unreachable by name.

    :class:`AmbiguousIdError` from an ambiguous prefix is **never** swallowed: it
    propagates with its candidate ids so the caller can disambiguate, rather than
    being reinterpreted as a (possibly-unrelated) title match.
    """
    try:
        return _resolve_by_id_or_prefix(token, items, not_found=not_found)
    except AmbiguousIdError:
        # An ambiguous prefix is a real, actionable result — do NOT fall through to
        # the title path; surface the candidates.
        raise
    except not_found:
        # The id/prefix path found nothing. Try a title match (exact, then unique
        # prefix) before failing so all-hex titles ("beef", "1234", "DEADBEEF")
        # remain reachable by name.
        return _resolve_by_title(token, items, not_found=not_found)


async def resolve_notebook(client: NotebookLMClient, ref: str) -> str:
    """Resolve a notebook reference (full/partial id, exact title, or unique title prefix) to its id.

    Args:
        client: The lifespan-bound client.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook list.

    Returns:
        The notebook's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        NotebookNotFoundError: No notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one notebook by prefix or title.
    """
    ref = validate_id(ref, "notebook")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref
    items = await client.notebooks.list()
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=NotebookNotFoundError)
    return _resolve_by_title(ref, items, not_found=NotebookNotFoundError)


async def resolve_source(client: NotebookLMClient, notebook_id: str, ref: str) -> str:
    """Resolve a source reference within a notebook to its id.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the source lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's source list.

    Returns:
        The source's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        SourceNotFoundError: No source in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one source by prefix or title.
    """
    ref = validate_id(ref, "source")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref
    items = await client.sources.list(notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=SourceNotFoundError)
    return _resolve_by_title(ref, items, not_found=SourceNotFoundError)


async def resolve_sources(
    client: NotebookLMClient, notebook_id: str, refs: Sequence[str]
) -> list[str]:
    """Resolve many source references within a notebook, listing sources at most once.

    The per-tool callers ``chat_ask`` / ``studio_generate`` previously resolved N
    refs via ``asyncio.gather(resolve_source(...) for ref in refs)``, which fired one
    ``client.sources.list(notebook_id)`` per non-UUID ref — N identical concurrent
    list RPCs. This resolves the whole batch against a single source-list snapshot.

    Matching rules are identical to :func:`resolve_source` (full-UUID fast-path,
    hex id/prefix, exact case-insensitive title, then unique title prefix) and
    reuse the same single-ref helpers, so behavior per ref is unchanged. An
    all-UUID batch still makes no list call (each ref takes the fast-path, as
    before). Two differences from the
    old ``gather`` path:

    * Non-UUID refs share a **single** ``sources.list`` snapshot instead of one
      concurrent list call per ref.
    * Errors are deterministic, not subject to ``gather``'s first-to-complete
      race: every ref is ``validate_id``-checked first (so an empty/whitespace
      ref raises before any resolution), then refs resolve sequentially over the
      snapshot, so a not-found / ambiguous ref raises in input order.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the sources live in.
        refs: Source references (full/partial id, exact title, or unique title prefix).

    Returns:
        The resolved canonical ids, in the same order as ``refs``. An empty
        ``refs`` returns an empty list (NOT ``None``): callers that treat
        "no refs" as "all sources" must keep their own ``if refs else None``
        guard — forwarding ``[]`` to the backend means "zero sources", which it
        refuses for source-requiring artifact types (#1652).

    Raises:
        ValidationError: A ref is empty/whitespace.
        SourceNotFoundError: A ref matches no source in the notebook.
        AmbiguousIdError: A ref matches more than one source by prefix or title.
    """
    validated = [validate_id(ref, "source") for ref in refs]
    # If every ref is already a full UUID, skip the list call entirely.
    if all(FULL_ID_PATTERN.fullmatch(ref) for ref in validated):
        return validated
    items = await client.sources.list(notebook_id)

    # Same matching dispatch as resolve_source, but against one shared snapshot.
    def match(ref: str) -> str:
        if FULL_ID_PATTERN.fullmatch(ref):
            return ref
        if _HEX_ISH.match(ref):
            return _resolve_hex(ref, items, not_found=SourceNotFoundError)
        return _resolve_by_title(ref, items, not_found=SourceNotFoundError)

    return [match(ref) for ref in validated]


async def resolve_note(client: NotebookLMClient, notebook_id: str, ref: str) -> str:
    """Resolve a note reference within a notebook to its id.

    Same matching rules as :func:`resolve_source`, over the notebook's note list.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the note lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's note list.

    Returns:
        The note's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        NoteNotFoundError: No note in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one note by prefix or title.
    """
    ref = validate_id(ref, "note")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref
    items = await client.notes.list(notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=NoteNotFoundError)
    return _resolve_by_title(ref, items, not_found=NoteNotFoundError)


async def resolve_artifact(client: NotebookLMClient, notebook_id: str, ref: str) -> str:
    """Resolve a studio-artifact reference within a notebook to its id.

    Same matching rules as :func:`resolve_source`, over the notebook's artifact
    list. ``client.artifacts.list`` MERGES both mind-map backings (interactive +
    note-backed) under one listing, so a note-backed mind map is resolvable by
    id / prefix / title here just like any other artifact.

    The full-UUID fast-path returns ``ref`` **verbatim with no list call**: this
    lets a concrete id (including a note-backed mind-map id, or one missing from a
    stale list) reach the ``_app`` core, which then routes it by kind.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the artifact lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's artifact list.

    Returns:
        The artifact's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        ArtifactNotFoundError: No artifact in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one artifact by prefix or title.
    """
    ref = validate_id(ref, "artifact")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref
    items = await client.artifacts.list(notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=ArtifactNotFoundError)
    return _resolve_by_title(ref, items, not_found=ArtifactNotFoundError)
