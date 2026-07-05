"""Transport-neutral id validation and partial-id resolution.

This is the Click-free core of ``cli/resolve.py``: it owns the matching rules
(empty -> error, full-UUID fast-path, exact-id wins over prefix, unique-prefix,
ambiguous, no-match) and raises the public ``notebooklm.exceptions`` hierarchy
instead of ``click.ClickException`` / ``rich`` consoles. The CLI keeps a thin
wrapper that re-raises :class:`ValidationError` as ``click.ClickException`` and
handles the "Matched: ..." status prose; the MCP/HTTP adapters call this core
directly.

It must stay transport-neutral — no ``click`` / ``rich`` / ``cli`` / ``fastmcp``
imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..exceptions import ValidationError

# Backend entity IDs are canonical UUIDs in the RFC 4122 8-4-4-4-12 hex layout
# (e.g. ``abc12345-6789-4abc-def0-1234567890ab``). Only a string in exactly that
# layout qualifies for the full-id fast-path — anything shorter (even a unique
# 25-char prefix) must go through local list-and-match so it is expanded to the
# full id before any backend call. Mirrors ``cli.resolve.FULL_ID_PATTERN`` so
# the two resolvers agree on which inputs skip listing.
FULL_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Max candidate ids listed in an :class:`AmbiguousIdError` message.
_MAX_AMBIGUOUS_CANDIDATES = 5


class AmbiguousIdError(ValidationError):
    """A partial id matched more than one item.

    Subclasses :class:`~notebooklm.exceptions.ValidationError` so existing
    ``except ValidationError`` handlers (and the CLI's ``VALIDATION_ERROR``
    code path) keep catching an ambiguous id, while callers that want to react
    specifically to ambiguity — e.g. to print every candidate — can catch this
    type. Carries the colliding candidate ids for that purpose.

    Attributes:
        partial_id: The ambiguous input the caller supplied.
        candidate_ids: Every item id that matched ``partial_id`` as a prefix.
    """

    def __init__(self, partial_id: str, candidate_ids: Sequence[str], message: str):
        self.partial_id = partial_id
        self.candidate_ids = tuple(candidate_ids)
        super().__init__(message)


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a partial reference to a full id.

    Attributes:
        id: The full, canonical id of the matched item.
        matched_title: The matched item's title when the input was a partial id
            that had to be expanded (so an adapter can surface a "Matched: ..."
            hint). ``None`` when the input was already a full id (fast-path or
            exact match) or no title accessor was provided.
    """

    id: str
    matched_title: str | None = None


def _is_full_id_candidate(value: str) -> bool:
    """Return whether ``value`` is shaped like a concrete backend UUID."""
    return FULL_ID_PATTERN.fullmatch(value) is not None


def validate_id(value: str, name: str = "id") -> str:
    """Validate and normalize an entity id.

    Args:
        value: The id to validate.
        name: Human label for the error message, e.g. ``"notebook"`` or
            ``"source"``.

    Returns:
        ``value`` stripped of surrounding whitespace.

    Raises:
        ValidationError: If ``value`` is empty or whitespace-only.
    """
    if not value or not value.strip():
        raise ValidationError(f"{name} ID cannot be empty")
    return value.strip()


def resolve_ref(
    token: str,
    items: Sequence[Any],
    *,
    id_of: Callable[[Any], str],
    title_of: Callable[[Any], str | None] | None = None,
) -> Resolution:
    """Resolve a (possibly partial) reference against a pre-fetched item list.

    Click-free port of ``cli.resolve.resolve_partial_id_in_items`` matching
    rules, in order:

    1. Empty / whitespace ``token`` -> :class:`ValidationError`.
    2. Canonical 36-char 8-4-4-4-12 UUID -> returned verbatim without scanning
       ``items`` (the full-id fast-path). The caller is responsible for any
       authoritative-membership check it needs; this core trusts a full id.
    3. Case-insensitive exact match against an item id -> that item (exact wins
       over prefix so a short-but-complete id is not reported ambiguous when it
       is also a prefix of another item).
    4. Case-insensitive prefix match: unique -> that item; ambiguous ->
       :class:`AmbiguousIdError`; none -> :class:`ValidationError`.

    Args:
        token: Full or partial id to resolve.
        items: Pre-fetched items the caller already loaded from the backend.
            Each item is opaque; ``id_of`` / ``title_of`` extract fields.
        id_of: Accessor returning an item's canonical id.
        title_of: Optional accessor returning an item's title, used only to
            populate :attr:`Resolution.matched_title` on a successful partial
            (non-fast-path, non-exact) match.

    Returns:
        A :class:`Resolution` with the full id and (for partial matches) the
        matched title.

    Raises:
        ValidationError: ``token`` is empty, or no item matches.
        AmbiguousIdError: ``token`` is a non-unique prefix.
    """
    token = validate_id(token)

    # Rule 2 — concrete UUID passes through without scanning ``items`` so direct
    # get/delete by id does not force a list call first.
    if _is_full_id_candidate(token):
        return Resolution(id=token, matched_title=None)

    token_lower = token.lower()
    matches: list[Any] = []
    for item in items:
        item_id = id_of(item)
        item_id_lower = item_id.lower()
        # Rule 3 — exact short ids win over prefix matches (avoids false
        # ambiguity when an exact id is also a prefix of a longer one).
        if item_id_lower == token_lower:
            return Resolution(id=item_id, matched_title=None)
        if item_id_lower.startswith(token_lower):
            matches.append(item)

    # Rule 4 — unique prefix.
    if len(matches) == 1:
        matched = matches[0]
        matched_id = id_of(matched)
        title = title_of(matched) if title_of is not None else None
        return Resolution(id=matched_id, matched_title=title)

    # Rule 4 — no match.
    if not matches:
        raise ValidationError(f"No item found starting with '{token}'.")

    # Rule 4 — ambiguous prefix.
    candidate_ids = [id_of(item) for item in matches]
    lines = [f"Ambiguous ID '{token}' matches {len(matches)} items:"]
    for item in matches[:_MAX_AMBIGUOUS_CANDIDATES]:
        title = (title_of(item) if title_of is not None else None) or "(untitled)"
        lines.append(f"  {id_of(item)[:12]}... {title}")
    if len(matches) > _MAX_AMBIGUOUS_CANDIDATES:
        lines.append(f"  ... and {len(matches) - _MAX_AMBIGUOUS_CANDIDATES} more")
    lines.append("\nSpecify more characters to narrow down.")
    raise AmbiguousIdError(token, candidate_ids, "\n".join(lines))


