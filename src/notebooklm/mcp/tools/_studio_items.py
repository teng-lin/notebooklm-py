"""Cross-type Studio helpers shared by the Studio (``studio.py``) tools.

The Studio surface presents a notebook's **notes AND generated artifacts** as one
merged panel. These helpers own that merge + the cross-type reference resolution
used by ``studio_list(item=…)`` and ``studio_delete``:

* :func:`_studio_items` fetches + projects notes and artifacts into one flat list
  with a shared hyphenated ``type`` discriminator.
* :func:`_resolve_studio_item` resolves a ref (note OR artifact) over that merged
  list, raising the public NOT_FOUND / ambiguous errors on a miss / collision.

Split out of ``studio.py`` to keep that module under the ADR-0008 size budget;
it is cohesive (all cross-type note∩artifact plumbing) rather than an arbitrary
slice. This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..._app.resolve import (
    FULL_ID_PATTERN,
    AmbiguousIdError,
    resolve_ref,
    validate_id,
)
from ...exceptions import NotFoundError, ValidationError
from ...types import ArtifactType

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = [
    "StudioResolvedItem",
    "hyphenated_type",
    "resolve_studio_item",
    "studio_items",
    "summarize_studio_item",
]

#: Chars of a note body surfaced as ``content_preview`` in ``studio_list``'s default
#: summary mode; the full body stays reachable via ``detail="full"`` or ``item=<ref>``.
NOTE_PREVIEW_CHARS = 200

#: A studio-item ref made only of hex digits and dashes takes the id/prefix path;
#: anything else (a space, a non-hex letter, punctuation) is treated as a title.
#: Mirrors ``mcp._resolve._HEX_ISH`` so the cross-type resolver classifies refs
#: exactly like the single-type ``resolve_*`` helpers.
_HEX_REF = re.compile(r"^[0-9a-fA-F-]+$")

#: Max candidate ids surfaced in an ambiguous cross-type title error.
_MAX_AMBIGUOUS_CANDIDATES = 5

#: ``ArtifactType`` → the hyphenated ``type`` spelling the whole Studio surface
#: uses (matching ``studio_generate``'s ``artifact_type`` input vocabulary). Only
#: the underscore members need remapping; every other member's ``.value`` is
#: already hyphen-free, and ``ArtifactType.UNKNOWN.value`` is ``"unknown"``.
_ARTIFACT_TYPE_HYPHEN: dict[ArtifactType, str] = {
    ArtifactType.SLIDE_DECK: "slide-deck",
    ArtifactType.MIND_MAP: "mind-map",
    ArtifactType.DATA_TABLE: "data-table",
}


def hyphenated_type(kind: ArtifactType) -> str:
    """Return the hyphenated Studio ``type`` string for an artifact ``kind``."""
    return _ARTIFACT_TYPE_HYPHEN.get(kind, kind.value)


#: Valid Studio ``kind`` FILTER values: ``note`` plus each concrete artifact kind's
#: hyphenated form. Used to reject an unknown ``kind`` up front rather than silently
#: returning an empty page (or a false NOT_FOUND on the by-ref path). ``unknown`` is
#: a pass-through *display* value for an unrecognized artifact, NOT a filterable kind,
#: so it is excluded here.
STUDIO_KINDS: frozenset[str] = frozenset(
    {"note"} | {hyphenated_type(t) for t in ArtifactType if t is not ArtifactType.UNKNOWN}
)


@dataclass(frozen=True)
class StudioResolvedItem:
    """A cross-type studio item resolved from a ref (note OR artifact).

    ``type`` is the hyphenated Studio vocabulary (``note`` for a text note, else
    the artifact's hyphenated kind). ``raw`` is the full projected item dict from
    :func:`studio_items` so a caller (``studio_list(item=…)``) can return the
    complete item without re-listing.
    """

    item_id: str
    type: str
    title: str | None
    raw: dict[str, Any] | None = None


async def studio_items(client: NotebookLMClient, nb_id: str) -> list[dict[str, Any]]:
    """Fetch + merge a notebook's text notes and studio artifacts into one list.

    Concurrently reads ``client.notes.list`` (text notes only — the notes facade
    drops mind-map rows) and ``client.artifacts.list`` (every artifact, incl. both
    mind-map backings), projecting each onto a flat item dict with a shared
    hyphenated ``type`` discriminator:

    * note → ``{"id", "title", "type": "note", "content"}``
    * artifact → ``{"id", "title", "type": <hyphenated kind>, "status_label", "url"}``

    Items are keyed by id (notes first) so a hypothetical future note∩artifact
    overlap can't double-list — this never fires today (``notes.list`` excludes
    mind maps, the only rows both listings could share).
    """
    notes, artifacts = await asyncio.gather(client.notes.list(nb_id), client.artifacts.list(nb_id))
    items: dict[str, dict[str, Any]] = {}
    for note in notes:
        items.setdefault(
            str(note.id),
            {
                "id": str(note.id),
                "title": note.title,
                "type": "note",
                "content": note.content,
            },
        )
    for art in artifacts:
        art_id = str(art.id)
        # Dedup by id (notes first) — never fires: notes.list excludes mind maps.
        if art_id in items:
            continue
        items[art_id] = {
            "id": art_id,
            "title": art.title,
            "type": hyphenated_type(art.kind),
            "status_label": getattr(art, "status_str", None),
            "url": getattr(art, "url", None),
        }
    return list(items.values())


def summarize_studio_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return ``studio_list``'s summary-mode projection of one merged item.

    A ``note`` item's full ``content`` body is replaced with a bounded
    ``content_preview`` (first :data:`NOTE_PREVIEW_CHARS` chars, ``…`` appended when
    truncated) plus the full-body ``char_count``, so a READ_ONLY discovery listing of a
    notebook with long notes doesn't spill every body into an agent's context. The full
    body stays reachable via ``studio_list(detail="full")`` or ``studio_list(item=<ref>)``.

    Keyed on ``type == "note"`` (not the presence of a ``content`` key), so artifact
    items — which carry no ``content`` — pass through unchanged, and a note with an
    empty/``None`` body is still summarized (``content_preview=""`` / ``char_count=0``).
    """
    if item.get("type") != "note":
        # Return a fresh dict (not the original ref) so the projection's contract is
        # symmetric — a caller can never mutate the in-flight items list through it.
        return dict(item)
    content = item.get("content") or ""
    preview = content[:NOTE_PREVIEW_CHARS]
    if len(content) > NOTE_PREVIEW_CHARS:
        preview += "…"
    summarized = {k: v for k, v in item.items() if k != "content"}
    summarized["content_preview"] = preview
    summarized["char_count"] = len(content)
    return summarized


