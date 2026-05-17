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
    from .types import ChatReference

__all__ = ["resolve_chat_reference_passage"]


async def resolve_chat_reference_passage(
    client: NotebookLMClient,
    notebook_id: str,
    reference: ChatReference,
    context_chars: int = 200,
) -> str:
    """Return the surrounding source-text passage for a chat citation.

    A :class:`~notebooklm.types.ChatReference` carries a ``source_id``
    plus a (usually truncated) ``cited_text`` snippet. The streaming
    chat response does not include the surrounding paragraph — only the
    matched span — so re-rendering a citation in a UI typically requires
    fetching the source's full text and locating the cited span within
    it. This helper performs that round-trip in one call.

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
        reference: The chat citation to resolve. Must have a non-empty
            ``cited_text`` — structural-anchor citations (single-char
            page/section markers, image refs) are not resolvable and
            will raise :class:`ChatResponseParseError`.
        context_chars: Approximate number of characters of surrounding
            context to return on each side of the cited span. Defaults
            to 200, which empirically lands ~1–2 sentences of context
            on either side for prose sources.

    Returns:
        The surrounding passage as a single string. When the citation
        matches multiple times in the source (the cited text appears
        repeatedly), the first match is returned — callers that need to
        disambiguate should use
        :meth:`~notebooklm.types.SourceFulltext.find_citation_context`
        directly to inspect all matches.

    Raises:
        ChatResponseParseError: If the reference has no ``cited_text``
            to search for, or if the cited text cannot be located in
            the source's indexed content (the citation may be a
            structural anchor, or the source content may have been
            re-chunked since the citation was emitted).
    """
    if not reference.cited_text:
        raise ChatResponseParseError(
            f"ChatReference for source {reference.source_id!r} has no "
            "cited_text to resolve. This is typical of structural-anchor "
            "citations (image/section markers) that have no plaintext "
            "passage to surface."
        )

    fulltext = await client.sources.get_fulltext(notebook_id, reference.source_id)

    matches = fulltext.find_citation_context(
        reference.cited_text,
        context_chars=context_chars,
    )

    if not matches:
        raise ChatResponseParseError(
            f"Could not locate cited_text in source {reference.source_id!r} "
            f"of notebook {notebook_id!r}. The source may have been "
            "re-indexed since the citation was emitted, or the cited span "
            "may have been transformed during chunking."
        )

    passage, _position = matches[0]
    return passage
