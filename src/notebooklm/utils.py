"""Public utility helpers for notebooklm-py users.

This module hosts thin, dependency-free helpers that compose the
namespaced client APIs into common end-user shapes. Helpers here are
top-level functions (not methods on dataclasses) so that:

* They can stay async and call into the live client without forcing
  every dataclass to hold a backreference to the open client.
* They can be re-exported from :mod:`notebooklm` for one-line
  imports (``from notebooklm import resolve_chat_reference_passage``).

The contract is intentionally narrow — each helper should be useful
without reading the source, and parse-failure paths should raise the
domain-specific exceptions from :mod:`notebooklm.exceptions` rather
than returning sentinel strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import ChatResponseParseError

if TYPE_CHECKING:
    from .client import NotebookLMClient
    from .types import ChatReference, StructuredDocument

__all__ = ["resolve_chat_reference_passage"]


def _declared_range(reference: ChatReference) -> tuple[int, int] | None:
    """The reference's own ``[start_char, end_char)``, or ``None`` if unusable.

    Source-independent: it reports what the *citation* declares, not whether
    any particular document contains it. Both callers need exactly that — the
    short-circuit below has to decide whether a fetch could possibly help
    before there is a document to check the range against.

    ``ChatReference`` already rejects a half-populated, negative or inverted
    range at construction, so "unusable" here means **absent** (a citation
    decoded before the offsets were, or one the server sent without them) or
    **zero-width** — the anchor shape the backend emits at an insertion point,
    which selects no text however it is resolved.
    """
    start, end = reference.start_char, reference.end_char
    if start is None or end is None or end <= start:
        return None
    return start, end


def _rendered_window(
    document: StructuredDocument,
    reference: ChatReference,
    context_chars: int,
) -> str:
    """The readable passage around ``reference``'s declared range, or ``""``.

    Returns ``""`` — the caller's signal to fall back to the value-based
    search — for every reason the range might not be usable, rather than
    returning a short or empty passage as though it had resolved:

    * :func:`_declared_range` rejects it: an offset is absent, or the range is
      empty or inverted (a structural-anchor citation, or a reference decoded
      before the offsets were);
    * the document decoded no blocks, so there is no coordinate space to index;
    * ``end_char`` runs past :attr:`StructuredDocument.extent`, which means the
      range and this document do not describe the same text — the source was
      re-indexed since the citation was emitted, most likely. Slicing anyway
      would silently return the truncated head of the range, or nothing;
    * the range lands entirely on positions that decoded no text (an image, a
      rule), where there is genuinely no passage to show.

    The window is widened by ``context_chars`` on each side in **UTF-16 code
    units**, the unit the range itself is in.
    """
    declared = _declared_range(reference)
    if declared is None:
        return ""
    start, end = declared
    extent = document.extent
    if extent == 0 or end > extent:
        return ""
    return document.render(start - context_chars, end + context_chars)


async def resolve_chat_reference_passage(
    client: NotebookLMClient,
    notebook_id: str,
    reference: ChatReference,
    context_chars: int = 200,
) -> str:
    """Return the surrounding source-text passage for a chat citation.

    A :class:`~notebooklm.types.ChatReference` carries a ``source_id``, a
    character range into the source document, and a (usually truncated)
    ``cited_text`` snippet. The streaming chat response does not include the
    surrounding paragraph — only the matched span — so re-rendering a citation
    in a UI typically requires fetching the source's full text and locating the
    cited span within it. This helper performs that round-trip in one call.

    **How the span is located.** The reference's ``start_char`` / ``end_char``
    index the source's parsed document, so when they are present and lie inside
    it the passage is read straight out of that coordinate space — exact, with
    no search — and returned in the readable rendering
    (:meth:`~notebooklm.types.StructuredDocument.render`: runs joined within a
    block, blocks separated).

    The older value-based search
    (:meth:`~notebooklm.types.SourceFulltext.find_citation_context`, over the
    flat ``content``) remains as the fallback, for a reference with no usable
    offsets or a source whose document did not decode. That search compares
    ``cited_text`` against a rendering that joins text runs with ``"\\n"``
    while ``cited_text`` uses no separator at all, so a key spanning a block
    boundary cannot match anywhere; #2210 bounded the key to keep it inside one
    block, and resolving by offset is what removes the failure mode rather than
    bounding it (`#2211
    <https://github.com/teng-lin/notebooklm-py/issues/2211>`_).

    The helper is deliberately a top-level function rather than a
    method on ``ChatReference``. ``ChatReference`` does not store a
    client backreference (citations are values, not handles) and has no
    ``notebook_id`` — both are required to fetch source content. Putting
    the helper here keeps ``ChatReference`` a plain value type while
    still offering a one-liner to end users::

        from notebooklm import resolve_chat_reference_passage

        passage = await resolve_chat_reference_passage(
            client, notebook_id, ask_result.references[0]
        )

    Args:
        client: An open :class:`~notebooklm.client.NotebookLMClient`.
        notebook_id: The notebook the citation belongs to. Required
            because the underlying fulltext RPC is notebook-scoped.
        reference: The chat citation to resolve. Must carry either a
            usable ``start_char`` / ``end_char`` range or a non-empty
            ``cited_text`` — a structural-anchor citation (single-char
            page/section markers, image refs) has neither and raises
            :class:`ChatResponseParseError` without issuing a request.
        context_chars: Approximate number of characters of surrounding
            context to return on each side of the cited span. Defaults
            to 200, which empirically lands ~1–2 sentences of context
            on either side for prose sources. Counted in UTF-16 code
            units on the offset path (the unit the range is in) and in
            Python characters on the search fallback; the two differ only
            for a source containing astral characters, and only by a
            character or so of context either way.

    Returns:
        The surrounding passage as a single string — a readable rendering
        of the document window on the offset path, or a window of the flat
        ``content`` on the search fallback. When the search fallback is
        used and the cited text appears repeatedly, the first match is
        returned; callers that need to disambiguate should use
        :meth:`~notebooklm.types.SourceFulltext.find_citation_context`
        directly to inspect all matches.

    Raises:
        ChatResponseParseError: If the reference carries neither offsets
            nor ``cited_text`` (nothing to resolve, and no request is
            made), or if neither path locates the passage — the source may
            have been re-indexed since the citation was emitted, leaving
            its offsets pointing outside the current document and its
            cited text no longer present.
    """
    if not reference.cited_text and _declared_range(reference) is None:
        raise ChatResponseParseError(
            f"ChatReference for source {reference.source_id!r} has no "
            "cited_text and no character range to resolve. This is typical "
            "of structural-anchor citations (image/section markers) that "
            "have no plaintext passage to surface."
        )

    fulltext = await client.sources.get_fulltext(notebook_id, reference.source_id)

    # Preferred: the citation's own range into the source document, which is
    # exact. Falls through to the search for a reference or a source the range
    # cannot be used on — see ``_rendered_window`` for each such reason.
    passage = _rendered_window(fulltext.document, reference, context_chars)
    if passage:
        return passage

    if reference.cited_text:
        matches = fulltext.find_citation_context(
            reference.cited_text,
            context_chars=context_chars,
        )
        if matches:
            passage, _position = matches[0]
            return passage

    raise ChatResponseParseError(
        f"Could not locate the cited passage in source {reference.source_id!r} "
        f"of notebook {notebook_id!r}: its character range "
        f"({reference.start_char}, {reference.end_char}) is not usable against "
        f"the source's document (extent {fulltext.document.extent}), and its "
        "cited_text was not found in the source's flat content. The source may "
        "have been re-indexed since the citation was emitted, or the cited span "
        "may have been transformed during chunking."
    )
