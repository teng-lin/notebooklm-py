"""Codec-backed source-content fake for service/facade compatibility tests."""

from __future__ import annotations

import logging
from typing import Any, Literal

from notebooklm._semantic.projectors import project_source_fulltext, project_source_guide
from notebooklm._types.research import SourceGuide
from notebooklm._web.codec.sources import decode_source_fulltext, decode_source_guide
from notebooklm.exceptions import SourceNotFoundError
from notebooklm.types import SourceFulltext


class CodecSourceContentService:
    """Decode an injected web payload behind the renderer's semantic protocol."""

    def __init__(self, response: Any, *, logger: logging.Logger | None = None) -> None:
        self.response = response
        self.logger = logger or logging.getLogger(__name__)

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        del notebook_id, source_id
        return project_source_guide(decode_source_guide(self.response))

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        record = decode_source_fulltext(
            self.response,
            source_id=source_id,
            output_format=output_format,
            logger=self.logger,
        )
        if record is None:
            raise SourceNotFoundError(f"Source {source_id} not found in notebook {notebook_id}")
        return project_source_fulltext(record)


__all__ = ["CodecSourceContentService"]
