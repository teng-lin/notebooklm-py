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

* A full canonical UUID is returned lowercased with **no list call** (so a tool
  invoked with a concrete id never pays for a list). Lowercased because the
  backend matches ids case-sensitively and its ids are always lowercase: an
  uppercase UUID would read as not found, and a delete by one would silently do
  nothing.
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

Tools that change data pass ``strict=True``: the title path
then accepts only a tier-1 match (canonical caseless exact title, or a unique
prefix), and a ref that only the accent-insensitive tier would match is refused
with :class:`ValidationError` naming the candidates, so a typo or a missing accent
can never rename, delete or share a different item. Reads and confirmation
previews keep the full tiered match; a preview returns the canonical id to
confirm with.

Strict IDs-only mode (``NOTEBOOKLM_MCP_STRICT_IDS=1``, off by default) turns every
resolver below into a full-UUID gate: a name, title, or short id *prefix* is
rejected with :class:`ValidationError` **before any list call**, so a long-lived
automation fails loud and deterministically instead of fuzzy-matching a token
whose meaning can drift (issue #1808). See :data:`STRICT_IDS_ENV`.

This module imports NO ``click`` / ``rich`` / ``cli`` — only the ``_app``
resolve core and the public exception hierarchy.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .._app.artifacts import require_complete_artifact_listing
from .._app.resolve import (
    FULL_ID_PATTERN,
    AmbiguousIdError,
    match_title,
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
    "partition_source_refs",
    "reject_non_canonical_id",
    "resolve_artifact",
    "resolve_note",
    "resolve_notebook",
    "resolve_notebook_listed",
    "resolve_source",
    "resolve_source_listed",
    "resolve_sources",
]

#: A token made only of hex digits and dashes routes to the id/prefix path; any
#: other character (a space, a letter outside ``a-f``, punctuation) routes to the
#: title path. Mirrors the plan's ``^[0-9a-fA-F-]+$`` discriminator.
_HEX_ISH = re.compile(r"^[0-9a-fA-F-]+$")

#: Max candidate ids surfaced in an ambiguous-title error message.
_MAX_AMBIGUOUS_CANDIDATES = 5

#: Opt-in strict IDs-only mode (issue #1808). When ``NOTEBOOKLM_MCP_STRICT_IDS`` is
#: ``"1"``, every resolver below rejects any reference that is not a full canonical
#: UUID — a name, a title, or even a would-be-unique short id *prefix* — so a
#: long-lived automation fails loud and deterministically instead of fuzzy-matching
#: a token whose meaning can drift as the notebook changes. Off (unset / any other
#: value) leaves the default name/prefix/title resolution untouched. Governs every
#: notebook / source / note / artifact reference parameter — the resolvers here plus
#: the studio ``item`` resolver and ``studio_download``'s explicit ``artifact_id``,
#: which share :func:`reject_non_canonical_id`. A secondary convenience like
#: ``source_list``'s ``label`` filter is out of scope (it uses the shared CLI/MCP
#: label resolver). The name matches the
#: ``NOTEBOOKLM_MCP_*`` family and the ``== "1"`` convention of
#: ``NOTEBOOKLM_MCP_TRUST_PROXY`` / ``NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND``.
STRICT_IDS_ENV = "NOTEBOOKLM_MCP_STRICT_IDS"

#: How a strict-path error names the accent-insensitive tier, shared with the
#: Studio resolver so the two messages cannot drift.
ACCENT_ONLY_STEP = "when ignoring accents or Unicode compatibility forms"


def _strict_ids_enabled() -> bool:
    """Return whether strict IDs-only mode is enabled.

    Read at call time (not cached at import) so tests and late-set environments
    are honored, mirroring the other ``NOTEBOOKLM_MCP_*`` runtime flags.
    """
    return os.environ.get(STRICT_IDS_ENV) == "1"


def reject_non_canonical_id(ref: str, kind: str) -> None:
    """In strict IDs mode, reject ``ref`` unless it is a full canonical UUID.

    Raises **before** any list call so the outcome is deterministic and does not
    depend on the current list contents. A no-op when strict mode is off, or when
    ``ref`` is already a full canonical UUID (which every resolver fast-paths).

    Raises:
        ValidationError: strict mode is on and ``ref`` is a name / title / short
            id prefix rather than a full ``8-4-4-4-12`` UUID.
    """
    if _strict_ids_enabled() and not FULL_ID_PATTERN.fullmatch(ref):
        raise ValidationError(
            f"{STRICT_IDS_ENV}=1: {kind} reference {ref!r} is not a canonical id. "
            "Strict mode requires a full UUID (8-4-4-4-12), not a name or prefix."
        )


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


def _accent_only_error(token: str, matches: Sequence[Any]) -> ValidationError:
    """Refuse a tier-2-only title match on a path that changes data.

    Lists every candidate with its full id, so the caller can retry with the id
    (or the exact title) instead of guessing.
    """
    lines = [
        f"'{token}' matches only {ACCENT_ONLY_STEP}. Tools that change data need "
        "the exact title, a unique title prefix, or the id. Candidates:"
    ]
    for item in matches[:_MAX_AMBIGUOUS_CANDIDATES]:
        lines.append(f"  {item.id} {item.title or '(untitled)'}")
    if len(matches) > _MAX_AMBIGUOUS_CANDIDATES:
        lines.append(f"  ... and {len(matches) - _MAX_AMBIGUOUS_CANDIDATES} more")
    return ValidationError("\n".join(lines))


def _resolve_by_title(
    token: str,
    items: Sequence[Any],
    *,
    not_found: type[
        NotebookNotFoundError | SourceNotFoundError | NoteNotFoundError | ArtifactNotFoundError
    ],
    strict: bool = False,
) -> str:
    """Resolve ``token`` by title over ``items``: exact match wins, else unique prefix.

    Matching order (mirrors the id resolver's "exact wins over prefix"; see
    :func:`~notebooklm._app.resolve.match_title`):

    1. Case-insensitive **exact** title: exactly one -> that id; more than one ->
       :class:`AmbiguousIdError`.
    2. No exact match -> case-insensitive title **prefix** (#1786): exactly one ->
       that id; more than one -> :class:`AmbiguousIdError` listing the candidates.
    3. Neither -> steps 1 and 2 again, accent-insensitively (#2476), so
       ``"istanbul"`` finds ``"İstanbul"``; none -> ``not_found(token)``.

    Exact precedence means a title that is also a prefix of a longer one
    (``"Report"`` vs ``"Report Q1"``) resolves to itself rather than being
    reported ambiguous.

    Items with no title (``item.title`` is ``None`` — e.g. an untitled source)
    fold to ``""`` and so never match a non-empty ``token`` on either axis.

    ``token`` is assumed already stripped and non-empty (callers run
    :func:`~notebooklm._app.resolve.validate_id` first), so an empty prefix
    cannot match every item.

    ``strict`` is for tools that change data: step 3 is skipped, and a ref that
    only step 3 would match raises :class:`ValidationError` listing the
    candidates instead of resolving. Steps 1 and 2 (including the unique prefix)
    still apply, as before tiered matching.
    """
    # Shared tiered matcher (issue #2476): canonical caseless exact, then prefix
    # (issue #1786, so a name is as prefix-resolvable as an id); only on a miss the
    # same two passes again accent-insensitively. casefold (not lower) means German
    # ß folds to "ss" so "STRASSE" matches a title "Straße". The keys deliberately
    # do NOT dash/space-normalize (unlike near_miss_candidates below): a
    # punctuation-slip token like "Acme - X" for a title "Acme — X" must stay a
    # true miss here so it reaches the near-miss "did you mean" path (#1787) rather
    # than silently resolving to a near match.
    matches, kind = match_title(
        token, items, title_of=lambda item: item.title, accent_insensitive=not strict
    )
    if len(matches) == 1:
        (match,) = matches  # unpack (not matches[0]) — typed items, not an RPC row
        return str(match.id)
    if matches:
        raise _ambiguous_title_error(token, matches, kind=kind or "title")
    if strict:
        accent_only, _ = match_title(token, items, title_of=lambda item: item.title)
        if accent_only:
            raise _accent_only_error(token, accent_only)

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
    strict: bool = False,
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
        return _resolve_by_title(token, items, not_found=not_found, strict=strict)


async def resolve_notebook(client: NotebookLMClient, ref: str, *, strict: bool = False) -> str:
    """Resolve a notebook reference (full/partial id, exact title, or unique title prefix) to its id.

    Args:
        client: The lifespan-bound client.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook list.
        strict: Set by tools that change data. The title path then refuses a
            match that only the accent-insensitive tier finds (see
            :func:`_resolve_by_title`).

    Returns:
        The notebook's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        NotebookNotFoundError: No notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one notebook by prefix or title.
    """
    return (await resolve_notebook_listed(client, ref, strict=strict))[0]


async def resolve_notebook_listed(
    client: NotebookLMClient, ref: str, *, strict: bool = False
) -> tuple[str, Sequence[Any] | None]:
    """:func:`resolve_notebook`, plus the notebook list it read (``None`` if it read none).

    A preview reuses the list for the title instead of listing a second time.
    """
    ref = validate_id(ref, "notebook")
    reject_non_canonical_id(ref, "notebook")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref.lower(), None
    items = await client.notebooks.list()
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=NotebookNotFoundError, strict=strict), items
    return _resolve_by_title(ref, items, not_found=NotebookNotFoundError, strict=strict), items


