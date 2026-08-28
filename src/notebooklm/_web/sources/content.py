"""Private source content rendering service."""

from __future__ import annotations

import builtins
import logging
import reprlib
from typing import Any, Literal

from ..._types.documents import StructuredDocument
from ..._types.research import SourceGuide
from ..._types.sources import _disambiguate_type_code, _pdf_url_title_fallback
from ...rpc import RPCMethod
from ...types import SourceFulltext, SourceNotFoundError, _extract_source_url
from ..contracts import RpcCaller
from ..rows.documents import build_document
from ..rows.sources import SourceFulltextRow, SourceGuideRow


class SourceContentRenderer:
    """Render source guide and fulltext content from source RPC responses."""

    def __init__(self, rpc: RpcCaller, logger: logging.Logger | None = None) -> None:
        self._rpc = rpc
        self._logger = logger or logging.getLogger("notebooklm._source.content")

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        """Get AI-generated summary and keywords for a specific source."""
        params = [[[[source_id]]]]
        result = await self._rpc.rpc_call(
            RPCMethod.GET_SOURCE_GUIDE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        # Position knowledge for the ``result[0][0]`` envelope unwrap and the
        # summary / keyword block reads lives in ``SourceGuideRow`` (the
        # sanctioned row-adapter layer); the adapter preserves the historical
        # soft contract — an absent / non-list envelope or block leaves the
        # ``""`` / ``[]`` defaults rather than raising.
        guide_row = SourceGuideRow(result)
        return SourceGuide(summary=guide_row.summary, keywords=tuple(guide_row.keywords))

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        """Get the full content of a source."""
        if output_format not in ("text", "markdown"):
            raise ValueError(f"Invalid format: '{output_format}'. Must be 'text' or 'markdown'.")

        if output_format == "markdown":
            try:
                import markdownify  # noqa: F401  # presence guard; conversion is below
            except ImportError:
                raise ImportError(
                    "The 'markdown' format requires the 'markdownify' package. "
                    "Install it with: pip install 'notebooklm-py[markdown]'"
                ) from None
            from ..._source.markdown import html_to_markdown

        params = [[source_id], [3], [3]] if output_format == "markdown" else [[source_id], [2], [2]]

        result = await self._rpc.rpc_call(
            RPCMethod.GET_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if not result or not isinstance(result, list):
            raise SourceNotFoundError(f"Source {source_id} not found in notebook {notebook_id}")

        source_type = None
        url = None
        content = ""
        document = StructuredDocument()

        # All positional knowledge for the ``GET_SOURCE`` envelope (descriptor
        # row, metadata, HTML / text blocks) lives in ``SourceFulltextRow`` (the
        # sanctioned row-adapter layer); every read preserves the historical
        # soft contract (missing slots -> empty defaults, never a raise).
        fulltext_row = SourceFulltextRow(result)
        title = fulltext_row.title
        metadata = fulltext_row.metadata
        if metadata is not None:
            # The type-code read is delegated to ``SourceRow.type_code``
            # (the descriptor row has the adapter's normalized-entry
            # layout: id-envelope, title, metadata, ...), which validates
            # that ``metadata[4]`` holds an int. An absent / ``None`` slot
            # keeps the silent ``None`` default; a present-but-non-int
            # value also degrades to ``None`` (the "unknown type" default)
            # but logs a WARNING instead of silently passing a malformed
            # value into ``SourceFulltext._type_code`` (#1485
            # absence-vs-malformed policy).
            source_row = fulltext_row.source_row
            # Disambiguate the type_code==14 native-Sheet/Drive-PDF overload by
            # the row MIME, mirroring ``Source.from_row`` so ``source fulltext``
            # / ``source_read`` decode a Drive-hosted PDF as PDF, not
            # GOOGLE_SPREADSHEET (#1832). GET_SOURCE carries the same MIME at
            # metadata[19] / metadata[9][2] as GET_NOTEBOOK (live-captured).
            source_type = (
                _disambiguate_type_code(source_row.type_code, source_row.mime)
                if source_row is not None
                else None
            )
            type_slot = fulltext_row.raw_metadata_type_slot
            if source_type is None and type_slot is not None:
                self._logger.warning(
                    "Source %s metadata type-code slot malformed (expected "
                    "int at metadata[4], got %s); treating type as unknown: %s",
                    source_id,
                    type(type_slot).__name__,
                    reprlib.repr(metadata),
                )
            url = _extract_source_url(metadata, allow_bare_http=False)

        # Parse the document tree (#2128) regardless of which rendering the
        # caller asked for. The response carries it either way — the two
        # ``output_format`` values pick which *legacy flat* rendition fills
        # ``content``, not which parts of the payload exist — so gating the
        # parse on the text branch would leave a markdown caller unable to
        # resolve citation offsets against a tree that was right there.
        content_blocks = fulltext_row.text_content_blocks
        if content_blocks is not None:
            document = build_document(content_blocks)

        if output_format == "markdown":
            # An absent HTML rendition legitimately means "no markdown
            # rendition" (warned + empty below).
            html_content = fulltext_row.html_content
            if html_content is not None:
                content = html_to_markdown(html_content, source_type=source_type)
            else:
                self._logger.warning(
                    "Source %s (type=%s) has no HTML rendition for output_format='markdown'; "
                    "returning empty content. Retry with output_format='text'.",
                    source_id,
                    source_type,
                )
        elif content_blocks is not None:
            # An absent text block legitimately means "no text content"
            # (empty content + warning). ``content`` stays the flat legacy
            # rendering — byte-identical, so no existing caller moves — while
            # ``document`` above retains the structure and offsets.
            texts = self.extract_all_text(content_blocks)
            content = "\n".join(texts)

        if not content:
            self._logger.warning(
                "Source %s returned empty content (type=%s, title=%s)",
                source_id,
                source_type,
                title,
            )

        return SourceFulltext(
            source_id=source_id,
            # Same #1850 direct-PDF-URL title fallback as ``Source.from_row``,
            # so ``source fulltext`` shows the basename too (not the raw URL).
            # ``or title`` keeps the ``str`` type: the helper only ever returns
            # the (str) input title or a derived stem, never ``None`` here.
            title=_pdf_url_title_fallback(title, url, source_type) or title,
            content=content,
            _type_code=source_type,
            url=url,
            char_count=len(content),
            document=document,
        )

    def extract_all_text(
        self, data: builtins.list[Any], max_depth: int = 100
    ) -> builtins.list[str]:
        """Recursively extract all text strings from nested arrays.

        The **legacy** flat rendering behind :attr:`SourceFulltext.content`.
        It is deliberately frozen: every string in traversal order, structure
        and offsets discarded, so existing callers see byte-identical output.
        The joins its caller applies insert separators the backend's character
        ranges never accounted for, which is why no citation offset can be used
        against the result (#2128). The offset-bearing parse of the same tree is
        :func:`notebooklm._web.rows.documents.build_document`, surfaced as
        :attr:`SourceFulltext.document`; prefer it for anything that needs to
        know *where* text sits.
        """
        if max_depth <= 0:
            self._logger.warning("Max recursion depth reached in text extraction")
            return []

        texts: builtins.list[str] = []
        for item in data:
            if isinstance(item, str) and len(item) > 0:
                texts.append(item)
            elif isinstance(item, builtins.list):
                texts.extend(self.extract_all_text(item, max_depth - 1))
        return texts


__all__ = ["SourceContentRenderer"]