# ---------------------------------------------------------------------------
# Near-miss suggestions for a failed *name* lookup (issue #1787)
# ---------------------------------------------------------------------------

#: Dash variants agents commonly type in place of one another (or of a plain
#: hyphen) when referencing an em-dash-heavy title: hyphen/non-breaking-hyphen,
#: figure/en/em dash, horizontal bar, and the minus sign. All fold to a plain
#: ``-`` before comparison so ``"… - Intel"`` still matches ``"… — Intel"``.
_DASH_TRANSLATION = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-"))

#: Minimum :func:`difflib.get_close_matches` similarity ratio for a fuzzy
#: near-miss suggestion. The library default; low enough to catch a couple of
#: transposed/changed characters, high enough to avoid noise.
_FUZZY_CUTOFF = 0.6

#: Default maximum near-miss candidates surfaced on a failed name lookup.
_MAX_NEAR_MISS_CANDIDATES = 3


def _normalize_for_match(text: str) -> str:
    """Fold a title/token for punctuation- and case-insensitive comparison.

    NFKC folds compatibility forms (e.g. a non-breaking space to a plain space),
    every dash variant collapses to a plain hyphen, runs of whitespace collapse
    to a single space, and the result is casefolded. This is what lets a hyphen
    typed for an em-dash — or a normal space typed for a non-breaking one — still
    match the real title.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_DASH_TRANSLATION)
    return " ".join(folded.split()).casefold()


def near_miss_candidates(
    token: str,
    items: Iterable[Any],
    *,
    id_of: Callable[[Any], str],
    title_of: Callable[[Any], str | None],
    limit: int = _MAX_NEAR_MISS_CANDIDATES,
) -> list[dict[str, str]]:
    """Return up to ``limit`` near-miss ``{"id", "title"}`` suggestions for ``token``.

    Enriches a failed *name* lookup (``NOT_FOUND``) with "did you mean" hints.
    Two cheap, stdlib-only passes over the already-fetched ``items`` (no extra
    RPC): a **prefix** pass (titles whose normalized form starts with the
    normalized token — catches ``"Scientific"`` -> ``"Scientific PDF Parsing —
    …"``), then a **fuzzy** pass via :func:`difflib.get_close_matches` over the
    normalized titles (catches punctuation-only differences such as em-dash vs
    hyphen, whose normalized forms are near-identical).

    Both passes compare *normalized* text (see :func:`_normalize_for_match`), so
    an em-dash/hyphen or non-breaking-space mismatch still surfaces the real
    title. Results de-duplicate by id, cap at ``limit``, and carry each item's
    ORIGINAL id and title. An empty/whitespace ``token``, or no near match,
    returns ``[]``.
    """
    norm_token = _normalize_for_match(token)
    if not norm_token:
        return []

    # (normalized_title, original_id, original_title) for every titled item.
    entries: list[tuple[str, str, str]] = []
    for item in items:
        title = title_of(item)
        if not title:
            continue
        entries.append((_normalize_for_match(title), str(id_of(item)), str(title)))

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(item_id: str, title: str) -> None:
        if item_id not in seen:
            seen.add(item_id)
            results.append({"id": item_id, "title": title})

    # Pass 1 — prefix matches, in list order.
    for norm_title, item_id, title in entries:
        if len(results) >= limit:
            return results
        if norm_title.startswith(norm_token):
            _add(item_id, title)

    # Pass 2 — fuzzy matches over the normalized titles for whatever slots remain.
    # ``norm_titles`` is de-duplicated (order-preserving) so a repeated title does
    # not waste a ``get_close_matches`` slot; every item sharing a matched
    # normalized title is then a candidate (no inner ``break``), so two items whose
    # titles differ only in punctuation both surface while slots remain. ``n=limit``
    # (not ``limit - len(results)``) is fine: ``limit`` is a cap, not a target, and
    # ``_add`` drops any fuzzy hit already surfaced by the prefix pass — so the
    # final count can be below ``limit`` even with more distinct matches available.
    if len(results) < limit and entries:
        norm_titles = list(dict.fromkeys(norm_title for norm_title, _, _ in entries))
        for match in difflib.get_close_matches(
            norm_token, norm_titles, n=limit, cutoff=_FUZZY_CUTOFF
        ):
            for norm_title, item_id, title in entries:
                if norm_title == match:
                    _add(item_id, title)
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break

    return results