async def resolve_source(
    client: NotebookLMClient, notebook_id: str, ref: str, *, strict: bool = False
) -> str:
    """Resolve a source reference within a notebook to its id.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the source lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's source list.
        strict: Set by tools that change data. The title path then refuses a
            match that only the accent-insensitive tier finds (see
            :func:`_resolve_by_title`).

    Returns:
        The source's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        SourceNotFoundError: No source in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one source by prefix or title.
    """
    return (await resolve_source_listed(client, notebook_id, ref, strict=strict))[0]


async def resolve_source_listed(
    client: NotebookLMClient, notebook_id: str, ref: str, *, strict: bool = False
) -> tuple[str, Sequence[Any] | None]:
    """:func:`resolve_source`, plus the source list it read (``None`` if it read none)."""
    ref = validate_id(ref, "source")
    reject_non_canonical_id(ref, "source")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref.lower(), None
    items = await client.sources.list(notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=SourceNotFoundError, strict=strict), items
    return _resolve_by_title(ref, items, not_found=SourceNotFoundError, strict=strict), items


async def resolve_sources(
    client: NotebookLMClient, notebook_id: str, refs: Sequence[str], *, strict: bool = False
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
        strict: Set by tools that change data. The title path then refuses a
            match that only the accent-insensitive tier finds (see
            :func:`_resolve_by_title`).

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
    # Strict mode rejects every non-UUID ref up front — BEFORE the fast-path and
    # the list call below — so a mixed UUID/name batch fails without a source-list
    # RPC (deterministic, no leak).
    for ref in validated:
        reject_non_canonical_id(ref, "source")
    # If every ref is already a full UUID, skip the list call entirely.
    if all(FULL_ID_PATTERN.fullmatch(ref) for ref in validated):
        return [ref.lower() for ref in validated]
    items = await client.sources.list(notebook_id)

    # Same matching dispatch as resolve_source, but against one shared snapshot.
    def match(ref: str) -> str:
        if FULL_ID_PATTERN.fullmatch(ref):
            return ref.lower()
        if _HEX_ISH.match(ref):
            return _resolve_hex(ref, items, not_found=SourceNotFoundError, strict=strict)
        return _resolve_by_title(ref, items, not_found=SourceNotFoundError, strict=strict)

    return [match(ref) for ref in validated]


def partition_source_refs(
    refs: Sequence[str], items: Sequence[Any]
) -> tuple[list[str], list[dict[str, str]]]:
    """Resolve refs against one source-list snapshot without aborting the batch.

    Full UUIDs must be members of ``items`` — an unknown UUID is ``not_found``,
    never trusted (``sources.delete`` silently no-ops missing ids). Missing
    title/prefix refs accumulate in ``not_found`` instead of raising. Empty
    refs and ambiguous matches still raise.
    """
    validated = [validate_id(ref, "source") for ref in refs]
    for ref in validated:
        reject_non_canonical_id(ref, "source")
    canonical_ids = {item.id.casefold(): item.id for item in items}
    resolved: list[str] = []
    not_found: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_missing: set[str] = set()
    for ref in validated:
        try:
            if FULL_ID_PATTERN.fullmatch(ref):
                if ref.casefold() not in canonical_ids:
                    raise SourceNotFoundError(ref)
                sid = canonical_ids[ref.casefold()]
            elif _HEX_ISH.match(ref):
                sid = _resolve_hex(ref, items, not_found=SourceNotFoundError)
            else:
                sid = _resolve_by_title(ref, items, not_found=SourceNotFoundError)
        except SourceNotFoundError as exc:
            if ref.casefold() not in seen_missing:
                seen_missing.add(ref.casefold())
                not_found.append({"source_id": ref, "error": str(exc)})
            continue
        if sid not in seen:
            seen.add(sid)
            resolved.append(sid)
    return resolved, not_found


async def resolve_note(
    client: NotebookLMClient, notebook_id: str, ref: str, *, strict: bool = False
) -> str:
    """Resolve a note reference within a notebook to its id.

    Same matching rules as :func:`resolve_source`, over the notebook's note list.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the note lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's note list.
        strict: Set by tools that change data. The title path then refuses a
            match that only the accent-insensitive tier finds (see
            :func:`_resolve_by_title`).

    Returns:
        The note's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        NoteNotFoundError: No note in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one note by prefix or title.
    """
    ref = validate_id(ref, "note")
    reject_non_canonical_id(ref, "note")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref.lower()
    items = await client.notes.list(notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=NoteNotFoundError, strict=strict)
    return _resolve_by_title(ref, items, not_found=NoteNotFoundError, strict=strict)


async def resolve_artifact(
    client: NotebookLMClient, notebook_id: str, ref: str, *, strict: bool = False
) -> str:
    """Resolve a studio-artifact reference within a notebook to its id.

    Same matching rules as :func:`resolve_source`, over the notebook's artifact
    list. ``client.artifacts.list`` MERGES both mind-map backings (interactive +
    note-backed) under one listing, so a note-backed mind map is resolvable by
    id / prefix / title here just like any other artifact.

    The full-UUID fast-path returns ``ref`` **lowercased with no list call**: this
    lets a concrete id (including a note-backed mind-map id, or one missing from a
    stale list) reach the ``_app`` core, which then routes it by kind.

    Fuzzy title/prefix matching requires a **complete** aggregate inventory
    (``list_with_status().is_complete``). A notes outage (or other secondary
    backing failure) must not turn an ambiguous title/prefix into a unique hit
    or project a partial miss as :class:`ArtifactNotFoundError`.

    Args:
        client: The lifespan-bound client.
        notebook_id: The (already-resolved) notebook id the artifact lives in.
        ref: A full canonical UUID, a hex id prefix, an exact (case-insensitive)
            title, or a unique (case-insensitive) title prefix over the
            notebook's artifact list.
        strict: Set by tools that change data. The title path then refuses a
            match that only the accent-insensitive tier finds (see
            :func:`_resolve_by_title`).

    Returns:
        The artifact's canonical id.

    Raises:
        ValidationError: ``ref`` is empty/whitespace.
        ArtifactNotFoundError: No artifact in the notebook matches ``ref``.
        AmbiguousIdError: ``ref`` matches more than one artifact by prefix or title.
        RPCError: The aggregate listing was incomplete, so a title/prefix match
            cannot be treated as unique or as absence.
    """
    ref = validate_id(ref, "artifact")
    reject_non_canonical_id(ref, "artifact")
    # Full UUID fast-path — never list.
    if FULL_ID_PATTERN.fullmatch(ref):
        return ref.lower()
    items = await require_complete_artifact_listing(client, notebook_id)
    if _HEX_ISH.match(ref):
        return _resolve_hex(ref, items, not_found=ArtifactNotFoundError, strict=strict)
    return _resolve_by_title(ref, items, not_found=ArtifactNotFoundError, strict=strict)
