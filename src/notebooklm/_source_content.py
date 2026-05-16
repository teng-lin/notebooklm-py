"""Private source content rendering service."""

from __future__ import annotations

import builtins
import logging
from typing import Any, Literal, Protocol

from .rpc import RPCMethod
from .types import SourceFulltext, SourceNotFoundError, _extract_source_url


class RpcCall(Protocol):
    async def __call__(
        self,
        method: RPCMethod,
        params: builtins.list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        """Call a NotebookLM RPC method."""


class SourceContentRenderer:
    """Render source guide and fulltext content from source RPC responses."""

    def __init__(self, rpc_call: RpcCall, logger: logging.Logger | None = None) -> None:
        self._rpc_call = rpc_call
        self._logger = logger or logging.getLogger(__name__)

    async def get_guide(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        """Get AI-generated summary and keywords for a specific source."""
        params = [[[[source_id]]]]
        result = await self._rpc_call(
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
                    if len(inner) > 1 and isinstance(inner[1], list) and len(inner[1]) > 0:
                        summary = inner[1][0] if isinstance(inner[1][0], str) else ""
                    if len(inner) > 2 and isinstance(inner[2], list) and len(inner[2]) > 0:
                        keywords = inner[2][0] if isinstance(inner[2][0], list) else []

        return {"summary": summary, "keywords": keywords}

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

        result = await self._rpc_call(
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

        if isinstance(result[0], list) and len(result[0]) > 1:
            title = result[0][1] if isinstance(result[0][1], str) else ""

            if len(result[0]) > 2 and isinstance(result[0][2], list):
                metadata = result[0][2]
                if len(metadata) > 4:
                    source_type = metadata[4]
                url = _extract_source_url(metadata, allow_bare_http=False)

        if output_format == "markdown":
            html_content = None
            if len(result) > 4 and isinstance(result[4], list) and len(result[4]) > 1:
                candidate = result[4][1]
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
            if len(result) > 3 and isinstance(result[3], list) and len(result[3]) > 0:
                content_blocks = result[3][0]
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