def _match_studio_ref(
    items: list[dict[str, Any]], ref: str, kind: str | None
) -> dict[str, Any] | None:
    """Match ``ref`` over the merged studio ``items``, honoring a ``kind`` scope.

    Matching order mirrors the single-type ``resolve_*`` helpers: full-UUID /
    exact id / unique hex-prefix (via :func:`resolve_ref`), then exact
    case-insensitive title (so an all-hex title stays reachable). Returns the
    matched item dict, or ``None`` on a miss (the caller decides whether a miss is
    NOT_FOUND or an idempotent no-op). Raises :class:`AmbiguousIdError` on an
    ambiguous prefix or title.
    """
    scoped = [it for it in items if kind is None or it["type"] == kind]
    if _HEX_REF.match(ref):
        try:
            resolution = resolve_ref(
                ref,
                scoped,
                id_of=lambda it: it["id"],
                title_of=lambda it: it.get("title"),
            )
        except AmbiguousIdError:
            # An ambiguous prefix is actionable — surface it, never fall to title.
            raise
        except ValidationError:
            resolution = None
        if resolution is not None:
            # ``resolve_ref``'s full-UUID fast-path returns the token verbatim
            # without a membership check; confirm membership case-insensitively
            # (also normalizes a prefix match to the list's canonical id).
            rid = resolution.id.casefold()
            match = next((it for it in scoped if it["id"].casefold() == rid), None)
            if match is not None:
                return match
        # A full UUID is an unambiguous id form — NEVER fall through to title
        # matching. Otherwise a note whose title happens to be a UUID could be
        # matched (and deleted) by a full-UUID ref, and studio_delete's
        # absent-full-UUID idempotent no-op would instead delete a title-collision
        # note. A short hex *prefix* may legitimately be a title, so it still
        # falls through.
        if FULL_ID_PATTERN.fullmatch(ref):
            return None
        # Fall through to the title path (id/prefix found nothing).
    ref_folded = ref.casefold()
    titled = [it for it in scoped if (it.get("title") or "").casefold() == ref_folded]
    if len(titled) == 1:
        (match,) = titled  # unpack (not titled[0]) — avoids the positional-index gate
        return match
    if len(titled) > 1:
        candidate_ids = [it["id"] for it in titled]
        lines = [f"Ambiguous title '{ref}' matches {len(titled)} items:"]
        for it in titled[:_MAX_AMBIGUOUS_CANDIDATES]:
            lines.append(f"  {it['id'][:12]}... {it.get('title') or '(untitled)'}")
        if len(titled) > _MAX_AMBIGUOUS_CANDIDATES:
            lines.append(f"  ... and {len(titled) - _MAX_AMBIGUOUS_CANDIDATES} more")
        lines.append("\nUse a more specific title or the id.")
        raise AmbiguousIdError(ref, candidate_ids, "\n".join(lines))
    return None


async def resolve_studio_item(
    client: NotebookLMClient, nb_id: str, ref: str, kind: str | None = None
) -> StudioResolvedItem:
    """Resolve a cross-type studio ref (note OR artifact) over the merged list.

    Builds :func:`studio_items` once and matches ``ref`` (full-UUID / hex-prefix /
    exact title) within an optional ``kind`` scope. A miss raises
    :class:`~notebooklm.exceptions.NotFoundError` (NOT_FOUND category); an ambiguous
    ref raises :class:`AmbiguousIdError`.
    """
    ref = validate_id(ref, "item")
    items = await studio_items(client, nb_id)
    match = _match_studio_ref(items, ref, kind)
    if match is None:
        raise NotFoundError(f"Studio item not found: {ref}")
    return StudioResolvedItem(
        item_id=match["id"],
        type=match["type"],
        title=match.get("title"),
        raw=match,
    )
