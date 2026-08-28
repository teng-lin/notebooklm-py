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

from ._types.documents import _PLACEHOLDER
from .exceptions import ChatResponseParseError

if TYPE_CHECKING:
    from .client import NotebookLMClient
    from .types import ChatReference, StructuredDocument

__all__ = ["resolve_chat_reference_passage"]


#: How much of ``cited_text`` to cross-check against the range it declares.
#: Long enough that agreeing by accident is implausible, short enough to
#: survive the local disagreements the two readings legitimately have (see
#: :func:`_quotes_the_same_text`).
_AGREEMENT_PROBE_CHARS = 32


def _declared_range(reference: ChatReference) -> tuple[int, int] | None:
    """The reference's own ``[start_char, end_char)``, or ``None`` if unusable.

    Source-independent: it reports what the *citation* declares, not whether
    any particular document contains it. Both callers need exactly that — the
    short-circuit below has to decide whether a fetch could possibly help
    before there is a document to check the range against.

    "Unusable" means **absent** — a citation whose fragment decoded no blocks,
    or one from before the offsets were read — or **empty**, where the two
    bounds coincide and the range selects nothing however it is resolved.

    Negative and inverted ranges are rejected here as well, even though
    ``ChatReference.__post_init__`` refuses to build one: the dataclass is
    mutable and its validation does not re-run on assignment, so a reference
    can carry a range this function did not see built. Re-checking is what
    keeps that from resolving to something — :meth:`StructuredDocument.render`
    clamps a negative lower bound to ``0``, which would quietly turn a broken
    range into the head of the source rather than a stand-down.
    """
    start, end = reference.start_char, reference.end_char
    if start is None or end is None or start < 0 or end <= start:
        return None
    return start, end


def _quotes_the_same_text(
    document: StructuredDocument, cited_text: str, start: int, end: int
) -> bool:
    """Whether ``[start, end)`` still resolves to what the citation quoted.

    The range is the exact instrument; ``cited_text`` is the independent
    reading of the same fragment, so comparing them catches the one failure
    :attr:`StructuredDocument.extent` cannot. An extent check only notices a
    source that *shrank*: re-index a source so that it grew, or so that text
    moved within it, and a stale range still fits and still resolves — to the
    wrong passage, silently. The value search this helper replaced had that
    detection for free, by failing to match; keeping it means the offset path
    is faster and exact *and* no blinder than what it replaced.

    This is a cross-check, not a locator: it never decides *where* the passage
    is, only whether to trust the offsets that already decided. A disagreement
    stands the range down and the search runs, which either finds the passage
    or reports that it could not.

    **Anchored at the start of the range, not merely contained in it.** Both
    readings begin at ``start``, so agreement means agreement *there*: text
    inserted ahead of the citation by a re-index leaves the old quote sitting
    further inside the range, where a containment test would accept a passage
    that opens with the inserted text and ends mid-quote.

    Compares a bounded prefix rather than the whole string because the two
    readings then diverge locally by design — ``cited_text`` omits positions
    this client cannot render as text where :meth:`StructuredDocument.slice`
    fills them with :data:`~notebooklm._types.documents._PLACEHOLDER`, and a
    run whose text disagrees with its declared width is normalised in one and
    not the other. The filler is imported rather than re-spelled here, so
    changing it cannot silently stop this check stripping it; leading
    whitespace is discarded on both sides, since ``cited_text`` starting at a
    block boundary routinely carries a newline the slice does not.

    A ``cited_text`` of nothing but whitespace leaves an empty probe, which
    matches trivially and stands the check down — correctly: it carries no
    evidence to contradict the range with.
    """
    probe = cited_text.strip()[:_AGREEMENT_PROBE_CHARS]
    resolved = document.slice(start, end).replace(_PLACEHOLDER, "").lstrip()
    return resolved.startswith(probe)


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
      empty (a citation whose fragment decoded no blocks, or one decoded before
      the offsets were);
    * ``end_char`` runs past :attr:`StructuredDocument.extent`, so the range and
      this document do not describe the same text. A document that decoded no
      blocks has extent ``0`` and fails here too, which is the same statement:
      there is no coordinate space to index;
    * the citation's own range renders nothing, because it lands entirely on
      positions that decoded no text (an image, a rule). Tested **before** the
      context window is applied, so a 200-unit window of neighbouring prose
      cannot stand in for a passage the citation does not actually have;
    * the range and ``cited_text`` disagree about what is there — see
      :func:`_quotes_the_same_text`.

    The window is widened by ``context_chars`` on each side in **UTF-16 code
    units**, the unit the range itself is in. The caller clamps it
    non-negative, for both paths at once.
    """
    declared = _declared_range(reference)
    if declared is None:
        return ""
    start, end = declared
    if end > document.extent:
        return ""
    if not document.render(start, end):
        return ""
    if reference.cited_text and not _quotes_the_same_text(
        document, reference.cited_text, start, end
    ):
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
            ``cited_text``. A citation whose fragment decoded no blocks at
            all — the structural anchor ``_web.rows.chat_stream`` reports as
            ``(None, None, None)`` — has neither and raises
            :class:`ChatResponseParseError` without issuing a request. A
            fragment holding only an image or a rule is *not* that case: it
            decodes a block, so it carries a range, and resolving it takes
            the fetch.
        context_chars: Approximate number of characters of surrounding
            context to return on each side of the cited span. Defaults
            to 200, which empirically lands ~1–2 sentences of context
            on either side for prose sources; negative values are clamped
            to 0. Counted in UTF-16 code units on the offset path (the
            unit the range is in) and in Python characters on the search
            fallback, so the same number buys one unit less context per
            astral character inside the window — no difference at all for
            text that stays in the Basic Multilingual Plane.

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
            made), or if neither path locates the passage. The message
            names only what was actually attempted, so it distinguishes a
            range that no longer fits its source from a citation that never
            carried one.
    """
    if not reference.cited_text and _declared_range(reference) is None:
        raise ChatResponseParseError(
            f"ChatReference for source {reference.source_id!r} has no "
            "cited_text and no character range to resolve. This is typical "
            "of structural-anchor citations whose fragment decoded no blocks "
            "at all, and which therefore have no plaintext passage to surface."
        )

    # Clamped once, here, so both paths widen by the same amount. Left
    # negative it does not merely fail to add context: the offset path narrows
    # until the citation resolves to nothing (silently changing *which* path
    # runs), and ``find_citation_context`` inverts its slice and returns "".
    context_chars = max(0, context_chars)

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

    # Report what was actually tried. Naming a failed search that never ran, or
    # blaming a range the citation does not carry, sends the reader after the
    # wrong cause — and the two causes have different remedies.
    declared = _declared_range(reference)
    attempts = [
        f"its character range ({reference.start_char}, {reference.end_char}) did not "
        f"resolve to text in the source's document (extent {fulltext.document.extent})"
        if declared is not None
        else "it carries no character range"
    ]
    if reference.cited_text:
        attempts.append("its cited_text was not found in the source's flat content")
    raise ChatResponseParseError(
        f"Could not locate the cited passage in source {reference.source_id!r} "
        f"of notebook {notebook_id!r}: " + ", and ".join(attempts) + ". The source "
        "may have been re-indexed since the citation was emitted, or the cited "
        "span may have been transformed during chunking."
    )
