"""Private source content rendering service."""

from __future__ import annotations

import builtins
import logging
import reprlib
from typing import Any, Literal

from .._row_adapters.sources import SourceRow
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

        summary = ""
        keywords: list[str] = []

        if result and isinstance(result, list) and len(result) > 0:
            outer = result[0]
            if isinstance(outer, list) and len(outer) > 0:
                inner = outer[0]
                if isinstance(inner, list):
                    # Bind the ``[1]`` summary and ``[2]`` keywords blocks to locals
                    # so each leaf read is single-level (not chained ``inner[1][0]`` /
                    # ``inner[2][0]``). Absent blocks legitimately leave the defaults.
                    summary_block = (
                        inner[1] if len(inner) > 1 and isinstance(inner[1], list) else None
                    )
                    if summary_block:
                        summary = summary_block[0] if isinstance(summary_block[0], str) else ""
                    keyword_block = (
                        inner[2] if len(inner) > 2 and isinstance(inner[2], list) else None
                    )
                    if keyword_block:
                        keywords = keyword_block[0] if isinstance(keyword_block[0], list) else []

        return SourceGuide(summary=summary, keywords=tuple(keywords))

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

        title = ""
        source_type = None
        url = None
        content = ""

        # ``result[0]`` is the source-descriptor row; bind it so the title and
        # metadata reads are single-level indices instead of chained
        # ``result[0][1]`` / ``result[0][2]`` descents.
        descriptor = result[0]
        if isinstance(descriptor, list) and len(descriptor) > 1:
            title = descriptor[1] if isinstance(descriptor[1], str) else ""

            if len(descriptor) > 2 and isinstance(descriptor[2], list):
                metadata = descriptor[2]
                # The type-code read is delegated to ``SourceRow.type_code``
                # (the descriptor row has the adapter's normalized-entry
                # layout: id-envelope, title, metadata, ...), which validates
                # that ``metadata[4]`` holds an int. An absent / ``None`` slot
                # keeps the silent ``None`` default; a present-but-non-int
                # value also degrades to ``None`` (the "unknown type" default)
                # but logs a WARNING instead of silently passing a malformed
                # value into ``SourceFulltext._type_code`` (#1485
                # absence-vs-malformed policy).
                source_row = SourceRow.from_entry(descriptor, method_id=RPCMethod.GET_SOURCE.value)
                source_type = source_row.type_code
                if source_type is None and len(metadata) > 4 and metadata[4] is not None:
                    self._logger.warning(
                        "Source %s metadata type-code slot malformed (expected "
                        "int at metadata[4], got %s); treating type as unknown: %s",
                        source_id,
                        type(metadata[4]).__name__,
                        reprlib.repr(metadata),
                    )
                url = _extract_source_url(metadata, allow_bare_http=False)

        if output_format == "markdown":
            html_content = None
            # ``result[4]`` is the HTML-rendition block; bind it so the candidate
            # read is a single-level ``html_block[1]`` index. An absent block
            # legitimately means "no markdown rendition" (warned + empty below).
            html_block = result[4] if len(result) > 4 and isinstance(result[4], list) else None
            if html_block is not None and len(html_block) > 1:
                candidate = html_block[1]
                if isinstance(candidate, str):
                    html_content = candidate
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
            # ``result[3]`` is the text-content block; bind it so the blocks read
            # is a single-level ``text_block[0]`` index. An absent block
            # legitimately means "no text content" (empty content + warning).
            text_block = result[3] if len(result) > 3 and isinstance(result[3], list) else None
            if text_block:
                content_blocks = text_block[0]
                if isinstance(content_blocks, list):
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
