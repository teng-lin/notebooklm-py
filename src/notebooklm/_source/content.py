"""Private source content rendering service."""

from __future__ import annotations

import builtins
import logging
import reprlib
from typing import Any, Literal

from .._row_adapters.sources import SourceFulltextRow, SourceGuideRow
from .._runtime.contracts import RpcCaller
from .._types.research import SourceGuide
from ..rpc import RPCMethod
from ..types import SourceFulltext, SourceNotFoundError, _extract_source_url


class SourceContentRenderer:
    """Render source guide and fulltext content from source RPC responses."""

    def __init__(self, rpc: RpcCaller, logger: logging.Logger | None = None) -> None:
        self._rpc = rpc
        self._logger = logger or logging.getLogger(__name__)

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
                from markdownify import markdownify as md
            except ImportError:
                raise ImportError(
                    "The 'markdown' format requires the 'markdownify' package. "
                    "Install it with: pip install 'notebooklm-py[markdown]'"
                ) from None

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
            source_type = source_row.type_code if source_row is not None else None
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

        if output_format == "markdown":
            # An absent HTML rendition legitimately means "no markdown
            # rendition" (warned + empty below).
            html_content = fulltext_row.html_content
            if html_content is not None:
                content = md(html_content, heading_style="ATX")
            else:
                self._logger.warning(
                    "Source %s (type=%s) has no HTML rendition for output_format='markdown'; "
                    "returning empty content. Retry with output_format='text'.",
                    source_id,
                    source_type,
                )
        else:
            # An absent text block legitimately means "no text content"
            # (empty content + warning).
            content_blocks = fulltext_row.text_content_blocks
            if content_blocks is not None:
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
            title=title,
            content=content,
            _type_code=source_type,
            url=url,
            char_count=len(content),
        )

    def extract_all_text(
        self, data: builtins.list[Any], max_depth: int = 100
    ) -> builtins.list[str]:
        """Recursively extract all text strings from nested arrays."""
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
